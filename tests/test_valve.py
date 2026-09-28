"""Valve exercise state-machine, storage, calendar and interlock regressions."""

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant required")
class ValveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.valve import ValveRuntime

        self.temp = TemporaryDirectory(dir=Path(__file__).parents[1] / "work")
        self.hass = HomeAssistant(self.temp.name)
        self.config = dict(
            process_type="valve_exercise",
            valves=["switch.hot", "switch.cold"],
            leak_sensors=["binary_sensor.leak1", "binary_sensor.leak2"],
            schedule_day=1,
            schedule_time="03:00:00",
            retry_hours=1,
            movement_timeout=0.02,
            closed_hold=0.001,
            between_valves=0,
        )
        self.entry = SimpleNamespace(
            entry_id="valves", title="Valves", data=self.config, options={}
        )
        self.commands = []
        self.feedback = True
        self.extra_runtimes = []
        for entity in self.config["valves"] + self.config["leak_sensors"]:
            self.hass.states.async_set(entity, "off")

        async def service(call):
            self.commands.append((call.service, call.data["entity_id"]))
            if self.feedback:
                self.hass.states.async_set(
                    call.data["entity_id"], "on" if call.service == "turn_on" else "off"
                )

        self.service = service
        self.hass.services.async_register("switch", "turn_on", service)
        self.hass.services.async_register("switch", "turn_off", service)
        self.runtime = ValveRuntime(self.hass, self.entry)
        await self.runtime.async_start()

    async def asyncTearDown(self):
        for runtime in self.extra_runtimes + [self.runtime]:
            await runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def enable(self):
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_wait_idle()

    async def run_manual(self):
        await self.runtime.async_press()
        await self.runtime.async_wait_idle()

    async def test_default_disabled_then_sequential_complete(self):
        await self.run_manual()
        self.assertEqual(self.commands, [])
        await self.enable()
        scheduled = self.runtime.next_due
        await self.run_manual()
        self.assertEqual(
            self.commands,
            [
                ("turn_on", "switch.hot"),
                ("turn_off", "switch.hot"),
                ("turn_on", "switch.cold"),
                ("turn_off", "switch.cold"),
            ],
        )
        self.assertEqual(self.runtime.next_due, scheduled)
        self.assertTrue(
            all(
                r["status"] == "completed" and r["last_success"]
                for r in self.runtime.records.values()
            )
        )

    async def test_closed_deferred_then_completed_without_repeating_other(self):
        self.hass.states.async_set("switch.hot", "on")
        await self.enable()
        await self.run_manual()
        record = self.runtime.records["switch.hot"]
        self.assertEqual(record["status"], "pending")
        self.assertEqual(len(self.commands), 2)
        self.hass.states.async_set("switch.hot", "off")
        record["retry_at"] = "2020-01-01T00:00:00+00:00"
        self.commands.clear()
        await self.runtime._run(self.runtime._generation, False)
        self.assertEqual(self.commands, [("turn_on", "switch.hot"), ("turn_off", "switch.hot")])

    async def test_no_sensor_reads_when_closed_or_retry_not_due(self):
        for entity in self.config["valves"]:
            self.hass.states.async_set(entity, "on")
        await self.hass.async_block_till_done()
        await self.enable()
        with patch.object(
            self.runtime, "_snapshot_protection", side_effect=AssertionError("Unexpected selection")
        ):
            await self.run_manual()
            await self.runtime._run(self.runtime._generation, False)
        self.assertEqual(self.commands, [])
        self.assertTrue(all(r["status"] == "pending" for r in self.runtime.records.values()))

    async def test_leak_and_unknown_block_start_and_aggregate(self):
        from custom_components.home_control.binary_sensor import GroupLeakSensor

        sensor = GroupLeakSensor(self.runtime)
        self.hass.states.async_set("binary_sensor.leak1", "unavailable")
        await self.hass.async_block_till_done()
        self.assertIsNone(sensor.is_on)
        await self.enable()
        await self.run_manual()
        self.assertEqual(self.commands, [])
        self.hass.states.async_set("binary_sensor.leak2", "on")
        await self.hass.async_block_till_done()
        self.assertTrue(sensor.is_on)
        await self.run_manual()
        self.assertEqual(self.commands, [])

    async def test_leak_during_hold_latches_even_if_it_clears(self):
        async def close(call):
            await self.service(call)
            self.hass.states.async_set("binary_sensor.leak1", "on")
            self.hass.states.async_set("binary_sensor.leak1", "off")
            await asyncio.sleep(0)

        self.hass.services.async_register("switch", "turn_on", close)
        await self.enable()
        await self.run_manual()
        self.assertFalse(any(name == "turn_off" for name, _ in self.commands))
        self.assertTrue(all(r["status"] == "interrupted" for r in self.runtime.records.values()))

    async def test_missing_feedback_blocks_open_and_does_not_retry_failed(self):
        self.feedback = False
        await self.enable()
        await self.run_manual()
        self.assertEqual(self.commands, [("turn_on", "switch.hot"), ("turn_on", "switch.cold")])
        self.assertTrue(
            all(r["error"] == "movement_timeout" for r in self.runtime.records.values())
        )
        await self.runtime._run(self.runtime._generation, False)
        self.assertEqual(len(self.commands), 2)

    async def test_service_error_on_one_valve_does_not_block_other(self):
        async def close(call):
            if call.data["entity_id"] == "switch.hot":
                raise RuntimeError("device offline")
            await self.service(call)

        self.hass.services.async_register("switch", "turn_on", close)
        await self.enable()
        await self.run_manual()
        self.assertEqual(self.runtime.records["switch.hot"]["status"], "failed")
        self.assertEqual(self.runtime.records["switch.cold"]["status"], "completed")

    async def test_disable_after_close_never_reopens(self):
        async def close(call):
            await self.service(call)
            self.runtime.set_enabled(False)

        self.hass.services.async_register("switch", "turn_on", close)
        await self.enable()
        await self.run_manual()
        self.assertEqual(self.commands, [("turn_on", "switch.hot")])
        self.assertEqual(self.runtime.records["switch.hot"]["status"], "interrupted")

    async def test_restart_retains_pending_and_interrupted_without_commands(self):
        from custom_components.home_control.valve import ValveRuntime

        await self.enable()
        self.runtime.records["switch.hot"].update(status="closing", due="2026-09-01T03:00:00+00:00")
        self.runtime.records["switch.cold"].update(
            status="pending", due="2026-09-01T03:00:00+00:00", retry_at="2026-10-01T03:00:00+00:00"
        )
        await self.runtime._save()
        await self.runtime.async_stop()
        self.runtime = ValveRuntime(self.hass, self.entry)
        await self.runtime.async_start()
        self.assertEqual(self.commands, [])
        self.assertTrue(self.runtime.enabled)
        self.assertEqual(self.runtime.records["switch.hot"]["status"], "interrupted")
        self.assertEqual(self.runtime.records["switch.cold"]["status"], "pending")

    async def test_monthly_calendar_does_not_drift_and_overdue_not_duplicated(self):
        from homeassistant.util import dt as dt_util

        from custom_components.home_control.valve_config import next_schedule

        now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        self.assertEqual(
            next_schedule(now, self.config), datetime(2026, 10, 1, 3, tzinfo=timezone.utc)
        )
        self.assertEqual(
            next_schedule(
                datetime(2026, 2, 1, tzinfo=timezone.utc), dict(self.config, schedule_day=31)
            ),
            datetime(2026, 2, 28, 3, tzinfo=timezone.utc),
        )
        await self.enable()
        for entity in self.config["valves"]:
            self.hass.states.async_set(entity, "on")
        self.runtime.next_due = (dt_util.now() - timedelta(days=40)).isoformat()
        due = self.runtime.next_due
        await self.runtime._run(self.runtime._generation, False)
        self.assertTrue(all(r["due"] == due for r in self.runtime.records.values()))
        self.assertGreater(dt_util.parse_datetime(self.runtime.next_due), dt_util.now())
        self.runtime.next_due = (dt_util.now() - timedelta(days=1)).isoformat()
        await self.runtime._run(self.runtime._generation, False)
        self.assertTrue(all(r["due"] == due for r in self.runtime.records.values()))

    async def test_global_serialization_and_duplicate_manual_run(self):
        from custom_components.home_control.valve import ValveRuntime

        config = dict(self.config, valves=["switch.third"])
        self.hass.states.async_set("switch.third", "off")
        other = ValveRuntime(
            self.hass, SimpleNamespace(entry_id="other", title="Other", data=config, options={})
        )
        self.extra_runtimes.append(other)
        await other.async_start()
        await self.enable()
        await other.async_set_enabled(True)
        await other.async_wait_idle()
        await self.runtime.async_press()
        task = self.runtime._task
        await self.runtime.async_press()
        self.assertIs(task, self.runtime._task)
        await other.async_press()
        await asyncio.gather(self.runtime.async_wait_idle(), other.async_wait_idle())
        for index in range(0, len(self.commands), 2):
            close, opened = self.commands[index : index + 2]
            self.assertEqual(close, ("turn_on", opened[1]))
            self.assertEqual(opened[0], "turn_off")

    async def test_real_platform_load_and_unload(self):
        from test_ha_runtime import HomeAssistantRuntimeTests

        await HomeAssistantRuntimeTests.test_config_entry_loads_real_switch_and_button_platforms(
            self
        )

    async def test_configuration_rejects_shared_valves_and_empty_sensors(self):
        from custom_components.home_control.valve_config import schema, validate

        other = SimpleNamespace(entry_id="other", data=self.config, options={})
        fake = SimpleNamespace(
            states=self.hass.states,
            config_entries=SimpleNamespace(async_entries=lambda domain: [other]),
        )
        _, errors = validate(fake, self.config)
        self.assertEqual(errors["valves"], "groups_in_use")
        _, errors = validate(fake, dict(self.config, leak_sensors=[]), exclude_id="other")
        self.assertEqual(errors["leak_sensors"], "valve_entities")
        values = schema({})(
            {"valves": self.config["valves"], "leak_sensors": self.config["leak_sensors"]}
        )
        self.assertEqual(values["retry_hours"], 1)
        self.assertEqual(values["schedule_time"], "03:00:00")

    async def test_storage_failure_before_close_sends_no_commands(self):
        from unittest.mock import AsyncMock

        await self.enable()
        with patch.object(
            self.runtime.store, "async_save", AsyncMock(side_effect=OSError("disk failed"))
        ):
            await self.run_manual()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.runtime.last_error, "storage_failed")

    async def test_unexpected_position_during_hold_never_reopens(self):
        async def close(call):
            await self.service(call)
            self.hass.loop.call_later(
                0.005, self.hass.states.async_set, call.data["entity_id"], "off"
            )

        self.runtime.config["closed_hold"] = 0.02
        self.hass.services.async_register("switch", "turn_on", close)
        await self.enable()
        await self.run_manual()
        self.assertFalse(any(name == "turn_off" for name, _ in self.commands))
        self.assertTrue(all(r["status"] == "failed" for r in self.runtime.records.values()))

    async def test_transient_storage_failure_stops_rest_of_group(self):
        await self.enable()
        save = self.runtime.store.async_save
        calls = 0

        async def fail_once(data):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("disk failed after closing")
            await save(data)

        with patch.object(self.runtime.store, "async_save", fail_once):
            await self.run_manual()
        self.assertEqual(self.commands, [("turn_on", "switch.hot")])
        self.assertEqual(self.runtime.last_error, "storage_failed")

    async def test_config_and_options_flow_keep_group_settings(self):
        from custom_components.home_control.config_flow import (
            HomeControlConfigFlow,
            HomeControlOptionsFlow,
        )
        from custom_components.home_control.valve_config import schema

        fake = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        values = schema({}, name=True)(
            {
                "name": "Room",
                "valves": self.config["valves"],
                "leak_sensors": self.config["leak_sensors"],
            }
        )
        flow = HomeControlConfigFlow()
        flow.hass = fake
        result = await flow.async_step_valve_exercise(values)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["process_type"], "valve_exercise")
        entry = SimpleNamespace(entry_id="room", data=result["data"], options={})

        class Options(HomeControlOptionsFlow):
            @property
            def config_entry(self):
                return entry

        options = Options()
        options.hass = fake
        form = await options.async_step_init()
        self.assertEqual(form["step_id"], "valve_exercise")
        values.pop("name")
        values["retry_hours"] = 2
        result = await options.async_step_valve_exercise(values)
        self.assertEqual(result["data"]["retry_hours"], 2)
        self.assertEqual(result["data"]["schedule_time"], "03:00:00")
