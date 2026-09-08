from __future__ import annotations

import unittest
from pathlib import Path

from app.models import Settings, ValidationError


class SettingsTests(unittest.TestCase):
    def test_accepts_schema_style_baudrate_string(self) -> None:
        settings = Settings.from_mapping(
            {"device": "/dev/ttyACM0", "baudrate": "9600", "boot_timeout": 45}
        )
        self.assertEqual(settings.device, Path("/dev/ttyACM0"))
        self.assertEqual(settings.baudrate, 9600)

    def test_rejects_non_device_path_and_unknown_baudrate(self) -> None:
        with self.assertRaisesRegex(ValidationError, "mapped /dev"):
            Settings.from_mapping({"device": "/tmp/ttyACM0"})
        with self.assertRaisesRegex(ValidationError, "not supported"):
            Settings.from_mapping({"device": "/dev/ttyACM0", "baudrate": 230400})
