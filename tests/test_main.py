from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.flasher import FlashError
from app.main import _verify_startup_firmware


class StartupVerificationTests(unittest.TestCase):
    def test_logs_verified_version_without_interrupting_startup(self) -> None:
        flasher = Mock()
        flasher.verify_running_application.return_value = "V 1.2 CUL868"

        with patch("app.main.LOGGER") as logger:
            _verify_startup_firmware(flasher)

        logger.info.assert_called_once_with(
            "Verified running CUL868 firmware at startup: %s", "V 1.2 CUL868"
        )

    def test_logs_warning_and_keeps_startup_running_on_probe_failure(self) -> None:
        flasher = Mock()
        error = FlashError("serial device is unavailable")
        flasher.verify_running_application.side_effect = error

        with patch("app.main.LOGGER") as logger:
            _verify_startup_firmware(flasher)

        logger.warning.assert_called_once_with(
            "Unable to verify CUL868 firmware at startup: %s",
            error,
        )
