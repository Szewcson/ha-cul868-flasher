"""Narrow Home Assistant Supervisor lifecycle support for CUL consumers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import __version__

_MAX_RESPONSE_BYTES = 256 * 1024
_REQUEST_TIMEOUT_SECONDS = 15
_STATE_TIMEOUT_SECONDS = 30
_RESTORE_ATTEMPTS = 3
_ADDON_STATES = frozenset({"started", "stopped", "error"})
_QUIESCENT_ADDON_STATES = frozenset({"stopped", "error"})


class SupervisorError(RuntimeError):
    """A necessary Home Assistant Supervisor operation did not complete."""


@dataclass
class CulConsumerPause:
    """Stopped known CUL consumers and the safe lifecycle decision for one flash."""

    wmbusmeters_addons: tuple[str, ...] = ()
    max2mqtt_addons: tuple[str, ...] = ()
    additional_addons: tuple[str, ...] = ()
    _restore_after_error: bool = True
    _leave_stopped_reason: str = "the CUL firmware did not verify"
    _retained_addons: set[str] = field(default_factory=set)

    @property
    def addons(self) -> tuple[str, ...]:
        """Return all consumers in the deterministic stop/restore order."""

        return tuple(
            sorted(set(self.wmbusmeters_addons + self.max2mqtt_addons + self.additional_addons))
        )

    def leave_stopped_after_error(self, reason: str = "the CUL firmware did not verify") -> None:
        """Keep every paused consumer stopped when reopening the CUL is unsafe."""

        self._restore_after_error = False
        self._leave_stopped_reason = reason

    @property
    def restore_after_error(self) -> bool:
        return self._restore_after_error

    @property
    def leave_stopped_reason(self) -> str:
        """Return the operator-facing reason that paused consumers remain stopped."""

        return self._leave_stopped_reason

    def retain_addons(self, addons: tuple[str, ...]) -> None:
        """Keep named paused consumers stopped after an otherwise successful flash.

        Firmware can change a serial-by-id alias. The Supervisor option API
        does not offer an atomic compare-and-set operation, so no consumer
        configuration is rewritten automatically. Retaining matching apps
        prevents them from reopening a stale endpoint before operator review.
        """

        self._retained_addons.update(set(addons).intersection(self.addons))

    @property
    def addons_to_restore(self) -> tuple[str, ...]:
        """Return paused consumers that are safe to restart."""

        return tuple(slug for slug in self.addons if slug not in self._retained_addons)

    @property
    def retained_addons(self) -> tuple[str, ...]:
        """Return paused consumers deliberately left stopped after success."""

        return tuple(sorted(self._retained_addons))
class SupervisorClient:
    """Use the narrowly scoped Supervisor endpoints required for a safe flash."""

    def __init__(self, base_url: str, token: str) -> None:
        if not base_url.startswith("http://") or len(base_url) > 256:
            raise SupervisorError("Supervisor URL is not a supported HTTP endpoint")
        if not token or len(token) > 16 * 1024:
            raise SupervisorError("Supervisor token is unavailable")
        self._base_url = base_url.rstrip("/")
        self._token = token

    @classmethod
    def from_environment(cls) -> SupervisorClient:
        return cls(
            os.environ.get("SUPERVISOR_URL", "http://supervisor"),
            os.environ.get("SUPERVISOR_TOKEN", ""),
        )

    def running_cul_consumers_using_device(
        self, device: Path, additional_cul_addons: tuple[str, ...]
    ) -> CulConsumerPause:
        """Return started, safely identifiable consumers of ``device``.

        ``wmbusmeters`` and ``max2mqtt`` are recognized only through their
        documented serial options. Additional app slugs are an explicit
        operator opt-in for services such as FHEM or Homegear, whose CUL path
        is normally stored in private service files rather than Supervisor
        options. Only their state is read; their options are not inspected.
        """

        document = self._request("GET", "/addons")
        data = _require_mapping(document.get("data"), "add-on list")
        addons = data.get("addons")
        if not isinstance(addons, list):
            raise SupervisorError("Supervisor returned an invalid add-on list")
        additional = {slug for slug in additional_cul_addons if _is_addon_slug(slug)}
        wmbusmeters: list[str] = []
        max2mqtt: list[str] = []
        opted_in: list[str] = []
        for addon in addons:
            if not isinstance(addon, dict):
                continue
            slug = addon.get("slug")
            if _is_wmbusmeters_slug(slug):
                info = self._addon_info(slug)
                if info.get("state") == "started" and _wmbusmeters_uses_device(
                    info.get("options"), device
                ):
                    wmbusmeters.append(slug)
            elif _is_max2mqtt_slug(slug):
                info = self._addon_info(slug)
                if info.get("state") == "started" and _max2mqtt_uses_device(
                    info.get("options"), device
                ):
                    max2mqtt.append(slug)
            elif isinstance(slug, str) and slug in additional and addon.get("state") == "started":
                opted_in.append(slug)
        return CulConsumerPause(
            wmbusmeters_addons=tuple(sorted(set(wmbusmeters))),
            max2mqtt_addons=tuple(sorted(set(max2mqtt))),
            additional_addons=tuple(sorted(set(opted_in))),
        )

    def addon_state(self, slug: str) -> str:
        data = self._addon_info(slug)
        state = data.get("state")
        if not isinstance(state, str) or state not in _ADDON_STATES:
            raise SupervisorError(f"Supervisor returned an invalid state for {slug}")
        return state

    def hardware_serial_by_id_paths_for_tty(self, device: Path) -> tuple[Path, ...]:
        """Return canonical by-id aliases for one current serial endpoint.

        The local ``/dev/serial/by-id`` mount can lag behind host udev after a
        USB personality change. The Supervisor hardware inventory is the
        authoritative Home Assistant view and relates its ``by_id`` value to
        the exact ``dev_path``. Accept only direct by-id paths, never an
        arbitrary path supplied by the API response.
        """

        _require_tty_path(device)
        document = self._request("GET", "/hardware/info")
        data = _require_mapping(document.get("data"), "hardware")
        devices = data.get("devices")
        if not isinstance(devices, list):
            raise SupervisorError("Supervisor returned invalid hardware devices data")

        aliases: set[Path] = set()
        for entry in devices:
            if not isinstance(entry, dict) or entry.get("dev_path") != str(device):
                continue
            alias = _serial_by_id_path(entry.get("by_id"))
            if alias is not None:
                aliases.add(alias)
        return tuple(sorted(aliases, key=str))

    def stop_addon(self, slug: str) -> None:
        self._request("POST", f"/addons/{_quote_slug(slug)}/stop")

    def start_addon(self, slug: str) -> None:
        self._request("POST", f"/addons/{_quote_slug(slug)}/start")

    def wait_for_state(self, slug: str, expected: str) -> None:
        deadline = monotonic() + _STATE_TIMEOUT_SECONDS
        last_state = "unknown"
        while monotonic() < deadline:
            last_state = self.addon_state(slug)
            if last_state == expected or (
                expected == "stopped" and last_state in _QUIESCENT_ADDON_STATES
            ):
                # Supervisor can report ``error`` after an explicit SIGTERM
                # even though the container has exited. It is safe to proceed
                # only because this context manager issued the stop request.
                return
            sleep(0.5)
        raise SupervisorError(
            f"{slug} did not reach {expected} state before the timeout (last state: {last_state})"
        )

    @contextmanager
    def temporarily_stop_cul_consumers(
        self, device: Path, additional_cul_addons: tuple[str, ...]
    ) -> Iterator[CulConsumerPause]:
        """Pause matching CUL consumers and restore only those.

        This forms a transaction around raw serial/USB access. A failed flash
        before DFU begins restores a previously running consumer. Once the CUL
        may be in DFU or have unverified firmware, the caller can retain the
        stopped state until the operator has repaired the radio.
        """

        matches = self.running_cul_consumers_using_device(device, additional_cul_addons)
        stopped: list[str] = []
        try:
            for slug in matches.addons:
                self.stop_addon(slug)
                # A stop request can take effect just before a following poll
                # fails. Record it first so the error path still restores it.
                stopped.append(slug)
                self.wait_for_state(slug, "stopped")
        except BaseException as err:
            _add_restore_note(err, self._restore(stopped))
            raise

        stopped_set = set(stopped)
        pause = CulConsumerPause(
            wmbusmeters_addons=tuple(
                slug for slug in matches.wmbusmeters_addons if slug in stopped_set
            ),
            max2mqtt_addons=tuple(slug for slug in matches.max2mqtt_addons if slug in stopped_set),
            additional_addons=tuple(slug for slug in matches.additional_addons if slug in stopped_set),
        )
        try:
            yield pause
        except BaseException as err:
            if pause.restore_after_error:
                _add_restore_note(err, self._restore(list(pause.addons_to_restore)))
            elif stopped:
                err.add_note("CUL consumer add-ons remain stopped because " + pause.leave_stopped_reason)
            raise
        else:
            failures = self._restore(list(pause.addons_to_restore))
            if failures:
                raise SupervisorError("could not restore CUL consumer add-ons: " + "; ".join(failures))

    def _restore(self, slugs: list[str]) -> list[str]:
        failures: list[str] = []
        for slug in reversed(slugs):
            restored = False
            last_error = "unknown error"
            for attempt in range(1, _RESTORE_ATTEMPTS + 1):
                try:
                    # A start request can succeed while the response/poll is
                    # lost. Check first to avoid turning a restored app into a
                    # misleading second-start failure.
                    if self.addon_state(slug) != "started":
                        self.start_addon(slug)
                    self.wait_for_state(slug, "started")
                    restored = True
                    break
                except SupervisorError as err:
                    last_error = str(err)
                    if attempt != _RESTORE_ATTEMPTS:
                        sleep(float(attempt))
            if not restored:
                failures.append(f"{slug}: {last_error}")
        return failures

    def _addon_info(self, slug: str) -> dict[str, Any]:
        document = self._request("GET", f"/addons/{_addon_path(slug)}/info")
        return _require_mapping(document.get("data"), f"add-on {slug}")

    def _request(self, method: str, path: str) -> dict[str, Any]:
        request = Request(
            self._base_url + path,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": f"cul868-flasher/{__version__}",
            },
        )
        try:
            with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as err:
            detail = err.read(1024).decode("utf-8", errors="replace").replace("\n", " ")
            raise SupervisorError(
                f"Supervisor request {method} {path} failed: HTTP {err.code}: {detail[:512]}"
            ) from err
        except URLError as err:
            raise SupervisorError(f"Supervisor request {method} {path} failed: {err.reason}") from err
        except OSError as err:
            raise SupervisorError(f"Supervisor request {method} {path} failed: {err}") from err
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise SupervisorError(f"Supervisor response to {method} {path} was too large")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise SupervisorError(f"Supervisor returned invalid JSON for {method} {path}") from err
        if not isinstance(document, dict):
            raise SupervisorError(f"Supervisor returned an invalid response for {method} {path}")
        if document.get("result") != "ok":
            raise SupervisorError(f"Supervisor rejected {method} {path}")
        return document


def _is_wmbusmeters_slug(value: object) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return lowered in {
        "wmbusmeters",
        "wmbusmeters-ha-addon",
        "wmbusmeters-ha-addon-edge",
    } or lowered.endswith(("_wmbusmeters", "_wmbusmeters-ha-addon", "_wmbusmeters-ha-addon-edge"))


def _is_max2mqtt_slug(value: object) -> bool:
    """Recognize the maintained MAX! to MQTT Bridge add-on slug.

    Home Assistant prefixes third-party app slugs with a repository hash, so
    accept the documented bare slug and the Supervisor-generated suffix form.
    """

    return isinstance(value, str) and (
        value.lower() == "max2mqtt" or value.lower().endswith("_max2mqtt")
    )


def _is_addon_slug(value: object) -> bool:
    """Validate a Supervisor app slug before it can be used for lifecycle calls."""

    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value == "self":
        return False
    return all(character in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)


def _wmbusmeters_uses_device(options: object, selected_device: Path) -> bool:
    """Identify only direct CUL paths and documented serial discovery modes."""

    if not isinstance(options, dict):
        return False
    configuration = options.get("conf")
    if isinstance(configuration, dict):
        device_value = configuration.get("device")
    else:
        # The historical community add-on exposed the setting at the root.
        device_value = options.get("device")
    if not isinstance(device_value, str) or len(device_value) > 4_096:
        return False
    return any(
        _wmbusmeters_device_spec_uses_device(specification, selected_device)
        for specification in device_value.split(";")
    )


def _wmbusmeters_device_spec_uses_device(specification: str, selected_device: Path) -> bool:
    """Compare a wmbusmeters ``device=`` value without evaluating it."""

    value = specification.strip()
    if not value:
        return False
    # wmbusmeters permits a bus alias such as MAIN=/dev/ttyUSB0:mbus:2400.
    if "=" in value:
        _alias, value = value.split("=", maxsplit=1)
        value = value.strip()
    endpoint = value.split(":", maxsplit=1)[0].strip()
    if endpoint.lower() in {"auto", "cul"}:
        return True
    return _same_device_path(endpoint, selected_device)


def _max2mqtt_uses_device(options: object, selected_device: Path) -> bool:
    """Match MAX! to MQTT Bridge's documented direct ``serial_port`` option."""

    if not isinstance(options, dict):
        return False
    serial_port = options.get("serial_port")
    return isinstance(serial_port, str) and _same_device_path(serial_port, selected_device)


