"""Bounded Intel HEX admission for the CUL868 V3 application flash area."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO


MAX_HEX_FILE_BYTES = 512 * 1024
# CUL V3 itself reserves its 2 KiB bootloader at 0x7800. Upstream
# dfu-programmer's atmega32u4 target conservatively reserves the top 4 KiB,
# however, and refuses data from 0x7000 upward. Enforcing its 0x7000 limit
# here makes validation a reliable promise that the invoked tool can accept
# the image while preserving a larger-than-physical bootloader margin.
APPLICATION_FLASH_BYTES = 0x7000
_UPLOAD_CHUNK_BYTES = 16 * 1024


class HexFileError(ValueError):
    """An uploaded file is not a safe CUL868 V3 application Intel HEX image."""


@dataclass(frozen=True)
class HexImage:
    """A verified one-shot application image held in the private tmpfs."""

    path: Path
    size: int
    sha256: str
    records: int
    data_bytes: int
    lowest_address: int
    highest_address: int


def write_upload(stream: BinaryIO, content_length: int, directory: Path = Path("/tmp")) -> Path:
    """Copy an exactly length-delimited Ingress upload to a mode-0600 temp file."""

    if not 1 <= content_length <= MAX_HEX_FILE_BYTES:
        raise HexFileError(
            f"firmware upload must be between 1 and {MAX_HEX_FILE_BYTES} bytes"
        )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="cul868-", suffix=".hex", dir=directory)
    path = Path(temporary_name)
    remaining = content_length
    try:
        with os.fdopen(descriptor, "wb") as destination:
            while remaining:
                chunk = stream.read(min(_UPLOAD_CHUNK_BYTES, remaining))
                if not chunk:
                    raise HexFileError("firmware upload ended before its declared length")
                if len(chunk) > remaining:
                    raise HexFileError("firmware upload exceeded its declared length")
                destination.write(chunk)
                remaining -= len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(path, 0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def parse_hex_file(path: Path) -> HexImage:
    """Validate one Intel HEX file without allowing it near the bootloader."""

    try:
        size = path.stat().st_size
    except OSError as err:
        raise HexFileError("uploaded firmware file is unavailable") from err
    if not 1 <= size <= MAX_HEX_FILE_BYTES:
        raise HexFileError(
            f"firmware file must be between 1 and {MAX_HEX_FILE_BYTES} bytes"
        )
    try:
        source = path.read_bytes()
    except OSError as err:
        raise HexFileError("uploaded firmware file cannot be read") from err
    if len(source) != size:
        raise HexFileError("uploaded firmware changed while it was being validated")
    return _parse_hex(source, path)


def _parse_hex(source: bytes, path: Path) -> HexImage:
    try:
        text = source.decode("ascii")
    except UnicodeDecodeError as err:
        raise HexFileError("firmware must be an ASCII Intel HEX file") from err
    lines = text.splitlines()
    if not lines:
        raise HexFileError("firmware file is empty")

    occupied = bytearray(APPLICATION_FLASH_BYTES)
    upper_address = 0
    data_bytes = 0
    lowest_address: int | None = None
    highest_address = -1
    saw_eof = False
    record_count = 0

    for line_number, line in enumerate(lines, start=1):
        if saw_eof:
            raise HexFileError(f"line {line_number}: data appears after the end-of-file record")
        record = _parse_record(line, line_number)
        record_count += 1
        byte_count = record[0]
        address = (record[1] << 8) | record[2]
        record_type = record[3]
        payload = record[4:-1]

        if record_type == 0x00:
            if byte_count == 0:
                raise HexFileError(f"line {line_number}: empty data records are not supported")
            absolute_address = (upper_address << 16) | address
            end_address = absolute_address + byte_count
            if absolute_address < 0 or end_address > APPLICATION_FLASH_BYTES:
                raise HexFileError(
                    f"line {line_number}: data targets the reserved CUL868 bootloader area or outside flash"
                )
            for offset in range(absolute_address, end_address):
                if occupied[offset]:
                    raise HexFileError(f"line {line_number}: overlapping data records are not allowed")
                occupied[offset] = 1
            data_bytes += byte_count
            lowest_address = (
                absolute_address if lowest_address is None else min(lowest_address, absolute_address)
            )
            highest_address = max(highest_address, end_address - 1)
        elif record_type == 0x01:
            if byte_count != 0 or address != 0:
                raise HexFileError(f"line {line_number}: end-of-file record is malformed")
            saw_eof = True
        elif record_type == 0x04:
            if byte_count != 2 or address != 0:
                raise HexFileError(f"line {line_number}: extended linear address record is malformed")
            upper_address = (payload[0] << 8) | payload[1]
        else:
            raise HexFileError(
                f"line {line_number}: unsupported Intel HEX record type 0x{record_type:02x}"
            )

    if not saw_eof:
        raise HexFileError("firmware has no end-of-file record")
    if data_bytes == 0 or lowest_address is None or highest_address < 0:
        raise HexFileError("firmware has no application data records")
    if not occupied[0]:
        raise HexFileError("firmware must include the application reset vector at address 0")

    return HexImage(
        path=path,
        size=len(source),
        sha256=sha256(source).hexdigest(),
        records=record_count,
        data_bytes=data_bytes,
        lowest_address=lowest_address,
        highest_address=highest_address,
    )


def _parse_record(line: str, line_number: int) -> bytes:
    if not line or not line.isascii() or not line.startswith(":"):
        raise HexFileError(f"line {line_number}: Intel HEX records must start with ':'")
    encoded = line[1:]
    if len(encoded) < 10 or len(encoded) % 2:
        raise HexFileError(f"line {line_number}: Intel HEX record length is invalid")
    try:
        record = bytes.fromhex(encoded)
    except ValueError as err:
        raise HexFileError(f"line {line_number}: Intel HEX record has non-hexadecimal data") from err
    if len(record) != record[0] + 5:
        raise HexFileError(f"line {line_number}: Intel HEX byte count does not match the record")
    if sum(record) & 0xFF:
        raise HexFileError(f"line {line_number}: Intel HEX checksum is invalid")
    return record
