from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.serial import CulSerial


class CulSerialTests(unittest.TestCase):
    def _version_from(self, response: bytes) -> tuple[str, object]:
        serial = CulSerial(Path("/dev/ttyACM0"), 9_600)
        serial._descriptor = 42
        with (
            patch.object(serial, "_write_all") as write_all,
            patch("app.serial.select.select", return_value=([42], [], [])),
            patch("app.serial.os.read", return_value=response),
        ):
            return serial.version(), write_all

    def test_tsculf_vts_response_uses_documented_crlf_request(self) -> None:
        version, write_all = self._version_from(b"VTS 0.43 CUL868\r\n")

        self.assertEqual(version, "VTS 0.43 CUL868")
        write_all.assert_called_once_with(b"V\r\n")

    def test_culfw_space_prefixed_response_remains_supported(self) -> None:
        version, write_all = self._version_from(b"V 1.67 CUL868\r\n")

        self.assertEqual(version, "V 1.67 CUL868")
        write_all.assert_called_once_with(b"V\r\n")
