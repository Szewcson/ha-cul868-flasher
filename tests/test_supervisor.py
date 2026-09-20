from __future__ import annotations

import unittest
from pathlib import Path

from app.supervisor import (
    CulConsumerPause,
    SupervisorClient,
    SupervisorError,
    _max2mqtt_uses_device,
    _wmbusmeters_uses_device,
)


class _LifecycleSupervisor(SupervisorClient):
    def __init__(self, *, initially_started: bool = True, fail_stop_wait_once: bool = False) -> None:
        self.started = initially_started
        self.fail_stop_wait_once = fail_stop_wait_once
        self.events: list[str] = []

    def running_cul_consumers_using_device(
        self, _device: Path, _additional_cul_addons: tuple[str, ...]
    ) -> CulConsumerPause:
        return CulConsumerPause(
            wmbusmeters_addons=("a0d7b954_wmbusmeters",) if self.started else ()
        )

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
        with supervisor.temporarily_stop_cul_consumers(Path("/dev/ttyACM0"), ()) as pause:
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
            supervisor.temporarily_stop_cul_consumers(Path("/dev/ttyACM0"), ()),
        ):
            self.fail("context must not yield after the stop poll failed")

        self.assertTrue(supervisor.started)
        self.assertEqual(
            supervisor.events,
            ["stop:a0d7b954_wmbusmeters", "start:a0d7b954_wmbusmeters"],
        )

    def test_leaves_previously_stopped_addon_stopped(self) -> None:
        supervisor = _LifecycleSupervisor(initially_started=False)
        with supervisor.temporarily_stop_cul_consumers(Path("/dev/ttyACM0"), ()) as pause:
            self.assertEqual(pause.addons, ())

        self.assertFalse(supervisor.started)
        self.assertEqual(supervisor.events, [])

    def test_accepts_supervisor_error_after_an_explicit_stop(self) -> None:
        class ErrorAfterStopSupervisor(SupervisorClient):
            def addon_state(self, _slug: str) -> str:
                return "error"

        supervisor = ErrorAfterStopSupervisor.__new__(ErrorAfterStopSupervisor)

        supervisor.wait_for_state("local_homegear", "stopped")

    def test_leaves_matching_addon_stopped_after_an_uncertain_dfu_failure(self) -> None:
        supervisor = _LifecycleSupervisor()
        with (
            self.assertRaisesRegex(RuntimeError, "DFU did not verify") as caught,
            supervisor.temporarily_stop_cul_consumers(Path("/dev/ttyACM0"), ()) as pause,
        ):
            pause.leave_stopped_after_error()
            raise RuntimeError("DFU did not verify")

        self.assertFalse(supervisor.started)
        self.assertEqual(supervisor.events, ["stop:a0d7b954_wmbusmeters"])
        self.assertIn("CUL consumer add-ons remain stopped", "\n".join(caught.exception.__notes__))

    def test_retains_all_matching_apps_after_alias_change(self) -> None:
        class MultiConsumerSupervisor(SupervisorClient):
            def __init__(self) -> None:
                self.states = {
                    "a0d7b954_wmbusmeters": "started",
                    "local_homegear": "started",
                }
                self.events: list[str] = []

            def running_cul_consumers_using_device(
                self, _device: Path, _additional_cul_addons: tuple[str, ...]
            ) -> CulConsumerPause:
                return CulConsumerPause(
                    wmbusmeters_addons=("a0d7b954_wmbusmeters",),
                    additional_addons=("local_homegear",),
                )

            def addon_state(self, slug: str) -> str:
                return self.states[slug]

            def stop_addon(self, slug: str) -> None:
                self.events.append(f"stop:{slug}")
                self.states[slug] = "stopped"

            def start_addon(self, slug: str) -> None:
                self.events.append(f"start:{slug}")
                self.states[slug] = "started"

            def wait_for_state(self, slug: str, expected: str) -> None:
                if self.states[slug] != expected:
                    raise AssertionError(f"unexpected simulated state for {slug}")

        supervisor = MultiConsumerSupervisor()
        with supervisor.temporarily_stop_cul_consumers(
            Path("/dev/ttyACM0"), ("local_homegear",)
        ) as pause:
            pause.retain_addons(pause.addons)

        self.assertEqual(
            supervisor.events,
            [
                "stop:a0d7b954_wmbusmeters",
                "stop:local_homegear",
            ],
        )
        self.assertEqual(supervisor.states["a0d7b954_wmbusmeters"], "stopped")
        self.assertEqual(supervisor.states["local_homegear"], "stopped")

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
        self.assertTrue(_max2mqtt_uses_device({"serial_port": str(device)}, device))
        self.assertFalse(_max2mqtt_uses_device({"serial_port": "/dev/ttyACM9"}, device))

    def test_selects_only_started_matching_known_and_opted_in_addons(self) -> None:
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
                                {"slug": "f591d177_max2mqtt"},
                                {"slug": "local_homegear", "state": "started"},
                                {"slug": "unrelated", "state": "started"},
                                {"slug": ["malformed"]},
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
                if path == "/addons/f591d177_max2mqtt/info":
                    return {
                        "data": {
                            "state": "started",
                            "options": {"serial_port": "/dev/ttyACM0"},
                        }
                    }
                raise AssertionError(f"unexpected Supervisor request: {path}")

        client = OptionsSupervisor.__new__(OptionsSupervisor)
        self.assertEqual(
            client.running_cul_consumers_using_device(
                Path("/dev/ttyACM0"), ("local_homegear",)
            ),
            CulConsumerPause(
                wmbusmeters_addons=("wmbusmeters-ha-addon",),
                max2mqtt_addons=("f591d177_max2mqtt",),
                additional_addons=("local_homegear",),
            ),
        )

    def test_reads_only_canonical_by_id_paths_from_matching_hardware_records(self) -> None:
        current = Path("/dev/serial/by-id/usb-Atmel_CUL868-new-if00")

        class HardwareSupervisor(SupervisorClient):
            def _request(self, method: str, path: str) -> dict[str, object]:
                if method != "GET" or path != "/hardware/info":
                    raise AssertionError(f"unexpected Supervisor request: {method} {path}")
                return {
                    "data": {
                        "devices": [
                            {"dev_path": "/dev/ttyACM0", "by_id": str(current)},
                            {"dev_path": "/dev/ttyACM0", "by_id": str(current)},
                            {
                                "dev_path": "/dev/ttyACM0",
                                "by_id": "/dev/serial/by-id/../not-a-direct-alias",
                            },
                            {"dev_path": "/dev/ttyACM0", "by_id": "/dev/ttyACM0"},
                            {
                                "dev_path": "/dev/ttyACM1",
                                "by_id": "/dev/serial/by-id/usb-other-radio-if00",
                            },
                        ]
                    }
                }

        client = HardwareSupervisor.__new__(HardwareSupervisor)
        self.assertEqual(client.hardware_serial_by_id_paths_for_tty(Path("/dev/ttyACM0")), (current,))

    def test_refuses_non_tty_hardware_inventory_lookup(self) -> None:
        client = SupervisorClient.__new__(SupervisorClient)
        with self.assertRaisesRegex(SupervisorError, "outside /dev/ttyACM"):
            client.hardware_serial_by_id_paths_for_tty(Path("/dev/serial/by-id/cul868"))
