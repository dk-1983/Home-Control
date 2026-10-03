"""Motor sequencing exercised against HA with optimistic writes and delayed polls."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant is not installed")
class HoodTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.hood import INPUT_KEYS, OUTPUT_KEYS

        self.temp = TemporaryDirectory(dir=Path(__file__).parents[1] / "work", prefix="ha-hood-")
        self.hass = HomeAssistant(self.temp.name)
        self.outputs = [f"switch.speed_{i}" for i in range(4)]
        self.inputs = [f"binary_sensor.input_{i}" for i in range(4)]
        self.config = (
            dict(zip(OUTPUT_KEYS, self.outputs))
            | dict(zip(INPUT_KEYS, self.inputs))
            | {
                "process_type": "hood",
                "feedback_timeout": 0.12,
                "break_delay": 0.005,
                "input_settle": 0.01,
            }
        )
        self.hardware = [False] * 4
        self.commands = []
        self.runtimes = []
        self.polling = True
        self.ack_writes = True
        self.overlap = False
        from homeassistant.helpers.entity_component import DATA_INSTANCES
        from hood_fakes import FakeCoordinator, FakeRelay

        self.coordinator = FakeCoordinator(self.hass, self)
        self.entities = {
            entity: FakeRelay(self.coordinator, i + 1) for i, entity in enumerate(self.outputs)
        }
        self.hass.data[DATA_INSTANCES] = {"switch": SimpleNamespace(get_entity=self.entities.get)}
        self._poll_task = asyncio.create_task(self.poll_loop())
        self.publish()
        for entity in self.inputs:
            self.hass.states.async_set(entity, "off")

        async def service(call):
            entity = call.data["entity_id"]
            on = call.service == "turn_on"
            self.commands.append((on, self.outputs.index(entity)))
            if self.ack_writes:
                self.hardware[self.outputs.index(entity)] = on
                self.overlap |= sum(self.hardware) > 1
            # Successful service return only updates the optimistic state.
            self.coordinator._write_generation += 1
            old = self.hass.states.get(entity)
            self.hass.states.async_set(entity, "on" if on else "off", dict(old.attributes))

        for name in ("turn_on", "turn_off"):
            self.hass.services.async_register("switch", name, service)

    def publish(self):
        for entity, value in zip(self.outputs, self.hardware):
            self.hass.states.async_set(entity, "on" if value else "off")

    async def poll_loop(self):
        while True:
            await asyncio.sleep(0.01)
            if self.polling:
                self.publish()

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.async_stop()
        self._poll_task.cancel()
        await asyncio.gather(self._poll_task, return_exceptions=True)
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def create(self, entry_id="hood"):
        from custom_components.home_control.hood import HoodRuntime

        runtime = HoodRuntime(
            self.hass,
            SimpleNamespace(
                data=self.config,
                options={},
                entry_id=entry_id,
                title="Hood",
            ),
        )
        self.runtimes.append(runtime)
        await runtime.async_start()
        return runtime

    async def enabled(self):
        runtime = await self.create()
        await runtime.async_set_enabled(True)
        return runtime

    async def test_disabled_and_enable_never_start_static_panel(self):
        self.hass.states.async_set(self.inputs[2], "on")
        runtime = await self.create()
        await asyncio.sleep(0.025)
        self.assertFalse(runtime.enabled)
        await runtime.async_set_enabled(True)
        await asyncio.sleep(0.025)
        self.assertEqual(self.commands, [])

    async def test_break_before_make_and_duplicate_speed_noop(self):
        runtime = await self.enabled()
        await runtime.async_request(50)
        await runtime.async_request(75)
        self.assertFalse(self.overlap)
        self.assertEqual(self.hardware, [False, False, True, False])
        self.assertEqual(self.commands[5:9], [(False, i) for i in range(4)])
        before = list(self.commands)
        await runtime.async_request(75)
        self.assertEqual(self.commands, before)
        self.assertEqual(runtime.last_speed, 75)

    async def test_restart_keeps_memory_without_starting(self):
        runtime = await self.enabled()
        await runtime.async_request(75)
        await runtime.async_request(0)
        await runtime.async_stop()
        self.commands.clear()
        restored = await self.create()
        self.assertTrue(restored.enabled)
        self.assertEqual(restored.last_speed, 75)
        await asyncio.sleep(0.025)
        self.assertEqual(self.commands, [])
        await restored.async_request()
        self.assertEqual(restored.percentage, 75)

    async def test_optimistic_off_cannot_start_next_speed(self):
        runtime = await self.enabled()
        self.hardware[1] = True
        self.publish()
        self.polling = False
        self.ack_writes = False
        with self.assertRaisesRegex(Exception, "feedback_timeout"):
            await runtime.async_request(75)
        self.assertFalse(any(on for on, _ in self.commands))
        self.assertEqual(runtime.last_speed, 25)
        self.assertTrue(runtime.attributes["commands_blocked"])

    async def test_cached_ha_update_cannot_replace_requested_poll(self):
        runtime = await self.enabled()
        self.polling = False
        cached = tuple(self.hardware)
        request = asyncio.create_task(runtime.async_request(100))
        await asyncio.sleep(0.01)
        self.publish()
        self.assertEqual(cached, (False,) * 4)
        with self.assertRaises(Exception):
            await request
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_latest_target_wins_while_waiting_for_off(self):
        runtime = await self.enabled()
        self.polling = False
        first = asyncio.create_task(runtime.async_request(25))
        await asyncio.sleep(0.005)
        second = asyncio.create_task(runtime.async_request(100))
        self.polling = True
        await asyncio.gather(first, second)
        self.assertEqual([i for on, i in self.commands if on], [3])
        self.assertFalse(self.overlap)

    async def test_stop_cancels_start_waiting_for_off(self):
        runtime = await self.enabled()
        self.polling = False
        first = asyncio.create_task(runtime.async_request(100))
        await asyncio.sleep(0.005)
        stop = asyncio.create_task(runtime.async_request(0))
        self.polling = True
        await asyncio.gather(first, stop)
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_disable_drops_pending_start_without_more_commands(self):
        runtime = await self.enabled()
        self.polling = False
        first = asyncio.create_task(runtime.async_request(100))
        await asyncio.sleep(0.015)
        before = list(self.commands)
        await runtime.async_set_enabled(False)
        await first
        self.assertEqual(self.commands, before)
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_timeout_recovers_only_on_new_request_with_off_barrier(self):
        runtime = await self.enabled()
        self.polling = False
        with self.assertRaises(Exception):
            await runtime.async_request(50)
        count = len(self.commands)
        self.polling = True
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.commands), count)
        self.assertTrue(runtime._failed)
        await runtime.async_request(100)
        self.assertEqual(self.commands[count:], [(False, i) for i in range(4)] + [(True, 3)])
        self.assertEqual(runtime.percentage, 100)
        self.assertFalse(runtime._failed)
        self.assertIsNone(runtime.last_error)
        self.assertFalse(self.overlap)

    async def test_recovery_does_not_trust_optimistic_off(self):
        runtime = await self.enabled()
        self.polling = False
        with self.assertRaises(Exception):
            await runtime.async_request(50)
        self.polling = True
        self.hardware[0] = True
        self.publish()
        self.ack_writes = False
        with self.assertRaisesRegex(Exception, "feedback_timeout"):
            await runtime.async_request(100)
        self.assertTrue(runtime._failed)
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_panel_new_edge_recovers_transient_fault(self):
        runtime = await self.enabled()
        self.polling = False
        with self.assertRaises(Exception):
            await runtime.async_request(50)
        self.polling = True
        self.hass.states.async_set(self.inputs[2], "on")
        await asyncio.sleep(0.06)
        await runtime.async_wait_idle()
        self.assertFalse(runtime._failed)
        self.assertEqual(runtime.percentage, 75)
        self.assertFalse(self.overlap)

    async def test_stop_confirms_recovery_without_starting(self):
        runtime = await self.enabled()
        self.polling = False
        with self.assertRaises(Exception):
            await runtime.async_request(50)
        self.polling = True
        await runtime.async_request(0)
        self.assertFalse(runtime._failed)
        self.assertIsNone(runtime.last_error)
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_disabling_during_recovery_stops_further_commands(self):
        runtime = await self.enabled()
        self.polling = False
        with self.assertRaises(Exception):
            await runtime.async_request(50)
        request = asyncio.create_task(runtime.async_request(100))
        await asyncio.sleep(0.01)
        count = len(self.commands)
        await runtime.async_set_enabled(False)
        with self.assertRaises(Exception):
            await request
        self.polling = True
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.commands), count)
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_read_failure_recovers_but_rechecks_running_target(self):
        from custom_components.home_control.hood_readback import ReadbackError

        runtime = await self.enabled()
        with patch.object(
            runtime.readback, "async_read", side_effect=ReadbackError("readback_failed")
        ):
            with self.assertRaisesRegex(Exception, "readback_failed"):
                await runtime.async_request(75)
        # Even if the requested speed already appears on, recovery must stop first.
        self.hardware[2] = True
        self.publish()
        count = len(self.commands)
        await runtime.async_request(75)
        self.assertEqual(self.commands[count:], [(False, i) for i in range(4)] + [(True, 2)])
        self.assertFalse(runtime._failed)
        self.assertFalse(self.overlap)

    async def test_automatic_recovery_after_network_returns_without_replaying(self):
        with patch("custom_components.home_control.hood.RECOVERY_INTERVAL", 0.02):
            runtime = await self.enabled()
            self.polling = False
            with self.assertRaises(Exception):
                await runtime.async_request(75)
            count = len(self.commands)
            self.polling = True
            for _ in range(100):
                if not runtime._failed:
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(runtime._failed)
            self.assertIsNone(runtime.last_error)
            self.assertEqual(runtime.phase, "idle")
            self.assertEqual(self.commands[count:], [(False, i) for i in range(4)])
            self.assertEqual(runtime.last_speed, 25)

    async def test_recovery_stops_running_channel_without_restarting(self):
        runtime = await self.enabled()
        self.polling = False
        with self.assertRaises(Exception):
            await runtime.async_request(75)
        self.polling = True
        self.hardware[1] = True
        self.publish()
        count = len(self.commands)
        await runtime._probe_recovery(runtime._generation)
        self.assertFalse(runtime._failed)
        self.assertEqual(self.commands[count:], [(False, i) for i in range(4)])
        self.assertFalse(any(self.hardware))

    async def test_safety_recovery_resets_outputs_and_requires_confirmed_off(self):
        runtime = await self.enabled()
        self.hardware[:] = [True, True, False, False]
        self.publish()
        await asyncio.sleep(0.04)
        await runtime.async_wait_idle()
        count = len(self.commands)
        self.hardware[0] = True
        self.ack_writes = False
        await runtime._probe_recovery(runtime._generation)
        self.assertTrue(runtime._failed)
        self.assertIsNotNone(runtime.recovery_error)
        self.ack_writes = True
        await runtime._probe_recovery(runtime._generation)
        self.assertFalse(runtime._failed)
        self.assertIsNone(runtime.recovery_error)
        self.assertFalse(any(self.hardware))
        self.assertFalse(any(on for on, _ in self.commands[count:]))

    async def test_idle_unavailability_detected_without_user_command(self):
        runtime = await self.enabled()
        self.polling = False
        self.hass.states.async_set(self.outputs[0], "unavailable")
        await asyncio.sleep(0)
        self.assertTrue(runtime._failed)
        self.assertEqual(runtime.last_error, "relay_unavailable")
        self.assertEqual(self.commands, [])
        self.polling = True
        self.publish()
        await runtime._probe_recovery(runtime._generation)
        self.assertFalse(runtime._failed)
        self.assertEqual(self.commands, [(False, i) for i in range(4)])
        count = len(self.commands)
        await runtime._probe_recovery(runtime._generation)
        self.assertEqual(len(self.commands), count)

    async def test_disabled_unavailability_does_not_reset_equipment(self):
        runtime = await self.create()
        self.hass.states.async_set(self.outputs[0], "unavailable")
        await asyncio.sleep(0.02)
        self.assertFalse(runtime._failed)
        self.assertIsNone(runtime._recovery_timer)
        self.assertEqual(self.commands, [])

    async def test_recovery_and_user_start_cannot_overlap(self):
        runtime = await self.enabled()
        runtime._record_fault("relay_unavailable")
        self.polling = False
        runtime._start_recovery()
        await asyncio.sleep(0.01)
        with self.assertRaisesRegex(Exception, "reset is in progress"):
            await runtime.async_request(100)
        self.polling = True
        await runtime._recovery_task
        self.assertFalse(runtime._failed)
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_disabling_during_emergency_off_stops_remaining_channels(self):
        runtime = await self.enabled()
        runtime._record_fault("relay_unavailable")
        original = runtime._command

        async def command(entity, on, generation):
            result = await original(entity, on, generation)
            runtime.set_enabled(False)
            return result

        runtime._command = command
        await runtime._probe_recovery(runtime._generation)
        self.assertEqual(self.commands, [(False, 0)])
        self.assertTrue(runtime._failed)
        self.assertIsNone(runtime._recovery_timer)

    async def test_emergency_reset_attempts_other_channels_after_service_failure(self):
        runtime = await self.enabled()
        runtime._record_fault("relay_unavailable")
        attempts = []

        async def service(call):
            attempts.append(call.data["entity_id"])
            if len(attempts) == 1:
                raise OSError("first channel failed")

        self.hass.services.async_register("switch", "turn_off", service)
        await runtime._probe_recovery(runtime._generation)
        self.assertEqual(attempts, self.outputs)
        self.assertTrue(runtime._failed)
        self.assertEqual(runtime.recovery_error, "command_failed")

    async def test_unavailability_during_start_cancels_target(self):
        runtime = await self.enabled()
        original = runtime.readback.async_read
        calls = 0

        async def read():
            nonlocal calls
            calls += 1
            if calls == 1:
                self.hass.states.async_set(self.outputs[0], "unavailable")
                await asyncio.sleep(0)
            return await original()

        with patch.object(runtime.readback, "async_read", side_effect=read):
            with self.assertRaises(Exception):
                await runtime.async_request(75)
        self.assertFalse(any(on for on, _ in self.commands))
        self.assertIsNone(runtime._target)

    async def test_disable_cancels_recovery_poll_and_timer(self):
        with patch("custom_components.home_control.hood.RECOVERY_INTERVAL", 0.02):
            runtime = await self.enabled()
            self.polling = False
            with self.assertRaises(Exception):
                await runtime.async_request(75)
            await asyncio.sleep(0.03)
            self.assertIsNotNone(runtime._recovery_task)
            count = len(self.commands)
            await runtime.async_set_enabled(False)
            self.polling = True
            await asyncio.sleep(0.04)
            self.assertTrue(runtime._failed)
            self.assertIsNone(runtime._recovery_timer)
            self.assertEqual(len(self.commands), count)

    async def test_inconsistent_recovery_snapshots_keep_fault(self):
        runtime = await self.enabled()
        self.polling = False
        with self.assertRaises(Exception):
            await runtime.async_request(75)
        self.polling = True
        with patch.object(
            runtime.readback,
            "async_read",
            side_effect=[(False,) * 4, (True, False, False, False)],
        ):
            await runtime._probe_recovery(runtime._generation)
        self.assertTrue(runtime._failed)

    async def test_panel_edges_and_static_panel_does_not_override_voice(self):
        runtime = await self.enabled()
        self.hass.states.async_set(self.inputs[1], "on")
        await asyncio.sleep(0.06)
        await runtime.async_wait_idle()
        self.assertEqual(runtime.percentage, 50)
        await runtime.async_request(100)
        self.hass.states.async_set(self.inputs[1], "on", {"new_attribute": True})
        await asyncio.sleep(0.025)
        self.assertEqual(runtime.percentage, 100)
        self.hass.states.async_set(self.inputs[1], "off")
        self.hass.states.async_set(self.inputs[2], "on")
        await asyncio.sleep(0.06)
        await runtime.async_wait_idle()
        self.assertEqual(runtime.percentage, 75)

    async def test_panel_recovery_and_multiple_inputs_do_not_start(self):
        runtime = await self.enabled()
        self.hass.states.async_set(self.inputs[0], "unavailable")
        await asyncio.sleep(0)
        self.hass.states.async_set(self.inputs[0], "on")
        await asyncio.sleep(0.025)
        self.assertEqual(self.commands, [])
        self.hass.states.async_set(self.inputs[1], "on")
        await asyncio.sleep(0.025)
        self.assertEqual(self.commands, [])
        self.assertEqual(runtime.input_error, "multiple_panel_inputs")

    async def test_only_confirmed_start_updates_memory(self):
        runtime = await self.enabled()
        original = runtime._command

        async def command(entity, on, generation):
            result = await original(entity, on, generation)
            if on:
                self.polling = False
            return result

        runtime._command = command
        with self.assertRaises(Exception):
            await runtime.async_request(100)
        self.assertEqual(runtime.last_speed, 25)
        self.assertFalse(self.overlap)

    async def test_storage_failure_is_reported_and_stops_motor(self):
        runtime = await self.enabled()
        with patch.object(runtime.store, "async_save", AsyncMock(side_effect=OSError)):
            with self.assertRaisesRegex(Exception, "storage_failed"):
                await runtime.async_request(75)
        self.assertFalse(any(self.hardware))

    async def test_validation_ownership_and_partial_inputs(self):
        from custom_components.home_control.hood_config import validate_hood
        from custom_components.home_control.process_config import owned_outputs

        self.assertEqual(owned_outputs(self.config), set(self.outputs))
        fake = SimpleNamespace(
            states=self.hass.states,
            data=self.hass.data,
            config_entries=SimpleNamespace(async_entries=lambda _: []),
        )
        with patch.object(fake.config_entries, "async_entries", return_value=[]):
            _, errors = validate_hood(fake, self.config)
            self.assertEqual(errors, {})
            _, errors = validate_hood(fake, self.config | {"input_25": ""})
            self.assertEqual(errors["base"], "hood_inputs_complete")
            self.entities.pop(self.outputs[0])
            _, errors = validate_hood(fake, self.config)
            self.assertEqual(errors["base"], "hood_readback_required")

    async def test_fan_entity_routes_resume_stop_and_percentages(self):
        from custom_components.home_control.fan import HoodFan

        runtime = await self.enabled()
        fan = HoodFan(runtime)
        await fan.async_set_percentage(60)
        self.assertEqual(fan.percentage, 75)
        await fan.async_turn_off()
        self.assertFalse(fan.is_on)
        await fan.async_turn_on()
        self.assertEqual(fan.percentage, 75)
        self.assertEqual(fan.speed_count, 4)

    async def test_real_fan_platform_load_and_unload(self):
        from test_ha_runtime import HomeAssistantRuntimeTests

        self._poll_task.cancel()
        await asyncio.gather(self._poll_task, return_exceptions=True)
        await HomeAssistantRuntimeTests.test_config_entry_loads_real_switch_and_button_platforms(
            self
        )

    async def test_hood_flow_and_diagnostics(self):
        from custom_components.home_control.config_flow import HomeControlConfigFlow
        from custom_components.home_control.diagnostics import async_get_config_entry_diagnostics

        flow = HomeControlConfigFlow()
        flow.hass = SimpleNamespace(
            states=self.hass.states,
            data=self.hass.data,
            config_entries=SimpleNamespace(async_entries=lambda _: []),
        )
        form = await flow.async_step_hood()
        self.assertEqual(form["type"], "form")
        result = await flow.async_step_hood(self.config | {"name": "Hood"})
        self.assertEqual(result["type"], "create_entry")
        runtime = await self.create()
        runtime.entry.runtime_data = runtime
        diagnostic = await async_get_config_entry_diagnostics(self.hass, runtime.entry)
        self.assertNotEqual(diagnostic["config"]["speed_25"], self.outputs[0])
        self.assertEqual(diagnostic["reported_states"], ["off"] * 4)

    async def test_new_request_during_start_confirmation_stops_before_new_speed(self):
        runtime = await self.enabled()
        original = runtime._command
        first_on = asyncio.Event()

        async def command(entity, on, generation):
            result = await original(entity, on, generation)
            if on and entity == self.outputs[0]:
                self.polling = False
                first_on.set()
            return result

        runtime._command = command
        first = asyncio.create_task(runtime.async_request(25))
        await asyncio.wait_for(first_on.wait(), 1)
        second = asyncio.create_task(runtime.async_request(100))
        await asyncio.sleep(0.01)
        self.polling = True
        await asyncio.gather(first, second)
        self.assertFalse(self.overlap)
        self.assertEqual(runtime.percentage, 100)

    async def test_service_failure_never_starts_another_channel(self):
        runtime = await self.enabled()

        async def fail(call):
            raise OSError("offline")

        self.hass.services.async_register("switch", "turn_off", fail)
        with self.assertRaisesRegex(Exception, "command_failed"):
            await runtime.async_request(100)
        self.assertFalse(any(on for on, _ in self.commands))

    async def test_voice_supersedes_pending_panel_command(self):
        runtime = await self.enabled()
        self.hass.states.async_set(self.inputs[0], "on")
        await asyncio.sleep(0)
        await runtime.async_request(75)
        await asyncio.sleep(0.025)
        self.assertEqual(runtime.percentage, 75)
        self.assertEqual([i for on, i in self.commands if on], [2])

    async def test_detected_multiple_outputs_stop_and_latch(self):
        runtime = await self.enabled()
        self.hardware[:] = [True, True, False, False]
        self.publish()
        await asyncio.sleep(0.04)
        await runtime.async_wait_idle()
        self.assertFalse(any(self.hardware))
        self.assertTrue(runtime.attributes["commands_blocked"])
        self.assertFalse(any(on for on, _ in self.commands))
        count = len(self.commands)
        with self.assertRaisesRegex(Exception, "blocked"):
            await runtime.async_request(100)
        self.hass.states.async_set(self.inputs[2], "on")
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.commands), count)
        # A physical stop is still accepted while the safety fault stays latched.
        self.hass.states.async_set(self.inputs[2], "off")
        await asyncio.sleep(0.04)
        await runtime.async_wait_idle()
        self.assertEqual(self.commands[count:], [(False, i) for i in range(4)])
        self.assertTrue(runtime._failed)

    async def test_failed_stop_does_not_downgrade_safety_latch(self):
        runtime = await self.enabled()
        self.hardware[:] = [True, True, False, False]
        self.publish()
        await asyncio.sleep(0.04)
        await runtime.async_wait_idle()
        self.assertEqual(runtime.last_error, "multiple_active_outputs")
        self.polling = False
        with self.assertRaisesRegex(Exception, "multiple_active_outputs"):
            await runtime.async_request(0)
        self.polling = True
        count = len(self.commands)
        with self.assertRaisesRegex(Exception, "blocked"):
            await runtime.async_request(100)
        self.assertEqual(len(self.commands), count)
        await runtime.async_set_enabled(False)
        await runtime.async_set_enabled(True)
        await runtime.async_request(100)
        self.assertEqual(runtime.percentage, 100)

    async def test_concurrent_lighting_write_requires_another_poll(self):
        runtime = await self.enabled()
        original = self.coordinator._async_update_data
        raced = False

        async def update():
            nonlocal raced
            data = await original()
            if not raced:
                raced = True
                self.coordinator._write_generation += 1
            return data

        self.coordinator._async_update_data = update
        await runtime.async_request(75)
        self.assertTrue(raced)
        self.assertFalse(runtime.attributes["commands_blocked"])
        self.assertEqual(runtime.last_speed, 75)
        self.assertFalse(self.overlap)

    async def test_light_follows_sessions_and_preserves_manual_off_across_speeds(self):
        from custom_components.home_control.light import HoodLight

        self.config["hood_light"] = "light.hood"
        self.hass.states.async_set("light.hood", "off")
        calls = []

        async def light_service(call):
            calls.append(call.service)
            self.hass.states.async_set("light.hood", "on" if call.service == "turn_on" else "off")

        for name in ("turn_on", "turn_off"):
            self.hass.services.async_register("light", name, light_service)
        runtime = await self.create()
        await runtime.async_set_enabled(True)
        light = HoodLight(runtime)
        await runtime.async_request(25)
        self.assertTrue(light.is_on)
        await runtime.async_request(75)
        self.assertEqual(calls, ["turn_on"])
        await light.async_turn_off()
        await runtime.async_request(50)
        self.assertFalse(light.is_on)
        self.assertEqual(calls, ["turn_on", "turn_off"])
        await runtime.async_request(0)
        await runtime.async_request(100)
        self.assertTrue(light.is_on)
        await runtime.async_request(0)
        self.assertFalse(light.is_on)
        await light.async_turn_on()
        self.assertTrue(light.is_on)
        self.assertEqual(runtime.percentage, 0)

    async def test_light_failure_does_not_fault_motor(self):
        self.config["hood_light"] = "light.hood"
        self.hass.states.async_set("light.hood", "unavailable")
        runtime = await self.create()
        await runtime.async_set_enabled(True)
        await runtime.async_request(50)
        self.assertEqual(runtime.percentage, 50)
        self.assertFalse(runtime._failed)
        self.assertEqual(runtime.light_error, "light_command_failed")

    async def test_light_no_startup_command_and_gate(self):
        from homeassistant.exceptions import HomeAssistantError

        self.config["hood_light"] = "light.hood"
        self.hass.states.async_set("light.hood", "off")
        self.hardware[:] = [False, True, False, False]
        self.publish()
        calls = []

        async def light_service(call):
            calls.append(call.service)

        self.hass.services.async_register("light", "turn_on", light_service)
        runtime = await self.create()
        await runtime.async_set_enabled(True)
        await runtime.async_request(75)
        self.assertEqual(calls, [])
        await runtime.async_set_enabled(False)
        with self.assertRaises(HomeAssistantError):
            await runtime.async_light(True)
        self.assertEqual(calls, [])
