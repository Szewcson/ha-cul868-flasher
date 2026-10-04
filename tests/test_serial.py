from __future__ import annotations

import termios
import unittest
from pathlib import Path
from unittest.mock import patch

from app.serial import CulSerial, CulSerialError, supports_cul_led_control


class CulSerialTests(unittest.TestCase):
    @staticmethod
    def _attributes(speed: int) -> list[object]:
        return [0, 0, termios.CS8, 0, speed, speed, [0] * 32]

    def test_open_reasserts_cdc_line_coding_before_using_the_cul(self) -> None:
        original = self._attributes(termios.B9600)
        configured = self._attributes(termios.B9600)
        serial = CulSerial(Path("/dev/ttyACM0"), 9_600)

        with (
            patch("app.serial.os.open", return_value=42),
            patch("app.serial.os.close"),
            patch("app.serial.fcntl.ioctl"),
            patch("app.serial.termios.tcgetattr", side_effect=[original, configured]),
            patch("app.serial.termios.tcsetattr") as set_attributes,
            patch("app.serial.termios.tcflush"),
            serial,
        ):
            pass

        self.assertEqual(set_attributes.call_args_list[0].args[2][4], termios.B115200)
        self.assertEqual(set_attributes.call_args_list[0].args[2][5], termios.B115200)
        self.assertEqual(set_attributes.call_args_list[1].args[2][4], termios.B9600)
        self.assertEqual(set_attributes.call_args_list[1].args[2][5], termios.B9600)

    def test_open_reasserts_115200_with_9600_as_the_alternate_speed(self) -> None:
        original = self._attributes(termios.B115200)
        configured = self._attributes(termios.B115200)
        serial = CulSerial(Path("/dev/ttyACM0"), 115_200)

        with (
            patch("app.serial.os.open", return_value=42),
            patch("app.serial.os.close"),
            patch("app.serial.fcntl.ioctl"),
            patch("app.serial.termios.tcgetattr", side_effect=[original, configured]),
            patch("app.serial.termios.tcsetattr") as set_attributes,
            patch("app.serial.termios.tcflush"),
            serial,
        ):
            pass

        self.assertEqual(set_attributes.call_args_list[0].args[2][4], termios.B9600)
        self.assertEqual(set_attributes.call_args_list[0].args[2][5], termios.B9600)
        self.assertEqual(set_attributes.call_args_list[1].args[2][4], termios.B115200)
        self.assertEqual(set_attributes.call_args_list[1].args[2][5], termios.B115200)

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
            serial.set_led("on")
            serial.set_led("off")
            serial.set_led("blink")

        self.assertEqual(write_all.call_args_list[0].args, (b"l01\r\n",))
        self.assertEqual(write_all.call_args_list[1].args, (b"l00\r\n",))
        self.assertEqual(write_all.call_args_list[2].args, (b"l02\r\n",))
        self.assertEqual(drain.call_count, 3)

    def test_led_command_rejects_unknown_modes_before_writing(self) -> None:
        serial = CulSerial(Path("/dev/ttyACM0"), 9_600)

        with self.assertRaisesRegex(CulSerialError, "off, on, or blink"):
            serial.set_led("pulse")

    def test_uptime_and_bare_mbus_commands_accept_only_expected_responses(self) -> None:
        serial = CulSerial(Path("/dev/ttyACM0"), 9_600)
        serial._descriptor = 42
        with (
            patch.object(serial, "_write_all") as write_all,
            patch("app.serial.select.select", return_value=([42], [], [])),
            patch("app.serial.os.read", return_value=b"0001E848\r\n"),
        ):
            self.assertEqual(serial.uptime_ticks(), 125_000)

        self.assertEqual(write_all.call_args.args, (b"t\r\n",))
        with (
            patch.object(serial, "_write_all") as write_all,
            patch("app.serial.select.select", return_value=([42], [], [])),
            patch("app.serial.os.read", return_value=b"TMODE\r\n"),
        ):
            self.assertEqual(serial.mbus_mode(), "TMODE")

        self.assertEqual(write_all.call_args.args, (b"b\r\n",))
