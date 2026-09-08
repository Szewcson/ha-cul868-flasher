from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.hexfile import parse_hex_file
from app.operation import OperationBusyError, OperationController

from .helpers import minimal_hex


class OperationTests(unittest.TestCase):
    def test_queue_serializes_one_flash_and_preserves_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.hex"
            path.write_bytes(minimal_hex())
            image = parse_hex_file(path)
            controller = OperationController()
            operation = controller.submit("artifact", image)
            with self.assertRaises(OperationBusyError):
                controller.submit("another", image)
            queued = controller.get(timeout=0)
            self.assertEqual(queued, operation)
            controller.mark_running(operation.operation_id)
            controller.report_progress(operation.operation_id, 40, "Writing")
            controller.report_progress(operation.operation_id, 30, "Older update")
            controller.complete(operation.operation_id, "Verified")

        snapshot = controller.snapshot()
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["progress"], 100)
        self.assertEqual(snapshot["message"], "Verified")

    def test_failure_includes_exception_notes_and_drain_returns_pending_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.hex"
            path.write_bytes(minimal_hex())
            image = parse_hex_file(path)
            controller = OperationController()
            operation = controller.submit("artifact", image)
            pending = controller.drain()
            self.assertEqual(pending, (operation,))

            replacement = controller.submit("replacement", image)
            controller.mark_running(replacement.operation_id)
            error = RuntimeError("firmware transfer failed")
            error.add_note("wmbusmeters could not be restored: wmbusmeters: start failed")
            controller.fail(replacement.operation_id, error)

        self.assertIn("wmbusmeters could not be restored", controller.snapshot()["error"])
