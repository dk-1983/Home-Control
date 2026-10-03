"""Voice routing, lifecycle, bounded delivery and shared doorbell regressions."""

import asyncio
import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant required")
class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.voice_events import ProcessVoice
        from custom_components.home_control.voice_runtime import VoiceRuntime

        self.temp = TemporaryDirectory(dir=Path(__file__).parents[1] / "work")
        self.hass = HomeAssistant(self.temp.name)
        self.config = dict(
            process_type="voice_center",
            rooms=[
                dict(
                    area="hall",
                    speakers=[],
                    presence=[],
                    motion=[],
                    alternatives=["kitchen", "bedroom"],
                ),
                dict(
                    area="kitchen",
                    speakers=["media_player.kitchen"],
                    presence=["binary_sensor.presence"],
                    motion=[],
                    alternatives=[],
                ),
                dict(
                    area="bedroom",
                    speakers=["media_player.bedroom"],
                    presence=[],
                    motion=["binary_sensor.motion"],
                    alternatives=[],
                ),
            ],
            night_intervals=[],
            fallback_area="bedroom",
            repeat_minutes=10,
            speech_gap=1,
        )
        self.entry = SimpleNamespace(entry_id="voice", title="Voice", data=self.config, options={})
        self.runtime = VoiceRuntime(self.hass, self.entry)
        self.calls = []

        async def service(call):
            self.calls.append(dict(call.data))

        self.hass.services.async_register("media_player", "play_media", service)
        for entity in ("media_player.kitchen", "media_player.bedroom"):
            self.hass.states.async_set(entity, "idle", {"alice_state": "IDLE", "volume_level": 0.6})
        self.hass.states.async_set("binary_sensor.presence", "off")
        self.hass.states.async_set("binary_sensor.motion", "off")
        self.hass.states.async_set("switch.output", "off")
        self.source_runtime = SimpleNamespace(
            hass=self.hass,
            entry=SimpleNamespace(entry_id="source", title="Кухня"),
            config=dict(
                process_type="motion",
                output="switch.output",
                voice_area="kitchen",
                voice_info=True,
                voice_warning=True,
                voice_error=True,
                voice_repeat=True,
            ),
            controller=SimpleNamespace(enabled=True),
            listeners=set(),
            attributes={},
        )
        self.observer = ProcessVoice(self.source_runtime)
        self.observer.start()
        await self.runtime.async_start()

        async def fast_wait(entity, seconds):
            from custom_components.home_control.speaker_queue import speaker_lane

            speaker_lane(self.hass, entity).until = 0

        self.runtime._wait_speech = fast_wait

    async def asyncTearDown(self):
        self.observer.stop()
        await self.runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    def notice(self, level="INFO", key="test", message="Проверка", active=False):
        from custom_components.home_control.voice_events import Notice

        return Notice("source", level, key, message, active)

    async def enable(self):
        await self.runtime.async_set_enabled(True)

    async def drain(self):
        await self.runtime.async_wait_idle()
        await asyncio.sleep(0)

    async def test_default_gate_and_native_tts_volume(self):
        self.runtime.bus.publish(self.notice())
        await self.drain()
        self.assertFalse(self.calls)
        await self.enable()
        self.runtime.bus.publish(self.notice())
        await self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["entity_id"], "media_player.kitchen")
        self.assertEqual(self.calls[0]["media_content_type"], "text")
        self.assertEqual(self.calls[0]["extra"], {"volume_level": 0.3})

    async def test_info_local_and_higher_levels_global(self):
        self.assertEqual(self.runtime.route(self.notice())[0], ["media_player.kitchen"])
        self.assertEqual(
            set(self.runtime.route(self.notice("WARNING"))[0]),
            {"media_player.kitchen", "media_player.bedroom"},
        )
        self.assertEqual(
            set(self.runtime.route(self.notice("ERROR"))[0]),
            {"media_player.kitchen", "media_player.bedroom"},
        )

    async def test_alternate_occupancy_and_fallback(self):
        self.source_runtime.config["voice_area"] = "hall"
        self.assertEqual(self.runtime.route(self.notice())[0], ["media_player.bedroom"])
        self.hass.states.async_set("binary_sensor.presence", "on")
        self.assertEqual(self.runtime.route(self.notice())[0], ["media_player.kitchen"])
        self.hass.states.async_set("media_player.kitchen", "unavailable")
        self.assertEqual(self.runtime.route(self.notice())[0], ["media_player.bedroom"])
        self.runtime.config.pop("fallback_area")
        self.assertEqual(self.runtime.route(self.notice())[0], [])
        self.hass.states.async_set("binary_sensor.motion", "on")
        await self.hass.async_block_till_done()
        self.hass.states.async_set("binary_sensor.motion", "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.runtime.route(self.notice())[0], ["media_player.bedroom"])
        self.runtime._motion["binary_sensor.motion"] -= 301
        self.assertEqual(self.runtime.route(self.notice())[0], [])

    async def test_unknown_motion_is_not_presence(self):
        self.source_runtime.config["voice_area"] = "hall"
        self.runtime.config.pop("fallback_area")
        self.runtime._motion["binary_sensor.motion"] = self.hass.loop.time()
        self.hass.states.async_set("binary_sensor.motion", "unavailable")
        self.assertEqual(self.runtime.route(self.notice())[0], [])

    async def test_night_silence_and_selected_volume(self):
        from custom_components.home_control.doorbell_config import slots

        rule = dict(
            start="23:00:00",
            end="09:00:00",
            days=list(range(7)),
            mode="silent",
            volume=0.1,
            speakers=[],
        )
        self.runtime._night_slots = [(slots(rule), rule)]
        await self.enable()
        with patch(
            "custom_components.home_control.voice_runtime.dt_util.now",
            return_value=datetime(2026, 9, 29, 3),
        ):
            self.runtime.bus.publish(self.notice("ERROR"))
            await self.drain()
            self.assertFalse(self.calls)
            rule["mode"] = "sound"
            rule["speakers"] = ["media_player.bedroom"]
            self.runtime.bus.publish(self.notice("ERROR", key="other"))
            await self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["extra"]["volume_level"], 0.1)
        self.assertEqual(self.calls[0]["entity_id"], "media_player.bedroom")

    async def test_cloud_only_station_excluded_for_volume_safety(self):
        self.hass.states.async_set("media_player.kitchen", "idle", {"volume_level": 0.6})
        self.assertEqual(self.runtime.route(self.notice())[0], ["media_player.bedroom"])

    async def test_level_and_process_gates(self):
        await self.enable()
        self.source_runtime.config["voice_info"] = False
        self.runtime.bus.publish(self.notice())
        self.source_runtime.controller.enabled = False
        self.runtime.bus.publish(self.notice("ERROR"))
        await self.drain()
        self.assertFalse(self.calls)

    async def test_priority_dedup_and_replacement(self):
        await self.enable()
        self.runtime.bus.publish(self.notice(message="Старое"))
        self.runtime.bus.publish(self.notice(message="Новое"))
        self.runtime.bus.publish(self.notice(message="Новое"))
        self.runtime.bus.publish(self.notice("ERROR", key="fault"))
        await self.drain()
        kitchen = [
            c["media_content_id"] for c in self.calls if c["entity_id"] == "media_player.kitchen"
        ]
        self.assertEqual(kitchen, ["Проверка", "Новое"])

    async def test_expired_queue_is_not_replayed(self):
        await self.enable()
        from custom_components.home_control.voice_runtime import TTL

        with patch.dict(TTL, INFO=-1):
            self.runtime.bus.publish(self.notice())
        await self.drain()
        self.assertFalse(self.calls)

    async def test_active_resolve_before_delivery_and_no_reminder(self):
        await self.enable()
        self.runtime.bus.publish(self.notice("ERROR", active=True))
        self.runtime.bus.resolve("source", "test")
        self.runtime._tick(None)
        await self.drain()
        self.assertFalse(self.calls)

    async def test_unchanged_issue_does_not_invalidate_pending_notice(self):
        await self.enable()
        self.runtime.bus.publish(self.notice("ERROR", active=True))
        self.runtime.bus.publish(self.notice("ERROR", active=True))
        await self.drain()
        self.assertEqual(len(self.calls), 2)

    async def test_reminders_optional_and_resolved(self):
        await self.enable()
        self.runtime.bus.publish(self.notice("ERROR", active=True))
        await self.drain()
        self.runtime._repeated[("source", "test")] -= 601
        self.source_runtime.config["voice_repeat"] = False
        self.runtime._tick(None)
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        self.source_runtime.config["voice_repeat"] = True
        self.runtime._tick(None)
        await self.drain()
        self.assertEqual(len(self.calls), 4)
        self.runtime.bus.resolve("source", "test")
        self.runtime._repeated[("source", "test")] -= 601
        self.runtime._tick(None)
        await self.drain()
        self.assertEqual(len(self.calls), 4)

    async def test_disable_clears_pending_and_does_not_operate_speakers(self):
        await self.enable()
        self.runtime.bus.publish(self.notice())
        self.runtime.set_enabled(False)
        await self.drain()
        await self.enable()
        await self.drain()
        self.assertFalse(self.calls)

    async def test_source_disable_cancels_pending_even_after_reenable(self):
        await self.enable()
        self.runtime.bus.publish(self.notice())
        self.source_runtime.controller.enabled = False
        self.observer.update()
        self.source_runtime.controller.enabled = True
        self.observer.update()
        await self.drain()
        self.assertFalse(self.calls)

    async def test_one_failed_station_does_not_block_others(self):
        async def service(call):
            if call.data["entity_id"] == "media_player.kitchen":
                raise RuntimeError("offline")
            self.calls.append(dict(call.data))

        self.hass.services.async_register("media_player", "play_media", service)
        await self.enable()
        self.runtime.bus.publish(self.notice("ERROR"))
        await self.drain()
        self.assertEqual([c["entity_id"] for c in self.calls], ["media_player.bedroom"])

    async def test_observer_has_no_startup_info_and_reports_source_error(self):
        await self.enable()
        self.observer.update()
        await self.drain()
        self.assertFalse(self.calls)
        self.source_runtime.attributes["last_error"] = "feedback_timeout"
        self.observer.update()
        self.observer.update()
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        self.source_runtime.attributes["last_error"] = None
        self.observer.update()
        self.assertFalse(self.runtime.bus.active)

    async def test_hood_fault_voice_explains_cause_and_resolves(self):
        await self.enable()
        self.source_runtime.config.update(process_type="hood", voice_repeat=False)
        self.source_runtime.attributes["last_error"] = "readback_failed"
        self.observer.update()
        self.observer.update()
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        notice = self.runtime.bus.active[("source", "last_error")]
        self.assertIn("свежее состояние реле", notice.message)
        self.assertEqual(notice.level, "ERROR")
        self.assertFalse(self.runtime.bus.allowed(notice, repeat=True))
        self.source_runtime.attributes["last_error"] = "multiple_active_outputs"
        self.observer.update()
        self.assertIn(
            "нескольких включённых скоростях",
            self.runtime.bus.active[("source", "last_error")].message,
        )
        self.source_runtime.attributes["last_error"] = None
        self.observer.update()
        self.assertNotIn(("source", "last_error"), self.runtime.bus.active)
        await self.drain()
        self.assertEqual(len(self.calls), 2)

    async def test_equipment_replacement_does_not_change_source_route(self):
        from custom_components.home_control.voice_events import ProcessVoice

        self.observer.stop()
        self.source_runtime.config["output"] = "switch.replacement"
        self.observer = ProcessVoice(self.source_runtime)
        self.observer.start()
        self.assertEqual(self.runtime.route(self.notice())[0], ["media_player.kitchen"])

    async def test_service_resolve_and_source_validation(self):
        from homeassistant.exceptions import HomeAssistantError

        await self.enable()
        await self.hass.services.async_call(
            "home_control",
            "announce",
            dict(source="source", key="example", message="Проверка", level="ERROR", active=True),
            blocking=True,
        )
        await self.drain()
        self.assertIn(("source", "custom:example"), self.runtime.bus.active)
        await self.hass.services.async_call(
            "home_control",
            "announce",
            dict(source="source", key="example", resolved=True),
            blocking=True,
        )
        self.assertFalse(self.runtime.bus.active)
        with self.assertRaises(HomeAssistantError):
            await self.hass.services.async_call(
                "home_control",
                "announce",
                dict(source="missing", key="x", message="Test"),
                blocking=True,
            )

    async def test_persistence_failure_keeps_center_disabled(self):
        with patch.object(self.runtime.store, "async_save", AsyncMock(side_effect=OSError("disk"))):
            with self.assertRaises(OSError):
                await self.enable()
        self.assertFalse(self.runtime.enabled)

    async def test_doorbell_waits_for_shared_speaker_but_free_speaker_plays(self):
        from custom_components.home_control.doorbell import DoorbellRuntime
        from custom_components.home_control.speaker_queue import speaker_lane

        config = dict(
            source="virtual",
            speakers=["media_player.kitchen", "media_player.bedroom"],
            volume=0.6,
            media_url="http://localhost/bell.mp3",
            cooldown=10,
            sound_duration=0.001,
            night_intervals=[],
        )
        bell = DoorbellRuntime(
            self.hass, SimpleNamespace(entry_id="bell", data=config, options={}, title="Bell")
        )
        await bell.async_start()
        await bell.async_set_enabled(True)
        lane = speaker_lane(self.hass, "media_player.kitchen")
        await lane.lock.acquire()
        try:
            task = bell.submit()
            for _ in range(10):
                await asyncio.sleep(0.001)
            self.assertEqual([c["entity_id"] for c in self.calls], ["media_player.bedroom"])
        finally:
            lane.lock.release()
        await task
        self.assertEqual(len(self.calls), 2)
        await bell.async_stop()

    async def test_shared_lane_timeout_does_not_release_another_owner(self):
        from custom_components.home_control.speaker_queue import speaker_lane

        lane = speaker_lane(self.hass, "media_player.kitchen")
        await lane.lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                async with lane.claim(0.001):
                    self.fail("must not acquire")
            self.assertTrue(lane.lock.locked())
        finally:
            lane.lock.release()

    async def test_real_center_platform_lifecycle(self):
        from test_ha_runtime import HomeAssistantRuntimeTests

        await self.runtime.async_stop()
        await HomeAssistantRuntimeTests.test_config_entry_loads_real_switch_and_button_platforms(
            self
        )

    async def test_flow_rooms_nights_validation_and_singleton(self):
        from homeassistant.helpers import area_registry

        from custom_components.home_control.config_flow import HomeControlConfigFlow

        await area_registry.async_load(self.hass)
        registry = area_registry.async_get(self.hass)
        area = registry.async_create("Kitchen").id
        self.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [])
        flow = HomeControlConfigFlow()
        flow.hass = self.hass
        form = await flow.async_step_voice_center()
        values = form["data_schema"]({"name": "Voice"})
        menu = await flow.async_step_voice_center(values)
        self.assertEqual(menu["step_id"], "voice_menu")
        error = await flow.async_step_voice_finish()
        self.assertEqual(error["errors"]["base"], "voice_speakers_required")
        await flow.async_step_voice_room(dict(area=area, speakers=["media_player.kitchen"]))
        rule = dict(
            start="23:00:00", end="09:00:00", days=["0"], mode="sound", volume=0.1, speakers=[]
        )
        error = await flow.async_step_voice_night(rule)
        self.assertEqual(error["errors"]["base"], "voice_overlap")
        await flow.async_step_voice_remove_night({"item": "0"})
        await flow.async_step_voice_night(rule)
        result = await flow.async_step_voice_finish()
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["night_intervals"][0]["speakers"], [])
        self.hass.config_entries.async_entries = lambda domain: [self.entry]
        self.assertEqual((await flow.async_step_voice_finish())["reason"], "voice_center_exists")
        self.hass.config_entries = None

    async def test_night_policy_rechecked_after_queue_wait(self):
        from custom_components.home_control.doorbell_config import slots

        await self.enable()
        self.runtime.bus.publish(self.notice())
        rule = dict(
            start="00:00:00",
            end="23:59:00",
            days=list(range(7)),
            mode="silent",
            speakers=[],
            volume=0.1,
        )
        self.runtime._night_slots = [(slots(rule), rule)]
        with patch(
            "custom_components.home_control.voice_runtime.dt_util.now",
            return_value=datetime(2026, 9, 29, 12),
        ):
            await self.drain()
        self.assertFalse(self.calls)

    async def test_issue_recurrence_drops_previous_pending_instance(self):
        await self.enable()
        first = self.notice("ERROR", active=True)
        self.runtime.bus.publish(first)
        self.runtime.bus.resolve("source", "test")
        self.runtime.bus.publish(self.notice("ERROR", active=True))
        await self.drain()
        self.assertEqual(len(self.calls), 2)

    async def test_observer_info_uses_settled_state_without_commands(self):
        await self.enable()
        self.hass.states.async_set("switch.output", "on")
        await self.hass.async_block_till_done()
        self.observer._timer.cancel()
        self.observer._info()
        await self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Включено", self.calls[0]["media_content_id"])

    async def test_enable_state_restores_without_replaying_info(self):
        from custom_components.home_control.voice_runtime import VoiceRuntime

        await self.enable()
        await self.runtime.async_stop()
        self.runtime = VoiceRuntime(self.hass, self.entry)
        await self.runtime.async_start()
        self.assertTrue(self.runtime.enabled)
        self.assertEqual(self.calls, [])
