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
    stopping = Event()
    api = IngressApi(controller, flasher, stopping=stopping)
    server = IngressServer(api)

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
            operation_id = operation.operation_id
            if not controller.claim_for_run(operation_id, stopping):
                api.discard_image(operation.image)
                break
            try:
                result = flasher.flash(
                    operation.image,
                    lambda percent, message, operation_id=operation_id: controller.report_progress(
                        operation_id, percent, message
                    ),
                    manual_recovery=operation.manual_recovery,
                )
            except Exception as err:
                LOGGER.exception("CUL868 flash operation failed")
                controller.fail(operation_id, err)
            else:
                installed = result.get("installed_version")
                LOGGER.info("Verified CUL868 firmware: %s", installed)
                retained = result.get("stopped_cul_addons")
                if isinstance(retained, list) and all(isinstance(slug, str) for slug in retained):
                    retained_message = (
                        "; CUL apps remain stopped for a manual serial-path review: "
                        + ", ".join(retained)
                    )
                else:
                    retained_message = ""
                migration = result.get("manual_serial_path_migration")
                if isinstance(migration, dict):
                    current = migration.get("current")
                    endpoint = migration.get("application_endpoint")
                    if isinstance(current, str):
                        migration_message = (
                            "; CUL serial-by-id name changed to "
                            f"{current}; update this add-on and paused CUL app settings manually"
                        )
                    elif isinstance(endpoint, str):
                        migration_message = (
                            "; CUL serial-by-id name needs manual resolution; the verified endpoint is "
                            f"{endpoint}"
                        )
                    else:
                        migration_message = "; CUL serial-by-id name needs manual resolution"
                else:
                    migration_message = ""
                controller.complete(
                    operation_id,
                    (
                        f"CUL868 firmware verified: {installed or 'version unavailable'}"
                        f"{migration_message}{retained_message}"
                    ),
                )
            finally:
                api.discard_image(operation.image)
    finally:
        api.close_admission()
        server.stop()
        for pending in controller.drain():
            api.discard_image(pending.image)
        api.close()


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
