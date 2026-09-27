"""Behavioral regression tests; run without Home Assistant installed."""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

PATH = Path(__file__).parents[1] / "custom_components/home_control/controller.py"
SPEC = importlib.util.spec_from_file_location("home_control_controller", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
ChandelierController = MODULE.ChandelierController
is_single_press = MODULE.is_single_press
GROUPS = tuple(f"switch.group_{i}" for i in range(1, 5))


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.states = ["off"] * 4
        self.commands = []
        self.error = None
        self.block = None
        self.started = asyncio.Event()
        self.controller = ChandelierController(
            GROUPS, lambda: self.states, self.send, clock=lambda: self.now
        )
        self.controller.set_enabled(True)

    async def send(self, on, targets):
        self.commands.append((on, targets))
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        if self.error:
            raise self.error

    async def press(self, after=0):
        self.now += after
        await self.controller.async_press(self.controller.capture_press(), "test")

    async def test_complete_cycle_with_no_feedback_and_immediate_restart(self):
        for _ in range(6):
            await self.press(0.1)
        self.assertEqual(
            self.commands,
            [
                (True, GROUPS),
                (False, (GROUPS[0],)),
                (False, (GROUPS[1],)),
                (False, (GROUPS[2],)),
                (False, (GROUPS[3],)),
                (True, GROUPS),
            ],
        )
        self.assertEqual(self.controller.step, 4)

    async def test_initial_stale_all_off_does_not_restart_cycle(self):
        await self.press()
        await self.press(0.1)
        self.assertEqual(self.commands[-1], (False, (GROUPS[0],)))

    async def test_terminal_stale_on_does_not_repeat_off(self):
        await self.press()
        self.states = ["on"] * 4
        for _ in range(4):
            await self.press(0.1)
        await self.press(0.1)
        self.assertEqual(self.commands[-1], (True, GROUPS))

    async def test_window_refreshes_after_every_press(self):
        await self.press()
        await self.press(2.9)
        await self.press(2.9)
        self.assertEqual(self.commands[-1], (False, (GROUPS[1],)))

    async def test_exact_deadline_is_inside_window(self):
        await self.press()
        await self.press(3)
        self.assertEqual(self.commands[-1], (False, (GROUPS[0],)))

    async def test_after_deadline_turns_everything_off_despite_stale_off(self):
        await self.press()
        await self.press(3.01)
        self.assertEqual(self.commands[-1], (False, GROUPS))

    async def test_restart_with_partial_light_turns_all_off(self):
        self.states = ["off", "on", "off", "off"]
        await self.press()
        self.assertEqual(self.commands, [(False, GROUPS)])

    async def test_unknown_group_resets_and_sends_nothing(self):
        await self.press()
        self.states[1] = "unavailable"
        await self.press(0.1)
        self.assertEqual(len(self.commands), 1)
        self.assertIsNone(self.controller.step)
        self.assertEqual(self.controller.last_error, "unavailable_group")

    async def test_disabled_press_is_not_replayed_after_enable(self):
        self.controller.set_enabled(False)
        ignored = self.controller.capture_press()
        self.controller.set_enabled(True)
        await self.controller.async_press(ignored, "disabled")
        self.assertEqual(self.commands, [])

    async def test_disable_drops_queued_press_and_does_not_switch_lights(self):
        self.block = asyncio.Event()
        first = asyncio.create_task(self.press())
        await self.started.wait()
        ticket = self.controller.capture_press()
        queued = asyncio.create_task(self.controller.async_press(ticket, "queued"))
        await asyncio.sleep(0)
        self.controller.set_enabled(False)
        self.block.set()
        await asyncio.gather(first, queued)
        self.assertEqual(self.commands, [(True, GROUPS)])
        self.assertIsNone(self.controller.step)
        self.assertIsNone(self.controller.feedback_due)

    async def test_disable_enable_invalidates_old_generation(self):
        ticket = self.controller.capture_press()
        self.controller.set_enabled(False)
        self.controller.set_enabled(True)
        await self.controller.async_press(ticket, "old")
        self.assertEqual(self.commands, [])

    async def test_simultaneous_sources_are_serialized(self):
        self.block = asyncio.Event()
        first = asyncio.create_task(self.press())
        await self.started.wait()
        second = asyncio.create_task(
            self.controller.async_press(self.controller.capture_press(), "mqtt")
        )
        third = asyncio.create_task(
            self.controller.async_press(self.controller.capture_press(), "input_button")
        )
        await asyncio.sleep(0)
        self.assertEqual(len(self.commands), 1)
        self.block.set()
        await asyncio.gather(first, second, third)
        self.assertEqual(
            self.commands, [(True, GROUPS), (False, (GROUPS[0],)), (False, (GROUPS[1],))]
        )

    async def test_service_failure_resets_cycle_and_invalidates_queue(self):
        self.error = RuntimeError("service failed")
        ticket = self.controller.capture_press()
        with self.assertLogs(MODULE.__name__, level="ERROR"):
            await self.press()
        self.error = None
        await self.controller.async_press(ticket, "queued")
        self.assertIsNone(self.controller.step)
        self.assertEqual(self.controller.last_error, "command_failed")
        self.assertEqual(len(self.commands), 1)

    async def test_feedback_timeout_resets_without_corrective_commands(self):
        await self.press()
        self.now += 5
        with self.assertLogs(MODULE.__name__, level="WARNING"):
            self.controller.check_feedback()
        self.assertEqual(self.controller.last_error, "feedback_timeout")
        self.assertIsNone(self.controller.step)
        self.assertEqual(len(self.commands), 1)

    async def test_matching_feedback_confirms_but_preserves_selection(self):
        await self.press()
        self.states = ["on"] * 4
        self.controller.check_feedback()
        self.assertIsNone(self.controller.expected)
        self.assertEqual(self.controller.step, 4)
        await self.press(0.5)
        self.assertEqual(self.commands[-1], (False, (GROUPS[0],)))

    async def test_expired_cycle_uses_actual_external_states(self):
        await self.press()
        self.states = ["on"] * 4
        self.controller.check_feedback()
        self.states = ["off"] * 4
        await self.press(3.1)
        self.assertEqual(self.commands[-1], (True, GROUPS))

    async def test_command_delay_does_not_extend_window(self):
        self.block = asyncio.Event()
        pending = asyncio.create_task(self.press())
        await self.started.wait()
        self.now += 2
        self.block.set()
        await pending
        self.assertEqual(self.controller.deadline, 103)
        self.assertEqual(self.controller.feedback_due, 107)

    async def test_long_stalled_input_is_discarded(self):
        ticket = self.controller.capture_press()
        self.now += 10.1
        await self.controller.async_press(ticket, "stale")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.controller.last_error, "stale_press")

    async def test_default_is_disabled_and_startup_sends_nothing(self):
        controller = ChandelierController(GROUPS, lambda: self.states, self.send)
        self.assertFalse(controller.enabled)
        self.assertIsNone(controller.capture_press())
        self.assertEqual(self.commands, [])

    async def test_invalid_feedback_on_press_abandons_that_press(self):
        await self.press()
        self.now += 5.1
        with self.assertLogs(MODULE.__name__, level="WARNING"):
            await self.press()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.controller.last_error, "feedback_timeout")


class PayloadTests(unittest.TestCase):
    def test_semantic_json(self):
        self.assertTrue(
            is_single_press('{"Extra": 1, "Button4": {"Action": "SINGLE", "More": 0}}', "Button4")
        )

    def test_wrong_button_action_and_invalid_shapes(self):
        for payload in (
            "null",
            "[]",
            "1",
            '"text"',
            "{",
            '{"Button4":null}',
            '{"Button4":"SINGLE"}',
            '{"Button4":{"Action":"DOUBLE"}}',
            '{"Button1":{"Action":"SINGLE"}}',
            b"\xff",
        ):
            with self.subTest(payload=payload):
                self.assertFalse(is_single_press(payload, "Button4"))

    def test_retained_single_is_ignored(self):
        self.assertFalse(is_single_press('{"Button4":{"Action":"SINGLE"}}', "Button4", retain=True))

    def test_duplicate_groups_rejected(self):
        with self.assertRaises(ValueError):
            ChandelierController(["switch.same"] * 4, lambda: [], None)


if __name__ == "__main__":
    unittest.main()
