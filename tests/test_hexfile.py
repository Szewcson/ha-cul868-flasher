from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from app.hexfile import APPLICATION_FLASH_BYTES, HexFileError, parse_hex_file, write_upload

from .helpers import intel_hex_record, minimal_hex


class HexFileTests(unittest.TestCase):
    def test_accepts_minimal_application_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.hex"
            path.write_bytes(minimal_hex())
            image = parse_hex_file(path)

        self.assertEqual(image.lowest_address, 0)
        self.assertEqual(image.highest_address, 3)
        self.assertEqual(image.data_bytes, 4)

    def test_rejects_data_in_dfu_programmer_reserved_region(self) -> None:
        content = (
            intel_hex_record(0, 0, b"\x0c\x94")
            + "\n"
            + intel_hex_record(APPLICATION_FLASH_BYTES, 0, b"\xff")
            + "\n"
            + intel_hex_record(0, 1)
            + "\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.hex"
            path.write_text(content, encoding="ascii")
            with self.assertRaisesRegex(HexFileError, "reserved CUL868 bootloader"):
                parse_hex_file(path)

    def test_rejects_overlap_and_missing_reset_vector(self) -> None:
        content = (
            intel_hex_record(1, 0, b"\x01\x02")
            + "\n"
            + intel_hex_record(2, 0, b"\x03")
            + "\n"
            + intel_hex_record(0, 1)
            + "\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.hex"
            path.write_text(content, encoding="ascii")
            with self.assertRaisesRegex(HexFileError, "overlapping"):
                parse_hex_file(path)

    def test_rejects_records_after_eof(self) -> None:
        content = minimal_hex() + intel_hex_record(4, 0, b"\x01").encode() + b"\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.hex"
            path.write_bytes(content)
            with self.assertRaisesRegex(HexFileError, "after the end-of-file"):
                parse_hex_file(path)

    def test_write_upload_requires_exact_declared_length_and_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            upload = io.BytesIO(minimal_hex())
            path = write_upload(upload, len(minimal_hex()), Path(directory))
            self.assertEqual(path.read_bytes(), minimal_hex())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.unlink()

            with self.assertRaisesRegex(HexFileError, "ended before"):
                write_upload(io.BytesIO(b"short"), 6, Path(directory))
