"""Home Assistant Ingress UI for explicit, one-shot CUL868 firmware flashes."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from .flasher import Cul868Flasher, FlashError, FlashPreflight
from .hexfile import MAX_HEX_FILE_BYTES, HexFileError, HexImage, parse_hex_file, write_upload
from .operation import OperationBusyError, OperationController

LOGGER = logging.getLogger(__name__)
INGRESS_PORT = 8099
_TRUSTED_INGRESS_PROXY = "172.30.32.2"
_MAX_JSON_BYTES = 8 * 1024
_STAGED_ARTIFACT_TTL_SECONDS = 15 * 60
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class IngressError(RuntimeError):
    """An Ingress request is invalid without exposing internal implementation detail."""


@dataclass
class _StagedArtifact:
    image: HexImage
    preflight: FlashPreflight
    expires_at: float
    claimed: bool = False


class _ArtifactRegistry:
    """Own one short-lived private upload until a flash worker consumes it."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: dict[str, _StagedArtifact] = {}

    def stage(self, image: HexImage, preflight: FlashPreflight) -> str:
        with self._lock:
            self._remove_expired_locked()
            if any(entry.claimed for entry in self._entries.values()):
                raise IngressError("a validated firmware image is already being submitted")
            replaced = tuple(self._entries.values())
            self._entries.clear()
            identifier = secrets.token_urlsafe(24)
            self._entries[identifier] = _StagedArtifact(
                image=image,
                preflight=preflight,
                expires_at=monotonic() + _STAGED_ARTIFACT_TTL_SECONDS,
            )
        for entry in replaced:
            entry.image.path.unlink(missing_ok=True)
        return identifier

    def claim(self, identifier: object) -> _StagedArtifact:
        if not isinstance(identifier, str) or not 16 <= len(identifier) <= 128:
            raise IngressError("a validated firmware image is required")
        with self._lock:
            self._remove_expired_locked()
            entry = self._entries.get(identifier)
            if entry is None:
                raise IngressError("validated firmware image expired or was not found")
            if entry.claimed:
                raise IngressError("validated firmware image is already being flashed")
            entry.claimed = True
            return entry

    def release_claim(self, identifier: str) -> None:
        with self._lock:
            entry = self._entries.get(identifier)
            if entry is not None:
                entry.claimed = False

    def consume(self, identifier: str) -> None:
        with self._lock:
            self._entries.pop(identifier, None)

    def expire(self) -> None:
        with self._lock:
            self._remove_expired_locked()

    def close(self) -> None:
        with self._lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.image.path.unlink(missing_ok=True)

    def _remove_expired_locked(self) -> None:
        expired = [
            identifier
            for identifier, entry in self._entries.items()
            if not entry.claimed and entry.expires_at <= monotonic()
        ]
        for identifier in expired:
            entry = self._entries.pop(identifier)
            entry.image.path.unlink(missing_ok=True)


