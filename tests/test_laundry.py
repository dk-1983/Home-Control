"""Washer completion, offline behavior and persisted voice reminders."""

import importlib.util
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant required")
class LaundryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.laundry import LaundryRuntime

        self.temp = TemporaryDirectory(dir=Path(__file__).parents[1] / "work")
        self.hass = HomeAssistant(self.temp.name)
        self.entry = SimpleNamespace(
            entry_id="laundry",
            title="Стирка",
            options={},
            data={
                "process_type": "laundry",
                "washer_status": "sensor.washer",
                "washer_notification": "event.washer",
                "washer_error": "event.error",
                "voice_info": True,
                "voice_error": True,
                "manual_acknowledge": True,
            },
        )
        self.runtime = LaundryRuntime(self.hass, self.entry)
        self.runtime.store.async_load = AsyncMock(return_value=None)
        self.runtime.store.async_save = AsyncMock()
        self.center = SimpleNamespace(
            enabled=True,
            route=Mock(return_value=(["media_player.one"], 0.3)),
            submit=Mock(),
            invalidate=Mock(),
        )
        self.runtime.bus.center = self.center
        await self.runtime.async_start()
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_wait_idle()

    async def asyncTearDown(self):
        await self.runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def report(self, entity, value, attrs=None):
        self.hass.states.async_set(entity, value, attrs or {})
        await self.hass.async_block_till_done()

    async def complete(self):
        from homeassistant.util import dt as dt_util

        await self.report(
            "event.washer", dt_util.utcnow().isoformat(), {"event_type": "washing_is_complete"}
        )

    async def test_complete_offline_dedup_ack(self):
        await self.report("sensor.washer", "running")
        await self.complete()
        await self.report("sensor.washer", "end")
        self.assertTrue(self.runtime.pending)
        self.assertEqual(self.center.submit.call_count, 1)
        for value in ("power_off", "unknown", "unavailable"):
            await self.report("sensor.washer", value)
            await self.report("event.error", value)
        self.assertEqual(self.center.submit.call_count, 1)
        self.assertTrue(self.runtime.pending)
        await self.runtime.async_press()
        self.assertFalse(self.runtime.pending)
        await self.complete()
        self.assertFalse(self.runtime.pending)
        self.center.invalidate.assert_called_with("laundry", "laundry_waiting")

    async def test_status_completion_requires_cycle(self):
        await self.report("sensor.washer", "end")
        self.assertFalse(self.runtime.pending)
        await self.report("sensor.washer", "running")
        await self.report("sensor.washer", "pause")
        await self.report("sensor.washer", "unavailable")
        self.assertFalse(self.runtime.pending)
        await self.report("sensor.washer", "end")
        self.assertTrue(self.runtime.pending)

    async def test_stale_and_restored_events_ignored(self):
        from homeassistant.util import dt as dt_util

        await self.report(
            "event.washer",
            (dt_util.utcnow() - timedelta(days=1)).isoformat(),
            {"event_type": "washing_is_complete"},
        )
        await self.report(
            "event.washer",
            dt_util.utcnow().isoformat(),
            {"event_type": "washing_is_complete", "restored": True},
        )
        self.assertFalse(self.runtime.pending)
        self.center.submit.assert_not_called()

    async def test_silence_defers_one_even_without_repeats(self):
        self.runtime.config["reminders"] = False
        self.center.route.return_value = ([], 0.3)
        await self.complete()
        for _ in range(3):
            await self.runtime._run(self.runtime._generation)
        self.center.submit.assert_not_called()
        self.center.route.return_value = (["media_player.one"], 0.3)
        await self.runtime._run(self.runtime._generation)
        await self.runtime._run(self.runtime._generation)
        self.assertEqual(self.center.submit.call_count, 1)

    async def test_repeat_and_disable_cancel(self):
        from homeassistant.util import dt as dt_util

        await self.complete()
        self.runtime.next_reminder = (dt_util.utcnow() - timedelta(seconds=1)).isoformat()
        await self.runtime._run(self.runtime._generation)
        self.assertEqual(self.center.submit.call_count, 2)
        old_generation = self.runtime._generation
        await self.runtime.async_set_enabled(False)
        await self.runtime._run(old_generation)
        self.assertTrue(self.runtime.pending)
        self.center.invalidate.assert_called_with("laundry")
        self.assertEqual(self.center.submit.call_count, 2)

    async def test_persisted_wait_survives_restart_without_duplicate(self):
        from custom_components.home_control.laundry import LaundryRuntime

        await self.complete()
        stored = self.runtime.store.async_save.call_args.args[0]
        await self.runtime.async_stop()
        self.runtime = LaundryRuntime(self.hass, self.entry)
        self.runtime.store.async_load = AsyncMock(return_value=stored)
        self.runtime.store.async_save = AsyncMock()
        await self.runtime.async_start()
        await self.runtime._run(self.runtime._generation)
        self.assertTrue(self.runtime.pending)
        self.assertTrue(self.runtime.enabled)
        self.assertEqual(self.center.submit.call_count, 1)

    async def test_new_error_only_and_no_generic_duplicate(self):
        from homeassistant.util import dt as dt_util

        stamp = dt_util.utcnow().isoformat()
        await self.report("event.error", stamp, {"event_type": "water_drain_error"})
        await self.report("event.error", "unavailable")
        await self.report("event.error", stamp, {"event_type": "water_drain_error"})
        await self.report(
            "event.washer", dt_util.utcnow().isoformat(), {"event_type": "error_during_washing"}
        )
        self.assertEqual(self.center.submit.call_count, 1)
        self.assertEqual(self.center.submit.call_args.args[0].level, "ERROR")

    async def test_storage_failure_suppresses_speech(self):
        self.runtime.store.async_save.side_effect = OSError("disk")
        await self.complete()
        self.assertFalse(self.runtime.enabled)
        self.assertEqual(self.runtime.last_error, "storage_failed")
        self.center.submit.assert_not_called()

    async def test_schema_and_duplicate(self):
        from custom_components.home_control.laundry_config import schema, validate

        fields = schema({}, name=True)(
            {
                "name": "Laundry",
                "washer_status": "sensor.washer",
                "washer_notification": "event.washer",
            }
        )
        self.assertEqual(fields["voice_scope"], "all")
        self.assertNotIn("voice_repeat", fields)
        for entity in ("sensor.washer", "event.washer"):
            await self.report(entity, "unknown")
        self.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [])
        data, errors = validate(self.hass, fields)
        self.assertFalse(errors)
        self.assertEqual(data["washer_error"], "")
        self.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [self.entry])
        _, errors = validate(self.hass, fields)
        self.assertEqual(errors["base"], "laundry_in_use")
        _, errors = validate(self.hass, fields, exclude_id=self.entry.entry_id)
        self.assertFalse(errors)
        self.hass.config_entries = None

    async def test_no_equipment_calls(self):
        with patch.object(type(self.hass.services), "async_call", new_callable=AsyncMock) as call:
            await self.report("sensor.washer", "running")
            await self.complete()
            await self.runtime.async_press()
            await self.runtime.async_set_enabled(False)
            call.assert_not_called()

    async def test_automatic_acknowledgement_on_power_off_or_lost_wifi(self):
        self.runtime.config["manual_acknowledge"] = False
        for offline in ("power_off", "unknown", "unavailable"):
            await self.report("sensor.washer", "initial")
            await self.report("sensor.washer", "running")
            await self.report("sensor.washer", "end")
            self.assertTrue(self.runtime.pending)
            await self.report("sensor.washer", offline)
            self.assertFalse(self.runtime.pending)
            await self.complete()
            self.assertFalse(self.runtime.pending)

    async def test_new_cycle_after_offline_gap(self):
        await self.report("sensor.washer", "running")
        await self.complete()
        await self.runtime.async_press()
        await self.report("sensor.washer", "power_off")
        await self.report("sensor.washer", "unavailable")
        await self.report("sensor.washer", "running")
        await self.report("sensor.washer", "end")
        self.assertTrue(self.runtime.pending)

    async def test_optional_button(self):
        from custom_components.home_control.button import async_setup_entry

        self.entry.runtime_data = self.runtime
        add = Mock()
        self.runtime.config["manual_acknowledge"] = False
        await async_setup_entry(self.hass, self.entry, add)
        add.assert_not_called()
        self.runtime.config["manual_acknowledge"] = True
        await async_setup_entry(self.hass, self.entry, add)
        self.assertEqual(add.call_args.args[0][0].translation_key, "laundry_acknowledge")
