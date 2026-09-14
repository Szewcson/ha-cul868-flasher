from __future__ import annotations

import unittest
from pathlib import Path

from app.supervisor import (
    SupervisorClient,
    SupervisorError,
    WmbusmetersPause,
    _wmbusmeters_uses_device,
)


class _LifecycleSupervisor(SupervisorClient):
    def __init__(self, *, initially_started: bool = True, fail_stop_wait_once: bool = False) -> None:
        self.started = initially_started
        self.fail_stop_wait_once = fail_stop_wait_once
        self.events: list[str] = []

    def running_wmbusmeters_using_device(self, _device: Path) -> tuple[str, ...]:
        return ("a0d7b954_wmbusmeters",) if self.started else ()

    def addon_state(self, _slug: str) -> str:
        return "started" if self.started else "stopped"

    def stop_addon(self, slug: str) -> None:
        self.events.append(f"stop:{slug}")
        self.started = False

    def start_addon(self, slug: str) -> None:
        self.events.append(f"start:{slug}")
        self.started = True

    def wait_for_state(self, _slug: str, expected: str) -> None:
        if expected == "stopped" and self.fail_stop_wait_once:
            self.fail_stop_wait_once = False
            raise SupervisorError("simulated polling failure")
        if self.addon_state(_slug) != expected:
            raise SupervisorError("unexpected simulated add-on state")