class IngressApi:
    """Authenticated-proxy API which queues, rather than performs, hardware work."""

    def __init__(
        self,
        controller: OperationController,
        flasher: Cul868Flasher,
        temporary_directory: Path = Path("/tmp"),
        stopping: Event | None = None,
    ) -> None:
        self._controller = controller
        self._flasher = flasher
        self._temporary_directory = temporary_directory
        self._artifacts = _ArtifactRegistry()
        # Serializes the two state transitions that span both the registry and
        # controller. Upload I/O and USB preflight stay outside this lock.
        self._submission_lock = Lock()
        self._accepting_mutations = True
        # Signal handlers only set this event. Request threads observe it while
        # holding the admission lock, avoiding lock acquisition in a handler.
        self._stopping = stopping if stopping is not None else Event()

    def status(self) -> dict[str, object]:
        self.expire()
        try:
            device = self._flasher.status()
        # Status must remain available when an unexpected device probe fails.
        except Exception as err:  # noqa: BLE001
            device = {
                "state": "unavailable",
                "message": f"Could not inspect CUL USB state: {str(err)[:384]}",
            }
        return {
            "device": device,
            "operation": self._controller.snapshot(),
            "scope": "CUL868 V3 manual firmware flashing only",
        }

    def operation(self) -> dict[str, object]:
        self.expire()
        return self._controller.snapshot()

    def validate_upload(self, stream: BinaryIO, content_length: int) -> dict[str, object]:
        with self._submission_lock:
            self._require_accepting_mutations_locked()
            self._require_idle()
        path = write_upload(stream, content_length, self._temporary_directory)
        try:
            image = parse_hex_file(path)
            preflight = self._flasher.preflight()
            with self._submission_lock:
                # A flash can have been submitted while the upload was parsed
                # or shutdown can have closed admission while it was running.
                self._require_accepting_mutations_locked()
                self._require_idle()
                artifact_id = self._artifacts.stage(image, preflight)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {
            "artifact_id": artifact_id,
            "firmware": {
                "sha256": image.sha256,
                "file_bytes": image.size,
                "application_bytes": image.data_bytes,
                "address_range": f"0x{image.lowest_address:04x}-0x{image.highest_address:04x}",
            },
            "preflight": preflight.as_dict(),
        }

    def flash(
        self,
        artifact_id: object,
        confirm: object,
        confirm_unpaired_recovery: object,
    ) -> dict[str, object]:
        if confirm is not True:
            raise IngressError("explicit confirmation is required before flashing")
        assert isinstance(artifact_id, str)
        with self._submission_lock:
            self._require_accepting_mutations_locked()
            artifact = self._artifacts.claim(artifact_id)
            try:
                if artifact.preflight.manual_recovery is not None:
                    if confirm_unpaired_recovery is not True:
                        raise IngressError(
                            "explicit confirmation of the unpaired CUL868 DFU bootloader is required"
                        )
                elif confirm_unpaired_recovery is not None and confirm_unpaired_recovery is not False:
                    raise IngressError("unpaired CUL868 DFU recovery was not selected for this upload")
                operation = self._controller.submit(
                    artifact_id,
                    artifact.image,
                    artifact.preflight.manual_recovery,
                )
            except Exception:
                self._artifacts.release_claim(artifact_id)
                raise
            self._artifacts.consume(artifact_id)
        return {"operation_id": operation.operation_id, "state": "queued"}

    def set_led(self, enabled: object) -> dict[str, object]:
        """Send one guarded CULFW LED command when no flash is pending.

        Keep admission locked while the flasher acquires its serial lock. This
        prevents a flash from being queued between the idle check and the LED
        command, while the flasher remains the single owner of USB access.
        """

        if not isinstance(enabled, bool):
            raise IngressError("LED state must be a boolean")
        with self._submission_lock:
            self._require_accepting_mutations_locked()
            self._require_idle()
            return self._flasher.set_led(enabled)

    @staticmethod
    def discard_image(image: HexImage) -> None:
        image.path.unlink(missing_ok=True)

    def close_admission(self) -> None:
        """Atomically reject new uploads and flash submissions during shutdown."""

        with self._submission_lock:
            self._accepting_mutations = False

    def close(self) -> None:
        self.close_admission()
        self._artifacts.close()

    def expire(self) -> None:
        self._artifacts.expire()

    def _require_idle(self) -> None:
        if self._controller.snapshot()["status"] in {"queued", "running"}:
            raise OperationBusyError("a CUL868 flash operation is already pending or active")

    def _require_accepting_mutations_locked(self) -> None:
        if self._stopping.is_set() or not self._accepting_mutations:
            raise IngressError("CUL868 Firmware Flasher is stopping and cannot accept a new flash")


