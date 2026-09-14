"""Narrow Home Assistant Supervisor lifecycle support for wmbusmeters."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_MAX_RESPONSE_BYTES = 256 * 1024
_REQUEST_TIMEOUT_SECONDS = 15
_STATE_TIMEOUT_SECONDS = 30
_RESTORE_ATTEMPTS = 3


class SupervisorError(RuntimeError):
    """A necessary Home Assistant Supervisor operation did not complete."""


@dataclass
class WmbusmetersPause:
    """The stopped instances and the safe lifecycle decision for one flash."""

    addons: tuple[str, ...]
    _restore_after_error: bool = True
    _leave_stopped_reason: str = "the CUL firmware did not verify"

    def leave_stopped_after_error(self, reason: str = "the CUL firmware did not verify") -> None:
        """Keep a reader stopped when reopening the CUL is not yet safe."""

        self._restore_after_error = False
        self._leave_stopped_reason = reason

    @property
    def restore_after_error(self) -> bool:
        return self._restore_after_error

    @property
    def leave_stopped_reason(self) -> str:
        """Return the operator-facing reason that a paused reader remains stopped."""

        return self._leave_stopped_reason


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

    def running_wmbusmeters_using_device(self, device: Path) -> tuple[str, ...]:
        """Return started wmbusmeters apps configured to access ``device``.

        The Supervisor intentionally exposes app options to a ``manager`` app.
        Read only the wmbusmeters device setting, never log or retain its
        options, which can include MQTT credentials. ``auto`` and ``cul`` are
        also treated as matches because those discovery modes can probe CUL
        serial devices even without naming this one by path.
        """

        document = self._request("GET", "/addons")
        data = _require_mapping(document.get("data"), "add-on list")
        addons = data.get("addons")
        if not isinstance(addons, list):
            raise SupervisorError("Supervisor returned an invalid add-on list")
        slugs: list[str] = []
        for addon in addons:
            if not isinstance(addon, dict):
                continue
            slug = addon.get("slug")
            if _is_wmbusmeters_slug(slug):
                info = self._addon_info(slug)
                if info.get("state") != "started":
                    continue
                if _wmbusmeters_uses_device(info.get("options"), device):
                    slugs.append(slug)
        return tuple(sorted(set(slugs)))

    def addon_state(self, slug: str) -> str:
        data = self._addon_info(slug)
        state = data.get("state")
        if not isinstance(state, str) or state not in {"started", "stopped"}:
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
            if last_state == expected:
                return
            sleep(0.5)
        raise SupervisorError(
            f"{slug} did not reach {expected} state before the timeout (last state: {last_state})"
        )

    def retarget_own_device_path(self, previous: Path, current: Path) -> bool:
        """Replace this add-on path only if it still names the exact old alias.

        The Supervisor options endpoint has no compare-and-set revision token,
        so this is a best-effort precondition check rather than an atomic
        transaction with a simultaneous Configuration UI edit.
        """

        _require_serial_by_id_path(previous)
        _require_serial_by_id_path(current)
        options = self._addon_options("self")
        if options.get("device") != str(previous):
            return False
        updated = dict(options)
        updated["device"] = str(current)
        self._set_addon_options("self", updated)
        return True

    def retarget_paused_wmbusmeters(
        self, pause: WmbusmetersPause, previous: Path, current: Path
    ) -> tuple[str, ...]:
        """Retarget only paused readers that still name the old direct by-id path.

        The options documents can contain credentials. They stay in memory only
        long enough to replace the one exact serial endpoint and are never logged.
        """

        _require_serial_by_id_path(previous)
        _require_serial_by_id_path(current)
        updated_slugs: list[str] = []
        for slug in pause.addons:
            options = self._addon_options(slug)
            updated = _retarget_wmbusmeters_options(options, previous, current)
            if updated is None:
                continue
            self._set_addon_options(slug, updated)
            updated_slugs.append(slug)
        return tuple(updated_slugs)

    @contextmanager
    def temporarily_stop_wmbusmeters(self, device: Path) -> Iterator[WmbusmetersPause]:
        """Pause matching active wmbusmeters instances and restore only those.

        This forms a transaction around raw serial/USB access. A failed flash
        before DFU begins restores a previously running meter reader. Once the
        CUL may be in DFU or have unverified firmware, the caller can retain
        the stopped state until the operator has repaired the radio.
        """

        stopped: list[str] = []
        try:
            for slug in self.running_wmbusmeters_using_device(device):
                self.stop_addon(slug)
                # A stop request can take effect just before a following poll
                # fails. Record it first so the error path still restores it.
                stopped.append(slug)
                self.wait_for_state(slug, "stopped")
        except BaseException as err:
            _add_restore_note(err, self._restore(stopped))
            raise

        pause = WmbusmetersPause(tuple(stopped))
        try:
            yield pause
        except BaseException as err:
            if pause.restore_after_error:
                _add_restore_note(err, self._restore(stopped))
            elif stopped:
                err.add_note(
                    "wmbusmeters remains stopped because " + pause.leave_stopped_reason
                )
            raise
        else:
            failures = self._restore(stopped)
            if failures:
                raise SupervisorError("could not restore wmbusmeters: " + "; ".join(failures))

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

    def _addon_options(self, slug: str) -> dict[str, Any]:
        return _require_mapping(self._addon_info(slug).get("options"), f"add-on {slug} options")

    def _set_addon_options(self, slug: str, options: dict[str, Any]) -> None:
        self._request("POST", f"/addons/{_addon_path(slug)}/options", {"options": options})

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data: bytes | None = None
        if payload is not None:
            try:
                data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError) as err:
                raise SupervisorError(f"Supervisor request {method} {path} has invalid JSON") from err
        request = Request(
            self._base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "cul868-flasher/0.1",
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


def _retarget_wmbusmeters_options(
    options: object, previous: Path, current: Path
) -> dict[str, Any] | None:
    """Copy options while replacing exact direct CUL endpoints, if any."""

    if not isinstance(options, dict):
        raise SupervisorError("wmbusmeters returned invalid options")
    configuration = options.get("conf")
    if isinstance(configuration, dict):
        section = configuration
        section_name = "conf"
    else:
        section = options
        section_name = None
    value = section.get("device")
    replacement = _retarget_wmbusmeters_device_value(value, previous, current)
    if replacement is None:
        return None
    updated = dict(options)
    updated_section = dict(section)
    updated_section["device"] = replacement
    if section_name is None:
        updated = updated_section
    else:
        updated[section_name] = updated_section
    return updated


def _retarget_wmbusmeters_device_value(
    value: object, previous: Path, current: Path
) -> str | None:
    if not isinstance(value, str) or len(value) > 4_096 or "\x00" in value:
        return None
    specifications = value.split(";")
    replacements = [
        _retarget_wmbusmeters_device_spec(specification, previous, current)
        for specification in specifications
    ]
    if replacements == specifications:
        return None
    return ";".join(replacements)


def _retarget_wmbusmeters_device_spec(specification: str, previous: Path, current: Path) -> str:
    """Replace only the endpoint portion, retaining aliases and mode suffixes."""

    alias, separator, value = specification.partition("=")
    prefix = f"{alias}{separator}" if separator else ""
    if not separator:
        value = specification
    endpoint, suffix_separator, suffix = value.partition(":")
    if endpoint.strip() != str(previous):
        return specification
    leading_length = len(endpoint) - len(endpoint.lstrip())
    trailing_length = len(endpoint) - len(endpoint.rstrip())
    leading = endpoint[:leading_length]
    trailing = endpoint[len(endpoint) - trailing_length :] if trailing_length else ""
    return f"{prefix}{leading}{current}{trailing}{suffix_separator}{suffix}"


def _quote_slug(slug: str) -> str:
    if not _is_wmbusmeters_slug(slug):
        raise SupervisorError("refusing to control an add-on other than wmbusmeters")
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
        error.add_note("wmbusmeters could not be restored: " + "; ".join(failures))
