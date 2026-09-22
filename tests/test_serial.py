from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.serial import CulSerial, supports_cul_led_control


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

    def test_led_control_accepts_verified_culfw_derived_version_responses(self) -> None:
        self.assertTrue(supports_cul_led_control("V 1.67 CUL868"))
        self.assertTrue(
            supports_cul_led_control("V 1.26.08 a-culfw Build: test CUL868 (F-Band: 868MHz)")
        )
        self.assertTrue(supports_cul_led_control("VTS 0.43 CUL868"))
        self.assertFalse(supports_cul_led_control("VX 1.0 CUL868"))

    def test_led_command_uses_documented_lowercase_modes_and_crlf(self) -> None:
        serial = CulSerial(Path("/dev/ttyACM0"), 9_600)
        serial._descriptor = 42
        with (
            patch.object(serial, "_write_all") as write_all,
            patch("app.serial.termios.tcdrain") as drain,
        ):
            serial.set_led(True)
            serial.set_led(False)

        self.assertEqual(write_all.call_args_list[0].args, (b"l01\r\n",))
        self.assertEqual(write_all.call_args_list[1].args, (b"l00\r\n",))
        self.assertEqual(drain.call_count, 2)
