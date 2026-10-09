"""Refrigerator door timing and optional filter announcements."""

import asyncio
import importlib.util
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant required")
class FridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.fridge import FridgeRuntime

        self.temp = TemporaryDirectory(dir=Path(__file__).parents[1] / "work")
        self.hass = HomeAssistant(self.temp.name)
        self.entry = SimpleNamespace(
            entry_id="fridge",
            title="Холодильник",
            options={},
            data={
                "process_type": "fridge",
                "fridge_door": "binary_sensor.door",
                "fridge_notification": "event.fridge",
                "voice_warning": True,
                "voice_info": True,
                "announce_closed": True,
            },
        )
        self.runtime = FridgeRuntime(self.hass, self.entry)
        self.runtime.store.async_load = AsyncMock(return_value=None)
        self.runtime.store.async_save = AsyncMock()
        self.center = SimpleNamespace(
            enabled=True,
            route=Mock(return_value=(["media_player.kitchen"], 0.3)),
            submit=Mock(),
            invalidate=Mock(),
        )
        self.runtime.bus.center = self.center
        self.hass.states.async_set("binary_sensor.door", "off")
        await self.runtime.async_start()
        await self.runtime.async_set_enabled(True)

    async def asyncTearDown(self):
        await self.runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def door(self, value):
        self.hass.states.async_set("binary_sensor.door", value)
        await self.hass.async_block_till_done()

    def due(self):
        self.runtime._opened_at = self.hass.loop.time() - self.runtime.config["door_delay"] - 1
        self.runtime._tick(None)

    async def notify(self, kind, stamp=None):
        from homeassistant.util import dt as dt_util

        self.hass.states.async_set(
            "event.fridge", stamp or dt_util.utcnow().isoformat(), {"event_type": kind}
        )
        await self.hass.async_block_till_done()

    async def test_short_open_silent_and_delayed_warning(self):
        await self.door("on")
        self.runtime._tick(None)
        await self.door("off")
        self.center.submit.assert_not_called()
        await self.door("on")
        self.due()
        notice = self.center.submit.call_args.args[0]
        self.assertEqual(notice.level, "WARNING")
        self.runtime._tick(None)
        self.assertEqual(self.center.submit.call_count, 1)
        await self.door("off")
        self.assertEqual(self.center.submit.call_args.args[0].key, "door_closed")
        self.assertFalse(self.runtime._warned)

    async def test_repeat_optional(self):
        await self.door("on")
        self.due()
        self.runtime._last_warning -= 301
        self.runtime.config["door_repeat"] = False
        self.runtime._tick(None)
        self.assertEqual(self.center.submit.call_count, 1)
        self.runtime.config["door_repeat"] = True
        self.runtime._tick(None)
        self.assertEqual(self.center.submit.call_count, 2)

    async def test_unknown_cancels_and_restarts_delay(self):
        await self.door("on")
        self.due()
        await self.door("unavailable")
        self.center.invalidate.assert_called_with("fridge", "door_open")
        self.runtime._tick(None)
        await self.door("on")
        self.runtime._tick(None)
        self.assertEqual(self.center.submit.call_count, 1)
        self.due()
        self.assertEqual(self.center.submit.call_count, 2)

    async def test_silent_interval_keeps_one_warning_not_backlog(self):
        self.center.route.return_value = ([], 0.3)
        await self.door("on")
        self.due()
        self.runtime._tick(None)
        self.center.submit.assert_not_called()
        self.center.route.return_value = (["media_player.kitchen"], 0.3)
        self.runtime._tick(None)
        self.runtime._tick(None)
        self.assertEqual(self.center.submit.call_count, 1)

    async def test_rapid_close_reopen_resets_timer(self):
        await self.door("on")
        self.runtime._opened_at -= 110
        self.hass.states.async_set("binary_sensor.door", "off")
        self.hass.states.async_set("binary_sensor.door", "on")
        await self.hass.async_block_till_done()
        self.assertLess(self.hass.loop.time() - self.runtime._opened_at, 5)

    async def test_filters_off_by_default_and_no_door_event_duplicate(self):
        for kind in (
            "time_to_change_filter",
            "time_to_change_water_filter",
            "door_is_open",
            "frozen_is_complete",
        ):
            await self.notify(kind)
        self.center.submit.assert_not_called()
        self.assertFalse(self.runtime.pending_filters)

    async def test_deodorizer_text_and_replayed_event(self):
        from homeassistant.util import dt as dt_util

        self.runtime.config["deodorizer"] = True
        stamp = dt_util.utcnow().isoformat()
        await self.notify("time_to_change_filter", stamp)
        self.assertIn("Не мойте его водой", self.center.submit.call_args.args[0].message)
        self.hass.states.async_set("event.fridge", "unavailable")
        await self.hass.async_block_till_done()
        await self.notify("time_to_change_filter", stamp)
        self.assertEqual(self.center.submit.call_count, 1)

    async def test_filter_pending_persisted_and_reset_cancels(self):
        from custom_components.home_control.fridge import FridgeRuntime

        self.runtime.config["deodorizer"] = True
        self.entry.data["deodorizer"] = True
        self.center.route.return_value = ([], 0.3)
        await self.notify("time_to_change_filter")
        saved = self.runtime.store.async_save.call_args.args[0]
        await self.runtime.async_stop()
        self.runtime = FridgeRuntime(self.hass, self.entry)
        self.runtime.store.async_load = AsyncMock(return_value=saved)
        self.runtime.store.async_save = AsyncMock()
        await self.runtime.async_start()
        self.assertEqual(self.runtime.pending_filters, {"deodorizer"})
        await self.notify("filter_reset_complete")
        self.assertFalse(self.runtime.pending_filters)
        self.center.route.return_value = (["media_player.kitchen"], 0.3)
        self.runtime._tick(None)
        await self.hass.async_block_till_done()
        self.center.submit.assert_not_called()

    async def test_old_events_and_restored_door_ignored(self):
        from homeassistant.util import dt as dt_util

        self.runtime.config["water_filter"] = True
        await self.notify(
            "time_to_change_water_filter", (dt_util.utcnow() - timedelta(days=1)).isoformat()
        )
        self.hass.states.async_set("binary_sensor.door", "on", {"restored": True})
        await self.hass.async_block_till_done()
        self.runtime._tick(None)
        self.assertIsNone(self.runtime._opened_at)
        self.center.submit.assert_not_called()

    async def test_disable_cancels_queued_work_and_never_controls_appliance(self):
        with patch.object(type(self.hass.services), "async_call", new_callable=AsyncMock) as call:
            await self.door("on")
            self.due()
            generation = self.runtime._generation
            await self.runtime.async_set_enabled(False)
            await self.runtime._filters(generation)
            self.runtime._tick(None)
            self.center.invalidate.assert_called_with("fridge", None)
            self.assertEqual(self.center.submit.call_count, 1)
            self.assertFalse(self.runtime.store.async_save.call_args.args[0]["enabled"])
            call.assert_not_called()

    async def test_disable_during_event_storage_prevents_delivery(self):
        self.runtime.config["deodorizer"] = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def save(data):
            entered.set()
            await release.wait()

        self.runtime.store.async_save.side_effect = save
        from homeassistant.util import dt as dt_util

        self.hass.states.async_set(
            "event.fridge", dt_util.utcnow().isoformat(), {"event_type": "time_to_change_filter"}
        )
        await entered.wait()
        task = asyncio.create_task(self.runtime.async_set_enabled(False))
        await asyncio.sleep(0)
        release.set()
        await task
        await self.hass.async_block_till_done()
        self.center.submit.assert_not_called()

    async def test_flow_and_duplicate_validation(self):
        from custom_components.home_control.config_flow import HomeControlConfigFlow
        from custom_components.home_control.fridge_config import validate

        self.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [])
        try:
            flow = HomeControlConfigFlow()
            flow.hass = self.hass
            form = await flow.async_step_fridge()
            values = form["data_schema"]({"name": "Fridge", "fridge_door": "binary_sensor.door"})
            self.assertFalse(values["deodorizer"])
            result = await flow.async_step_fridge(values)
            self.assertEqual(result["data"]["process_type"], "fridge")
            _, errors = validate(self.hass, values | {"deodorizer": True})
            self.assertIn("fridge_notification", errors)
            self.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [self.entry])
            _, errors = validate(self.hass, values)
            self.assertEqual(errors["base"], "fridge_in_use")
        finally:
            self.hass.config_entries = None