class IngressServer:
    """Serve the tiny UI exclusively through Home Assistant's trusted proxy."""

    def __init__(
        self,
        api: IngressApi,
        host: str = "0.0.0.0",
        port: int = INGRESS_PORT,
        trusted_proxy_addresses: frozenset[str] = frozenset({_TRUSTED_INGRESS_PROXY}),
    ) -> None:
        self._api = api
        self._host = host
        self._port = port
        self._trusted_proxy_addresses = trusted_proxy_addresses
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._reaper_stop = Event()
        self._reaper_thread: Thread | None = None

    @property
    def port(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        return self._port

    def start(self) -> None:
        if self._server is not None:
            return
        server = ThreadingHTTPServer((self._host, self._port), self._handler_type())
        server.daemon_threads = True
        self._server = server
        self._thread = Thread(target=server.serve_forever, name="cul868-ingress", daemon=True)
        self._reaper_stop.clear()
        self._reaper_thread = Thread(
            target=self._reap_expired_artifacts,
            name="cul868-artifact-reaper",
            daemon=True,
        )
        self._thread.start()
        self._reaper_thread.start()
        LOGGER.info("Started Home Assistant Ingress server on port %s", self.port)

    def stop(self) -> None:
        # Close the registry/controller handoff before stopping the HTTP
        # listener. Handler threads that were still parsing a request recheck
        # this gate before staging or submitting any hardware operation.
        self._api.close_admission()
        server = self._server
        thread = self._thread
        reaper_thread = self._reaper_thread
        self._server = None
        self._thread = None
        self._reaper_thread = None
        self._reaper_stop.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if reaper_thread is not None:
            reaper_thread.join(timeout=5)

    def _reap_expired_artifacts(self) -> None:
        while not self._reaper_stop.wait(30):
            self._api.expire()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        api = self._api
        trusted_proxy_addresses = self._trusted_proxy_addresses
        static_directory = Path(__file__).with_name("web")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(30)

            def do_GET(self) -> None:
                if not self._trusted_proxy():
                    self._json_error(HTTPStatus.FORBIDDEN, "Ingress requests must come from Home Assistant")
                    return
                path = self._relative_path()
                if path == "/api/status":
                    self._json(HTTPStatus.OK, api.status())
                elif path == "/api/operation":
                    self._json(HTTPStatus.OK, api.operation())
                elif path in _STATIC_FILES:
                    self._static(path)
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "resource was not found")

            def do_POST(self) -> None:
                # A request body is read exactly once. Closing state-changing
                # requests prevents bytes beyond Content-Length being reused
                # as a second HTTP/1.1 request on this connection.
                self.close_connection = True
                if not self._trusted_proxy():
                    self._json_error(HTTPStatus.FORBIDDEN, "Ingress requests must come from Home Assistant")
                    return
                if self.headers.get("X-Requested-With") != "XMLHttpRequest":
                    self._json_error(HTTPStatus.FORBIDDEN, "missing Ingress request header")
                    return
                try:
                    path = self._relative_path()
                    if path == "/api/validate-upload":
                        if self.headers.get_content_type() != "application/octet-stream":
                            raise IngressError("firmware upload must use application/octet-stream")
                        response = api.validate_upload(
                            self.rfile, self._content_length(MAX_HEX_FILE_BYTES)
                        )
                    elif path == "/api/flash":
                        body = self._json_body()
                        response = api.flash(
                            body.get("artifact_id"),
                            body.get("confirm"),
                            body.get("confirm_unpaired_recovery"),
                        )
                    elif path == "/api/led":
                        body = self._json_body()
                        response = api.set_led(body.get("enabled"))
                    else:
                        self._json_error(HTTPStatus.NOT_FOUND, "resource was not found")
                        return
                except OperationBusyError as err:
                    self._json_error(HTTPStatus.CONFLICT, str(err))
                    return
                except (IngressError, HexFileError, FlashError) as err:
                    self._json_error(HTTPStatus.BAD_REQUEST, str(err))
                    return
                self._json(HTTPStatus.OK, response)

            def _json_body(self) -> dict[str, object]:
                size = self._content_length(_MAX_JSON_BYTES)
                try:
                    value = json.loads(self.rfile.read(size).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    raise IngressError("request body must be a JSON object") from err
                if not isinstance(value, dict):
                    raise IngressError("request body must be a JSON object")
                return value

            def _content_length(self, maximum: int) -> int:
                header = self.headers.get("Content-Length")
                if header is None or not header.isascii() or not header.isdecimal():
                    raise IngressError("request requires a valid Content-Length")
                size = int(header)
                if not 1 <= size <= maximum:
                    raise IngressError(f"request body must be between 1 and {maximum} bytes")
                return size

            def _relative_path(self) -> str:
                path = urlsplit(self.path).path
                ingress_path = self.headers.get("X-Ingress-Path", "").rstrip("/")
                if ingress_path and (path == ingress_path or path.startswith(f"{ingress_path}/")):
                    path = path[len(ingress_path) :] or "/"
                return path

            def _trusted_proxy(self) -> bool:
                return self.client_address[0] in trusted_proxy_addresses

            def _static(self, path: str) -> None:
                filename, content_type = _STATIC_FILES[path]
                try:
                    content = (static_directory / filename).read_bytes()
                except OSError:
                    self._json_error(HTTPStatus.NOT_FOUND, "resource was not found")
                    return
                self._bytes(HTTPStatus.OK, content_type, content)

            def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
                self._bytes(
                    status,
                    "application/json; charset=utf-8",
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"),
                )

            def _json_error(self, status: HTTPStatus, message: str) -> None:
                # Rejections can happen before a request body is read. Closing
                # the HTTP/1.1 connection prevents it becoming a second request.
                self.close_connection = True
                self._json(status, {"error": str(message)[:512]})

            def _bytes(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if self.close_connection:
                    self.send_header("Connection", "close")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                    "form-action 'self'; img-src 'self'; script-src 'self'; style-src 'self'",
                )
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args
                LOGGER.debug("Ingress HTTP request completed")

        return Handler
