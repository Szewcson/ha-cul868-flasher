"""A serialized, observable flash-operation queue for the Ingress server."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Full, Queue
from secrets import token_hex
from threading import Lock
from time import time

from .hexfile import HexImage
from .usb import ManualRecoveryTarget

_MAX_EVENTS = 32


class OperationBusyError(RuntimeError):
    """A flash is queued or running already."""


@dataclass(frozen=True)
class FlashOperation:
    """One validated staged upload assigned to a single flash worker."""

    operation_id: str
    artifact_id: str
    image: HexImage
    manual_recovery: ManualRecoveryTarget | None = None


class OperationController:
    """Keep untrusted HTTP clients from initiating concurrent hardware access."""

    def __init__(self) -> None:
        self._queue: Queue[FlashOperation] = Queue(maxsize=1)
        self._lock = Lock()
        self._status = "idle"
        self._operation_id: str | None = None
        self._progress = 0
        self._message = "No flash operation is running."
        self._error: str | None = None
        self._events: list[dict[str, object]] = []

    def submit(
        self,
        artifact_id: str,
        image: HexImage,
        manual_recovery: ManualRecoveryTarget | None = None,
    ) -> FlashOperation:
        if not artifact_id or len(artifact_id) > 128:
            raise ValueError("artifact ID is invalid")
        operation = FlashOperation(token_hex(16), artifact_id, image, manual_recovery)
        with self._lock:
            if self._status in {"queued", "running"}:
                raise OperationBusyError("another CUL868 flash operation is already in progress")
            try:
                self._queue.put_nowait(operation)
            except Full as err:
                raise OperationBusyError("another CUL868 flash operation is already queued") from err
            self._status = "queued"
            self._operation_id = operation.operation_id
            self._progress = 0
            self._message = "Firmware is queued for flashing."
            self._error = None
            self._append_event("queued", self._message)
        return operation

    def get(self, timeout: float = 1.0) -> FlashOperation | None:
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def drain(self) -> tuple[FlashOperation, ...]:
        """Return unstarted work so shutdown can delete its private upload."""

        pending: list[FlashOperation] = []
        with self._lock:
            while True:
                try:
                    pending.append(self._queue.get_nowait())
                except Empty:
                    break
            if pending and self._status == "queued":
                self._status = "idle"
                self._operation_id = None
                self._progress = 0
                self._message = "No flash operation is running."
                self._error = None
                self._append_event("discarded", "Queued firmware was discarded during shutdown.")
        return tuple(pending)

    def mark_running(self, operation_id: str) -> None:
        with self._lock:
            self._require_current(operation_id)
            self._status = "running"
            self._progress = max(self._progress, 1)
            self._message = "Preparing the CUL868 flash operation."
            self._append_event("running", self._message)

    def report_progress(self, operation_id: str, percent: int, message: str) -> None:
        if not isinstance(percent, int) or not 0 <= percent <= 100:
            raise ValueError("progress percentage is invalid")
        safe_message = _safe_message(message)
        with self._lock:
            self._require_current(operation_id)
            if self._status != "running":
                return
            self._progress = max(self._progress, percent)
            self._message = safe_message
            self._append_event("progress", safe_message)

    def complete(self, operation_id: str, message: str) -> None:
        with self._lock:
            self._require_current(operation_id)
            self._status = "succeeded"
            self._progress = 100
            self._message = _safe_message(message)
            self._error = None
            self._append_event("succeeded", self._message)

    def fail(self, operation_id: str, error: BaseException) -> None:
        notes = getattr(error, "__notes__", ())
        note_text = "\n".join(note for note in notes if isinstance(note, str))
        message = _safe_message("\n".join(part for part in (str(error), note_text) if part))
        with self._lock:
            self._require_current(operation_id)
            self._status = "failed"
            self._message = "The flash operation failed."
            self._error = message
            self._append_event("failed", message)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "status": self._status,
                "operation_id": self._operation_id,
                "progress": self._progress,
                "message": self._message,
                "error": self._error,
                "events": [event.copy() for event in self._events],
            }

    def _require_current(self, operation_id: str) -> None:
        if operation_id != self._operation_id:
            raise ValueError("flash operation is no longer current")

    def _append_event(self, kind: str, message: str) -> None:
        self._events.append({"at": int(time()), "kind": kind, "message": message})
        del self._events[:-_MAX_EVENTS]


def _safe_message(value: object) -> str:
    if not isinstance(value, str):
        return "Unexpected flash operation status."
    value = " ".join(value.split())
    if not value:
        return "Unexpected flash operation status."
    return value[:512]