def _same_device_path(candidate: str, selected_device: Path) -> bool:
    """Match either the configured spelling or the same resolved /dev node."""

    if not candidate or len(candidate) > 512 or "\x00" in candidate:
        return False
    path = PurePosixPath(candidate)
    if not path.is_absolute() or path.parts[:2] != ("/", "dev") or ".." in path.parts:
        return False
    candidate_path = Path(path)
    if candidate_path == selected_device:
        return True
    try:
        return candidate_path.resolve(strict=True) == selected_device.resolve(strict=True)
    except OSError:
        return False


def _quote_slug(slug: str) -> str:
    if not _is_addon_slug(slug):
        raise SupervisorError("refusing to control an add-on with an invalid slug")
    return quote(slug, safe="_-")


def _addon_path(slug: str) -> str:
    return "self" if slug == "self" else _quote_slug(slug)


def _require_serial_by_id_path(value: Path) -> None:
    if value.parent != Path("/dev/serial/by-id") or value.name in {"", ".", ".."}:
        raise SupervisorError("refusing to migrate a device path outside /dev/serial/by-id")


def _require_tty_path(value: Path) -> None:
    candidate = PurePosixPath(value)
    if (
        not candidate.is_absolute()
        or candidate.parent != PurePosixPath("/dev")
        or not candidate.name.startswith(("ttyACM", "ttyUSB"))
        or ".." in candidate.parts
    ):
        raise SupervisorError("refusing to inspect a serial path outside /dev/ttyACM* or /dev/ttyUSB*")


def _serial_by_id_path(value: object) -> Path | None:
    """Parse one direct, bounded by-id path from Supervisor hardware data."""

    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        return None
    path = Path(value)
    try:
        _require_serial_by_id_path(path)
    except SupervisorError:
        return None
    return path


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisorError(f"Supervisor returned invalid {label} data")
    return value


def _add_restore_note(error: BaseException, failures: list[str]) -> None:
    if failures:
        error.add_note("CUL consumer add-ons could not be restored: " + "; ".join(failures))
