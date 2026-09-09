from __future__ import annotations

import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path

from app.flasher import FlashPreflight
from app.ingress import IngressApi, IngressError, IngressServer
from app.operation import OperationController
from app.usb import ManualRecoveryTarget

from .helpers import minimal_hex


class _IngressFlasher:
    def __init__(self, preflight: FlashPreflight | None = None) -> None:
        self._preflight = preflight or FlashPreflight("application", "2-3", "Ready to verify")

    def status(self) -> dict[str, object]:
        return {"state": "application", "topology": "2-3", "message": "Ready"}

    def preflight(self) -> FlashPreflight:
        return self._preflight


class IngressApiTests(unittest.TestCase):
    def test_upload_is_staged_then_requires_explicit_confirmation(self) -> None:
        controller = OperationController()
        with tempfile.TemporaryDirectory() as directory:
            api = IngressApi(controller, _IngressFlasher(), Path(directory))  # type: ignore[arg-type]
            response = api.validate_upload(io.BytesIO(minimal_hex()), len(minimal_hex()))
            artifact_id = response["artifact_id"]
            self.assertIsInstance(artifact_id, str)
            with self.assertRaisesRegex(IngressError, "explicit confirmation"):
                api.flash(artifact_id, False, None)
            queued = api.flash(artifact_id, True, None)
            operation = controller.get(timeout=0)
            self.assertEqual(queued["operation_id"], operation.operation_id if operation else None)
            assert operation is not None
            api.discard_image(operation.image)

    def test_unpaired_recovery_requires_a_second_confirmation(self) -> None:
        controller = OperationController()
        preflight = FlashPreflight(
            "manual-recovery",
            "2-3",
            "Explicit recovery confirmation is required.",
            ManualRecoveryTarget("2-3", "CUL-TEST"),
        )
        with tempfile.TemporaryDirectory() as directory:
            api = IngressApi(controller, _IngressFlasher(preflight), Path(directory))  # type: ignore[arg-type]
            response = api.validate_upload(io.BytesIO(minimal_hex()), len(minimal_hex()))
            artifact_id = response["artifact_id"]

            self.assertTrue(response["preflight"]["requires_unpaired_recovery_confirmation"])
            with self.assertRaisesRegex(IngressError, "unpaired CUL868 DFU bootloader"):
                api.flash(artifact_id, True, None)
            queued = api.flash(artifact_id, True, True)
            operation = controller.get(timeout=0)

            self.assertEqual(queued["operation_id"], operation.operation_id if operation else None)
            assert operation is not None
            self.assertEqual(operation.manual_recovery, ManualRecoveryTarget("2-3", "CUL-TEST"))
            api.discard_image(operation.image)

    def test_invalid_upload_is_not_left_in_temporary_directory(self) -> None:
        controller = OperationController()
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            api = IngressApi(controller, _IngressFlasher(), temporary_directory)  # type: ignore[arg-type]
            with self.assertRaisesRegex(Exception, "Intel HEX"):
                api.validate_upload(io.BytesIO(b"not a firmware"), len(b"not a firmware"))
            self.assertEqual(list(temporary_directory.iterdir()), [])


class IngressServerTests(unittest.TestCase):
    def test_mutating_requests_require_ingress_header(self) -> None:
        controller = OperationController()
        api = IngressApi(controller, _IngressFlasher())  # type: ignore[arg-type]
        server = IngressServer(
            api,
            host="127.0.0.1",
            port=0,
            trusted_proxy_addresses=frozenset({"127.0.0.1"}),
        )
        try:
            server.start()
        except PermissionError:
            # Some CI sandboxes prohibit all AF_INET binds. The same test runs
            # normally where a loopback listener is permitted.
            api.close()
            self.skipTest("this sandbox does not permit loopback listeners")
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            body = json.dumps({"artifact_id": "x", "confirm": True}).encode()
            connection.request("POST", "/api/flash", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 403)
            self.assertIn("Ingress request header", payload["error"])
        finally:
            server.stop()
