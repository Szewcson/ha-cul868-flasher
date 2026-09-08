"""Narrow Home Assistant Supervisor lifecycle support for wmbusmeters."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
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


class SupervisorClient:
    """Use only the add-on lifecycle endpoints required for a safe flash."""

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

    @contextmanager
    def temporarily_stop_wmbusmeters(self, device: Path) -> Iterator[tuple[str, ...]]:
        """Pause matching active wmbusmeters instances and restore only those.

        This forms a transaction around raw serial/USB access. A failed flash
        must not leave a previously running meter reader stopped, while an
        originally stopped reader must remain stopped after the operation.
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

        try:
            yield tuple(stopped)
        except BaseException as err:
            _add_restore_note(err, self._restore(stopped))
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
        document = self._request("GET", f"/addons/{_quote_slug(slug)}/info")
        return _require_mapping(document.get("data"), f"add-on {slug}")

    def _request(self, method: str, path: str) -> dict[str, Any]:
        request = Request(
            self._base_url + path,
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


def _quote_slug(slug: str) -> str:
    if not _is_wmbusmeters_slug(slug):
        raise SupervisorError("refusing to control an add-on other than wmbusmeters")
    return quote(slug, safe="_-")


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisorError(f"Supervisor returned invalid {label} data")
    return value


def _add_restore_note(error: BaseException, failures: list[str]) -> None:
    if failures:
        error.add_note("wmbusmeters could not be restored: " + "; ".join(failures))
