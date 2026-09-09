"""Long-running Home Assistant add-on process for manual CUL868 flashes."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from signal import SIGINT, SIGTERM, signal
from threading import Event

from .flasher import Cul868Flasher, FlashError
from .ingress import IngressApi, IngressServer
from .models import Settings, ValidationError
from .operation import OperationController


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
_MAX_OPTIONS_BYTES = 64 * 1024


def _load_options() -> dict[str, object]:
    path = Path(os.environ.get("CUL868_FLASHER_OPTIONS", "/data/options.json"))
    try:
        if path.stat().st_size > _MAX_OPTIONS_BYTES:
            raise ValidationError(f"app options exceed {_MAX_OPTIONS_BYTES} bytes")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValidationError(f"cannot load app options: {err}") from err
    if not isinstance(document, dict):
        raise ValidationError("app options must be a JSON object")
    return document


def run() -> None:
    """Run the Ingress server and consume at most one flash operation at a time."""

    settings = Settings.from_mapping(_load_options())
    controller = OperationController()
    flasher = Cul868Flasher(settings)
    api = IngressApi(controller, flasher)
    server = IngressServer(api)
    stopping = Event()

    def request_stop(signum: int, _frame: object) -> None:
        if not stopping.is_set():
            LOGGER.info("Received signal %s; stopping after the current operation", signum)
            stopping.set()

    signal(SIGTERM, request_stop)
    signal(SIGINT, request_stop)
    server.start()
    try:
        _verify_startup_firmware(flasher)
        while not stopping.is_set():
            operation = controller.get(timeout=0.5)
            if operation is None:
                continue
            controller.mark_running(operation.operation_id)
            try:
                result = flasher.flash(
                    operation.image,
                    lambda percent, message: controller.report_progress(
                        operation.operation_id, percent, message
                    ),
                )
            except Exception as err:
                LOGGER.exception("CUL868 flash operation failed")
                controller.fail(operation.operation_id, err)
            else:
                installed = result.get("installed_version")
                LOGGER.info("Verified CUL868 firmware: %s", installed)
                controller.complete(
                    operation.operation_id,
                    f"CUL868 firmware verified: {installed or 'version unavailable'}",
                )
            finally:
                api.discard_image(operation.image)
    finally:
        for pending in controller.drain():
            api.discard_image(pending.image)
        server.stop()


def _verify_startup_firmware(flasher: Cul868Flasher) -> None:
    """Record a current CUL version without making an unavailable device fatal."""

    try:
        version = flasher.verify_running_application()
    except FlashError as err:
        LOGGER.warning("Unable to verify CUL868 firmware at startup: %s", err)
    else:
        LOGGER.info("Verified running CUL868 firmware at startup: %s", version)


def main() -> None:
    try:
        run()
    except ValidationError as err:
        LOGGER.critical("Invalid app configuration: %s", err)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