class SupervisorLifecycleTests(unittest.TestCase):
    def test_restores_only_addon_that_was_initially_running(self) -> None:
        supervisor = _LifecycleSupervisor()
        with supervisor.temporarily_stop_wmbusmeters(Path("/dev/ttyACM0")) as pause:
            self.assertEqual(pause.addons, ("a0d7b954_wmbusmeters",))
            self.assertFalse(supervisor.started)

        self.assertTrue(supervisor.started)
        self.assertEqual(
            supervisor.events,
            ["stop:a0d7b954_wmbusmeters", "start:a0d7b954_wmbusmeters"],
        )

    def test_stop_poll_failure_still_attempts_restore(self) -> None:
        supervisor = _LifecycleSupervisor(fail_stop_wait_once=True)
        with (
            self.assertRaisesRegex(SupervisorError, "simulated polling failure"),
            supervisor.temporarily_stop_wmbusmeters(Path("/dev/ttyACM0")),
        ):
            self.fail("context must not yield after the stop poll failed")

        self.assertTrue(supervisor.started)
        self.assertEqual(
            supervisor.events,
            ["stop:a0d7b954_wmbusmeters", "start:a0d7b954_wmbusmeters"],
        )

    def test_leaves_previously_stopped_addon_stopped(self) -> None:
        supervisor = _LifecycleSupervisor(initially_started=False)
        with supervisor.temporarily_stop_wmbusmeters(Path("/dev/ttyACM0")) as pause:
            self.assertEqual(pause.addons, ())

        self.assertFalse(supervisor.started)
        self.assertEqual(supervisor.events, [])

    def test_leaves_matching_addon_stopped_after_an_uncertain_dfu_failure(self) -> None:
        supervisor = _LifecycleSupervisor()
        with (
            self.assertRaisesRegex(RuntimeError, "DFU did not verify") as caught,
            supervisor.temporarily_stop_wmbusmeters(Path("/dev/ttyACM0")) as pause,
        ):
            pause.leave_stopped_after_error()
            raise RuntimeError("DFU did not verify")

        self.assertFalse(supervisor.started)
        self.assertEqual(supervisor.events, ["stop:a0d7b954_wmbusmeters"])
        self.assertIn("wmbusmeters remains stopped", "\n".join(caught.exception.__notes__))

    def test_matches_only_direct_path_and_serial_discovery_modes(self) -> None:
        device = Path("/dev/serial/by-id/cul868")
        self.assertTrue(
            _wmbusmeters_uses_device(
                {"conf": {"device": "/dev/serial/by-id/cul868:cul:t1"}}, device
            )
        )
        self.assertTrue(
            _wmbusmeters_uses_device({"conf": {"device": "auto:t1"}}, device)
        )
        self.assertTrue(
            _wmbusmeters_uses_device({"conf": {"device": "cul:t1"}}, device)
        )
        self.assertFalse(
            _wmbusmeters_uses_device(
                {"conf": {"device": "/dev/ttyUSB9:cul:t1"}}, device
            )
        )
        self.assertFalse(
            _wmbusmeters_uses_device({"conf": {"device": "rtlwmbus:t1"}}, device)
        )

    def test_selects_only_started_matching_official_addon(self) -> None:
        class OptionsSupervisor(SupervisorClient):
            def _request(self, method: str, path: str) -> dict[str, object]:
                if method != "GET":
                    raise AssertionError(f"unexpected Supervisor method: {method}")
                if path == "/addons":
                    return {
                        "data": {
                            "addons": [
                                {"slug": "wmbusmeters-ha-addon"},
                                {"slug": "wmbusmeters-ha-addon-edge"},
                                {"slug": "unrelated"},
                            ]
                        }
                    }
                if path == "/addons/wmbusmeters-ha-addon/info":
                    return {
                        "data": {
                            "state": "started",
                            "options": {"conf": {"device": "/dev/ttyACM0:cul:t1"}},
                        }
                    }
                if path == "/addons/wmbusmeters-ha-addon-edge/info":
                    return {
                        "data": {
                            "state": "stopped",
                            "options": {"conf": {"device": "auto:t1"}},
                        }
                    }
                raise AssertionError(f"unexpected Supervisor request: {path}")

        client = OptionsSupervisor.__new__(OptionsSupervisor)
        self.assertEqual(
            client.running_wmbusmeters_using_device(Path("/dev/ttyACM0")),
            ("wmbusmeters-ha-addon",),
        )

    def test_retargets_only_exact_paused_by_id_paths_and_own_option(self) -> None:
        previous = Path("/dev/serial/by-id/usb-busware.de_CUL868-old-if00")
        current = Path("/dev/serial/by-id/usb-busware.de_CUL868-new-if00")

        class OptionsSupervisor(SupervisorClient):
            def __init__(self) -> None:
                self.options: dict[str, dict[str, object]] = {
                    "self": {"device": str(previous), "baudrate": 9_600},
                    "a0d7b954_wmbusmeters": {
                        "conf": {
                            "device": f"MAIN= {previous} :cul:t1; auto:t1",
                            "loglevel": "normal",
                        },
                        "mqtt": {"username": "not logged"},
                    },
                }
                self.requests: list[tuple[str, str, dict[str, object] | None]] = []

            def _request(
                self, method: str, path: str, payload: dict[str, object] | None = None
            ) -> dict[str, object]:
                self.requests.append((method, path, payload))
                if method == "GET" and path.endswith("/info"):
                    slug = path.removeprefix("/addons/").removesuffix("/info")
                    return {"data": {"options": self.options[slug]}}
                if method == "POST" and path.endswith("/options"):
                    slug = path.removeprefix("/addons/").removesuffix("/options")
                    assert payload is not None
                    options = payload.get("options")
                    assert isinstance(options, dict)
                    self.options[slug] = options
                    return {"result": "ok"}
                raise AssertionError(f"unexpected Supervisor request: {method} {path}")

        supervisor = OptionsSupervisor()
        pause = WmbusmetersPause(("a0d7b954_wmbusmeters",))

        self.assertEqual(
            supervisor.retarget_paused_wmbusmeters(pause, previous, current),
            ("a0d7b954_wmbusmeters",),
        )
        self.assertTrue(supervisor.retarget_own_device_path(previous, current))
        self.assertEqual(supervisor.options["self"]["device"], str(current))
        self.assertEqual(
            supervisor.options["a0d7b954_wmbusmeters"]["conf"],
            {
                "device": f"MAIN= {current} :cul:t1; auto:t1",
                "loglevel": "normal",
            },
        )
        self.assertEqual(
            [(method, path) for method, path, _payload in supervisor.requests],
            [
                ("GET", "/addons/a0d7b954_wmbusmeters/info"),
                ("POST", "/addons/a0d7b954_wmbusmeters/options"),
                ("GET", "/addons/self/info"),
                ("POST", "/addons/self/options"),
            ],
        )

    def test_does_not_overwrite_a_different_own_device_option(self) -> None:
        previous = Path("/dev/serial/by-id/usb-busware.de_CUL868-old-if00")
        current = Path("/dev/serial/by-id/usb-busware.de_CUL868-new-if00")

        class ChangedOptionsSupervisor(SupervisorClient):
            def __init__(self) -> None:
                self.requests: list[tuple[str, str]] = []

            def _request(
                self, method: str, path: str, _payload: dict[str, object] | None = None
            ) -> dict[str, object]:
                self.requests.append((method, path))
                if method == "GET":
                    return {"data": {"options": {"device": "/dev/ttyACM9"}}}
                raise AssertionError("the changed option must not be overwritten")

        supervisor = ChangedOptionsSupervisor()

        self.assertFalse(supervisor.retarget_own_device_path(previous, current))
        self.assertEqual(supervisor.requests, [("GET", "/addons/self/info")])
