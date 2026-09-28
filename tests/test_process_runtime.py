"""Environmental adapters tested against real HA with local fake services."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant is not installed")
class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        work = Path(__file__).parents[1] / "work"
        work.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=work, prefix="ha-process-")
        self.hass = HomeAssistant(self.temp.name)
        self.runtimes = []
        self.commands = []
        self.config = {
            "process_type": "motion",
            "motion_sensors": ["binary_sensor.motion"],
            "output": "switch.room_light",
            "light_off_delay": 0,
        }
        for entity_id in (
            "binary_sensor.motion",
            "switch.room_light",
            "switch.other_light",
            "switch.bath_fan",
            "switch.shared_fan",
        ):
            self.hass.states.async_set(entity_id, "off")
        self.hass.states.async_set("sensor.humidity", "50")

        async def service(call):
            self.commands.append((call.service, call.data["entity_id"]))
            self.hass.states.async_set(
                call.data["entity_id"], "on" if call.service == "turn_on" else "off"
            )

        for command in ("turn_on", "turn_off"):
            self.hass.services.async_register("switch", command, service)

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def create(self, config=None, entry_id=None):
        from custom_components.home_control.process_runtime import ProcessRuntime

        entry = SimpleNamespace(
            data=config or self.config,
            options={},
            entry_id=entry_id or str(len(self.runtimes)),
            title="Test",
        )
        runtime = ProcessRuntime(self.hass, entry)
        self.runtimes.append(runtime)
        await runtime.async_start()
        runtime._ready_at = 0
        return runtime

    def shared_config(self):
        return {
            "process_type": "shared_fan",
            "lights": ["switch.room_light", "switch.other_light"],
            "boost_fans": ["switch.bath_fan"],
            "output": "switch.shared_fan",
            "fan_on_delay": 120,
            "fan_off_delay": 0,
        }

    def humidity_config(self):
        return {
            "process_type": "humidity",
            "room_light": "switch.room_light",
            "humidity_sensor": "sensor.humidity",
            "output": "switch.bath_fan",
        }

    async def test_motion_gate_persistence_and_unsubscribe(self):
        runtime = await self.create()
        self.hass.states.async_set("binary_sensor.motion", "on")
        await self.hass.async_block_till_done()
        self.assertEqual(self.commands, [])
        await runtime.async_set_enabled(True)
        await self.hass.async_block_till_done()
        self.assertEqual(self.commands, [("turn_on", "switch.room_light")])
        await runtime.async_stop()
        self.hass.states.async_set("binary_sensor.motion", "off")
        await self.hass.async_block_till_done()
        self.assertIsNone(runtime._timer)
        self.assertEqual(len(self.commands), 1)
        restored = await self.create(entry_id=runtime.entry.entry_id)
        self.assertTrue(restored.enabled)
        await restored.async_set_enabled(False)
        self.assertEqual(len(self.commands), 1)

    async def test_shared_fan_boost_ignores_light_delay_and_releases(self):
        runtime = await self.create(self.shared_config())
        await runtime.async_set_enabled(True)
        self.hass.states.async_set("switch.bath_fan", "on")
        await self.hass.async_block_till_done()
        await runtime.async_evaluate()
        self.assertIn(("turn_on", "switch.shared_fan"), self.commands)
        self.hass.states.async_set("switch.bath_fan", "off")
        await self.hass.async_block_till_done()
        await runtime.async_evaluate()
        self.assertEqual(self.commands[-1], ("turn_off", "switch.shared_fan"))

    async def test_common_fan_is_only_commanded_by_its_owner(self):
        humidity = await self.create(self.humidity_config())
        shared = await self.create(self.shared_config())
        await humidity.async_set_enabled(True)
        await shared.async_set_enabled(True)
        self.hass.states.async_set("switch.room_light", "on")
        await self.hass.async_block_till_done()
        now = self.hass.loop.time()
        humidity.logic.history.extend([(now - 120, 40), (now - 60, 41)])
        humidity.logic.last_sample = now - 60
        self.hass.states.async_set("sensor.humidity", "42")
        await self.hass.async_block_till_done()
        await humidity.async_evaluate()
        await self.hass.async_block_till_done()
        await shared.async_evaluate()
        self.assertIn(("turn_on", "switch.bath_fan"), self.commands)
        self.assertIn(("turn_on", "switch.shared_fan"), self.commands)
        self.assertEqual(humidity.config["output"], "switch.bath_fan")
        await shared.async_set_enabled(False)
        self.hass.states.async_set("switch.shared_fan", "off")
        await humidity.async_evaluate()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get("switch.shared_fan").state, "off")

    async def test_feedback_timeout_blocks_repeated_commands(self):
        async def no_feedback(call):
            self.commands.append((call.service, call.data["entity_id"]))

        self.hass.services.async_register("switch", "turn_on", no_feedback)
        runtime = await self.create()
        self.hass.states.async_set("binary_sensor.motion", "on")
        await runtime.async_set_enabled(True)
        await self.hass.async_block_till_done()
        runtime._feedback_due = self.hass.loop.time() - 1
        for _ in range(3):
            await runtime.async_evaluate()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(runtime.last_error, "feedback_timeout")
        self.assertTrue(runtime.attributes["commands_blocked"])

    async def test_disable_waits_for_service_and_discards_queued_work(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(call):
            self.commands.append((call.service, call.data["entity_id"]))
            entered.set()
            await release.wait()

        self.hass.services.async_register("switch", "turn_on", blocked)
        runtime = await self.create()
        self.hass.states.async_set("binary_sensor.motion", "on")
        await runtime.async_set_enabled(True)
        await entered.wait()
        ticket = runtime._generation
        queued = asyncio.create_task(runtime.async_evaluate(ticket))
        stopping = asyncio.create_task(runtime.async_set_enabled(False))
        await asyncio.sleep(0)
        self.assertFalse(runtime.enabled)
        release.set()
        await asyncio.gather(stopping, queued)
        self.assertEqual(len(self.commands), 1)
        self.assertIsNone(runtime._expected)
        self.assertIsNone(runtime._timer)

    async def test_storage_failure_never_enables_controller(self):
        runtime = await self.create()
        with patch.object(runtime.store, "async_save", AsyncMock(side_effect=OSError("disk"))):
            with self.assertRaises(OSError):
                await runtime.async_set_enabled(True)
        self.assertFalse(runtime.enabled)
        self.assertEqual(runtime.last_error, "storage_failed")

    async def test_humidity_sensor_loss_still_stops_after_fallback(self):
        runtime = await self.create(self.humidity_config())
        self.hass.states.async_set("switch.bath_fan", "on")
        self.hass.states.async_set("sensor.humidity", "unavailable")
        await runtime.async_set_enabled(True)
        await self.hass.async_block_till_done()
        runtime.logic.uncertain_since = self.hass.loop.time() - 1201
        await runtime.async_evaluate()
        self.assertEqual(self.commands, [("turn_off", "switch.bath_fan")])

    async def test_config_forms_ownership_and_redaction(self):
        from custom_components.home_control.config_flow import HomeControlConfigFlow, _validate
        from custom_components.home_control.diagnostics import async_get_config_entry_diagnostics
        from custom_components.home_control.process_config import process_schema, validate_process

        fake = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        flow = HomeControlConfigFlow()
        flow.hass = fake
        form = await flow.async_step_humidity()
        self.assertEqual(form["type"], "form")
        submitted = process_schema("humidity", {}, name=True)(
            {k: v for k, v in self.humidity_config().items() if k != "process_type"}
            | {"name": "Humidity"}
        )
        result = await flow.async_step_humidity(submitted)
        self.assertEqual(result["type"], "create_entry")
        _, errors = validate_process(fake, "humidity", submitted | {"minimum_samples": 6})
        self.assertEqual(errors["base"], "invalid_parameters")
        runtime = await self.create(self.humidity_config())
        runtime.entry.runtime_data = runtime
        fake.config_entries.async_entries = lambda domain: [runtime.entry]
        _, errors = validate_process(
            fake,
            "motion",
            {"output": "switch.bath_fan", "motion_sensors": ["binary_sensor.motion"]},
        )
        self.assertEqual(errors["output"], "groups_in_use")
        _, errors = _validate(
            fake,
            {"group_1": "switch.bath_fan", "mqtt_topic": "test/RESULT", "mqtt_button": "Button1"},
        )
        self.assertEqual(errors["base"], "groups_in_use")
        diagnostic = await async_get_config_entry_diagnostics(self.hass, runtime.entry)
        self.assertNotEqual(diagnostic["config"]["output"], "switch.bath_fan")
        self.assertNotEqual(diagnostic["config"]["humidity_sensor"], "sensor.humidity")

    async def test_real_motion_platform_load_and_unload(self):
        from test_ha_runtime import HomeAssistantRuntimeTests

        await HomeAssistantRuntimeTests.test_config_entry_loads_real_switch_and_button_platforms(
            self
        )

    async def test_real_shared_platform_load_and_unload(self):
        from test_ha_runtime import HomeAssistantRuntimeTests

        self.config = self.shared_config()
        await HomeAssistantRuntimeTests.test_config_entry_loads_real_switch_and_button_platforms(
            self
        )

    async def test_real_humidity_platform_load_and_unload(self):
        from test_ha_runtime import HomeAssistantRuntimeTests

        self.config = self.humidity_config()
        await HomeAssistantRuntimeTests.test_config_entry_loads_real_switch_and_button_platforms(
            self
        )

    async def test_humidity_stop_leaves_shared_fan_to_its_own_controller(self):
        humidity = await self.create(self.humidity_config())
        shared = await self.create(self.shared_config())
        self.hass.states.async_set("switch.bath_fan", "on")
        self.hass.states.async_set("switch.shared_fan", "on")
        await humidity.async_set_enabled(True)
        await shared.async_set_enabled(True)
        await self.hass.async_block_till_done()
        now = self.hass.loop.time()
        humidity.logic.history.clear()
        humidity.logic.history.extend([(now - 180, 50), (now - 120, 50), (now - 60, 50)])
        humidity.logic.stable = 3
        humidity.logic.stable_since = now - 301
        humidity.logic.last_sample = now - 61
        humidity.logic.last_report = None
        await humidity.async_evaluate()
        await self.hass.async_block_till_done()
        await shared.async_evaluate()
        self.assertEqual(
            self.commands, [("turn_off", "switch.bath_fan"), ("turn_off", "switch.shared_fan")]
        )
