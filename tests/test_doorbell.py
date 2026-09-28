"""Doorbell input, schedule and independent speaker regressions."""

import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant required")
class DoorbellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.doorbell import DoorbellRuntime

        self.temp = TemporaryDirectory(dir=Path(__file__).parents[1] / "work")
        self.hass = HomeAssistant(self.temp.name)
        self.config = dict(
            process_type="doorbell",
            source="virtual",
            speakers=["media_player.test", "media_player.other"],
            media_url="http://localhost/local/bell.mp3",
            volume=0.4,
            sound_duration=0.001,
            cooldown=10,
            night_intervals=[],
        )
        self.entry = SimpleNamespace(entry_id="bell", title="Bell", data=self.config, options={})
        self.runtime = DoorbellRuntime(self.hass, self.entry)
        self.calls = []

        async def service(call):
            self.calls.append((call.service, dict(call.data)))
            if call.service == "volume_set":
                self.hass.states.async_set(
                    call.data["entity_id"], "idle", {"volume_level": call.data["volume_level"]}
                )

        for name in ("play_media", "volume_set"):
            self.hass.services.async_register("media_player", name, service)
        for entity in self.config["speakers"]:
            self.hass.states.async_set(entity, "idle", {"volume_level": 0.6})
        await self.runtime.async_start()

    async def asyncTearDown(self):
        await self.runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def test_disabled_start_and_multiple_speakers_restore(self):
        await self.runtime.async_press()
        self.assertEqual(self.calls, [])
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        self.assertEqual(len([c for c in self.calls if c[0] == "play_media"]), 2)
        self.assertEqual(self.hass.states.get("media_player.test").attributes["volume_level"], 0.6)
        await self.runtime.async_press()
        self.assertEqual(len(self.calls), 6)

    async def test_failed_speaker_does_not_block_other(self):
        self.hass.states.async_set("media_player.test", "unavailable")
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        self.assertEqual(self.runtime.failed_speakers, 1)
        self.assertEqual(len([c for c in self.calls if c[0] == "play_media"]), 1)

    async def test_night_silent_and_selected_speaker(self):
        from custom_components.home_control.doorbell_config import DEFAULT_NIGHTS

        await self.runtime.async_set_enabled(True)
        self.runtime.config["night_intervals"] = DEFAULT_NIGHTS
        with patch(
            "custom_components.home_control.doorbell.dt_util.now",
            return_value=datetime(2026, 9, 29, 1),
        ):
            await self.runtime.async_press()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime.last_result, "quiet_hours")
        self.runtime.config["night_intervals"] = [
            dict(DEFAULT_NIGHTS[0], mode="sound", speakers=["media_player.other"], volume=0.1)
        ]
        self.runtime._until = 0
        with patch(
            "custom_components.home_control.doorbell.dt_util.now",
            return_value=datetime(2026, 9, 29, 1),
        ):
            await self.runtime.async_press()
        self.assertEqual(
            [c[1]["entity_id"] for c in self.calls if c[0] == "play_media"], ["media_player.other"]
        )

    async def test_schedule_week_boundary_and_overlap(self):
        from custom_components.home_control.doorbell_config import policy, slots

        rule = dict(start="23:00", end="09:00", days=[6], mode="silent")
        self.assertIn(60, slots(rule))
        self.assertNotIn(540, slots(rule))
        config = dict(self.config, night_intervals=[rule])
        self.assertEqual(policy(config, datetime(2026, 9, 28, 8, 59)), ([], None))
        self.assertEqual(policy(config, datetime(2026, 9, 28, 9))[0], self.config["speakers"])
        self.assertTrue(slots(rule) & slots(dict(rule, start="08:00", end="10:00", days=[0])))
        with self.assertRaises(ValueError):
            slots(dict(rule, end="23:00"))

    async def test_mqtt_retained_invalid_and_exact_value(self):
        self.runtime.config.update(mqtt_key="Switch1", mqtt_payload="OFF")
        await self.runtime.async_set_enabled(True)
        for payload, retain in [
            ("bad", False),
            ("{}", False),
            ('{"Switch1":"ON"}', False),
            ('{"Switch1":"OFF"}', True),
        ]:
            self.runtime._mqtt(SimpleNamespace(payload=payload, retain=retain))
        self.assertFalse(self.runtime._tasks)
        self.runtime._mqtt(SimpleNamespace(payload='{"Switch1":"OFF"}', retain=False))
        await self.runtime.async_wait_idle()
        self.assertEqual(len([c for c in self.calls if c[0] == "play_media"]), 2)

    async def test_sensor_recovery_and_attribute_updates_ignored(self):
        self.runtime.config.update(source="binary_sensor", active_state="on")
        await self.runtime.async_set_enabled(True)

        def event(old, new):
            return SimpleNamespace(
                data={
                    "old_state": SimpleNamespace(state=old),
                    "new_state": SimpleNamespace(state=new),
                }
            )

        for old, new in [("unavailable", "on"), ("unknown", "on"), ("on", "on"), ("on", "off")]:
            self.runtime._event(event(old, new))
        self.assertFalse(self.runtime._tasks)
        self.runtime._event(event("off", "on"))
        await self.runtime.async_wait_idle()
        self.assertEqual(len([c for c in self.calls if c[0] == "play_media"]), 2)

    async def test_maintenance_during_volume_prevents_play(self):
        async def disable(call):
            self.runtime.set_enabled(False)

        self.hass.services.async_register("media_player", "volume_set", disable)
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        self.assertFalse(any(c[0] == "play_media" for c in self.calls))

    async def test_restart_preserves_gate_without_ring(self):
        from custom_components.home_control.doorbell import DoorbellRuntime

        await self.runtime.async_set_enabled(True)
        await self.runtime.async_stop()
        self.runtime = DoorbellRuntime(self.hass, self.entry)
        await self.runtime.async_start()
        self.assertTrue(self.runtime.enabled)
        self.assertEqual(self.calls, [])

    async def test_flow_add_reject_overlap_and_save(self):
        from custom_components.home_control.config_flow import HomeControlConfigFlow

        flow = HomeControlConfigFlow()
        flow.hass = self.hass
        result = await flow.async_step_doorbell(
            dict(
                self.config,
                name="Bell",
                mqtt_topic="",
                mqtt_payload="",
                mqtt_key="",
                active_state="on",
            )
        )
        self.assertEqual(result["step_id"], "doorbell_schedule")
        rule = dict(
            start="00:00:00", end="02:00:00", days=["0"], mode="silent", speakers=[], volume=0.1
        )
        result = await flow.async_step_doorbell_night(rule)
        self.assertIn("base", result["errors"])
        await flow.async_step_doorbell_remove({"interval": "0"})
        result = await flow.async_step_doorbell_night(rule)
        self.assertEqual(result["step_id"], "doorbell_schedule")
        result = await flow.async_step_doorbell_finish()
        self.assertEqual(result["data"]["night_intervals"][0]["days"], [0])

    async def test_real_platform_load_and_unload(self):
        from test_ha_runtime import HomeAssistantRuntimeTests

        await HomeAssistantRuntimeTests.test_config_entry_loads_real_switch_and_button_platforms(
            self
        )

    async def test_manual_volume_change_is_not_overwritten(self):
        async def manual_change(call):
            self.hass.states.async_set(call.data["entity_id"], "idle", {"volume_level": 0.8})

        self.hass.services.async_register("media_player", "play_media", manual_change)
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        self.assertEqual(self.hass.states.get("media_player.test").attributes["volume_level"], 0.8)
        self.assertEqual(len([c for c in self.calls if c[0] == "volume_set"]), 2)

    async def test_repeated_events_during_ring_are_dropped(self):
        await self.runtime.async_set_enabled(True)
        first = self.runtime.submit()
        self.runtime._until = 0
        self.assertIsNone(self.runtime.submit())
        await first
        self.assertEqual(len([c for c in self.calls if c[0] == "play_media"]), 2)

    async def test_unknown_volume_skips_only_that_speaker(self):
        self.hass.states.async_set("media_player.test", "idle")
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        self.assertEqual(self.runtime.failed_speakers, 1)
        self.assertEqual(
            [c[1]["entity_id"] for c in self.calls if c[0] == "play_media"], ["media_player.other"]
        )

    async def test_options_preserve_intervals_until_saved(self):
        from custom_components.home_control.config_flow import HomeControlOptionsFlow

        entry = self.entry

        class Options(HomeControlOptionsFlow):
            @property
            def config_entry(self):
                return entry

        flow = Options()
        flow.hass = self.hass
        result = await flow.async_step_init()
        self.assertEqual(result["step_id"], "doorbell")
        values = {
            "speakers": self.config["speakers"],
            "media_url": self.config["media_url"],
            "volume": 0.2,
            "sound_duration": 5,
            "cooldown": 10,
            "source": "virtual",
            "active_state": "on",
            "mqtt_topic": "",
            "mqtt_payload": "",
            "mqtt_key": "",
        }
        values = result["data_schema"](values)
        result = await flow.async_step_doorbell(values)
        self.assertEqual(result["step_id"], "doorbell_schedule")
        self.assertEqual(flow._doorbell["night_intervals"], [])
        result = await flow.async_step_doorbell_finish()
        self.assertEqual(result["data"]["volume"], 0.2)
        self.assertEqual(self.entry.data["volume"], 0.4)
