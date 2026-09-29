"""Public voice protocol receiver tests against real HA services and queues."""

import asyncio
import importlib.util
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant required")
class ExternalVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.config_entries import ConfigEntryState
        from test_voice import VoiceTests

        await VoiceTests.asyncSetUp(self)
        self.owner = SimpleNamespace(
            entry_id="srcc-entry", domain="fourvrs_server_room", state=ConfigEntryState.LOADED
        )
        self.entries = {self.owner.entry_id: self.owner}
        self.hass.config_entries = SimpleNamespace(
            async_get_entry=lambda entry: self.entries.get(entry)
        )
        self.registry = self.runtime.external
        self.time = self.hass.loop.time()
        self.registry.now = lambda: self.time
        self.identity = dict(
            domain="fourvrs_server_room", config_entry_id="srcc-entry", process_id="climate"
        )
        self.source = tuple(self.identity.values())
        self.revision = 0

    async def asyncTearDown(self):
        from test_voice import VoiceTests

        await VoiceTests.asyncTearDown(self)

    def envelope(self, **fields):
        self.revision += 1
        return dict(
            protocol_version=1,
            source=dict(self.identity),
            producer_session="producer-one",
            revision=self.revision,
            **fields,
        )

    def registration(self, **changes):
        data = self.envelope(
            name="Серверная",
            area_id="kitchen",
            preferences=dict(
                voice_info=True, voice_warning=True, voice_error=True, voice_repeat=True
            ),
            maintenance=False,
            active_issues=[],
        )
        data.update(changes)
        return data

    def event(
        self,
        key="temperature",
        level="ERROR",
        message="Температура повышена.",
        active=True,
        resolved=False,
    ):
        return dict(key=key, level=level, message=message, active=active, resolved=resolved)

    async def call(self, operation, data):
        return await self.hass.services.async_call(
            "home_control", operation, data, blocking=True, return_response=True
        )

    async def register(self, **changes):
        return await self.call("register_voice_source", self.registration(**changes))

    async def publish(self, event=None, **changes):
        data = self.envelope(
            center_session=self.registry.center_session, event=event or self.event()
        )
        data.update(changes)
        return await self.call("publish_voice_event", data)

    async def drain(self):
        await self.runtime.async_wait_idle()
        await asyncio.sleep(0)

    async def test_register_disabled_center_then_enable_uses_current_snapshot(self):
        response = await self.register(
            active_issues=[dict(key="temperature", level="ERROR", message="Температура повышена.")]
        )
        self.assertEqual(
            response,
            {
                "accepted": True,
                "protocol_version": 1,
                "center_session": self.registry.center_session,
                "lease_seconds": 180,
            },
        )
        self.assertFalse(self.calls)
        await self.runtime.async_set_enabled(True)
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["media_content_id"], "Серверная. Температура повышена.")

    async def test_info_local_warning_global_and_preferences(self):
        await self.runtime.async_set_enabled(True)
        await self.register()
        await self.publish(self.event(key="mode", level="INFO", active=False))
        await self.drain()
        self.assertEqual([c["entity_id"] for c in self.calls], ["media_player.kitchen"])
        await self.publish(self.event(level="WARNING"))
        await self.drain()
        self.assertEqual(len(self.calls), 3)
        prefs = dict(voice_info=False, voice_warning=False, voice_error=False, voice_repeat=False)
        await self.register(preferences=prefs)
        await self.publish(self.event(key="suppressed"))
        await self.drain()
        self.assertEqual(len(self.calls), 3)

    async def test_external_and_internal_namespaces_do_not_collide(self):
        self.entries["source"] = SimpleNamespace(
            entry_id="source", domain="fourvrs_server_room", state=self.owner.state
        )
        self.identity["config_entry_id"] = "source"
        await self.register()
        self.assertIn("source", self.runtime.bus.sources)
        self.assertIn(("fourvrs_server_room", "source", "climate"), self.runtime.bus.sources)
        self.assertIs(self.runtime.bus.sources["source"], self.observer)

    async def test_unknown_owner_domain_mismatch_and_unloaded_owner_rejected(self):
        from homeassistant.config_entries import ConfigEntryState

        for changes in ({"config_entry_id": "missing"}, {"domain": "other"}):
            result = await self.register(source=self.identity | changes)
            self.assertEqual(result["reason"], "unknown_source")
        self.owner.state = ConfigEntryState.NOT_LOADED
        self.assertEqual((await self.register())["reason"], "unknown_source")
        self.assertFalse(self.registry.sources)

    async def test_snapshot_renew_does_not_repeat_or_reset_reminder(self):
        await self.runtime.async_set_enabled(True)
        snapshot = [dict(key="temperature", level="ERROR", message="Температура повышена.")]
        await self.register(active_issues=snapshot)
        await self.drain()
        stamp = self.runtime._repeated[(self.source, "temperature")]
        first = self.runtime.bus.active[(self.source, "temperature")]
        self.time += 60
        await self.register(active_issues=snapshot)
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.runtime._repeated[(self.source, "temperature")], stamp)
        self.assertIs(self.runtime.bus.active[(self.source, "temperature")], first)
        self.assertEqual(self.registry.sources[self.source].expires, self.time + 180)

    async def test_resolved_and_snapshot_removal_cancel_queued(self):
        await self.runtime.async_set_enabled(True)
        await self.register()
        # Claim lanes before service calls, so no worker can race delivery.
        from custom_components.home_control.speaker_queue import speaker_lane

        lanes = [
            speaker_lane(self.hass, e) for e in ("media_player.kitchen", "media_player.bedroom")
        ]
        for lane in lanes:
            await lane.lock.acquire()
        try:
            await self.publish()
            await self.publish(self.event(active=False, resolved=True, message=""))
            self.assertFalse(self.runtime.bus.active)
            self.assertEqual(self.runtime.attributes["queued"], 0)
            await self.publish(self.event(key="second"))
            await self.register()
            self.assertFalse(self.runtime.bus.active)
            self.assertEqual(self.runtime.attributes["queued"], 0)
        finally:
            for lane in lanes:
                lane.lock.release()
        await self.drain()
        self.assertFalse(self.calls)

    async def test_duplicate_revision_is_idempotent_and_collision_rejected(self):
        data = self.registration()
        first = await self.call("register_voice_source", data)
        deadline = self.registry.sources[self.source].expires
        self.time += 60
        self.assertEqual(await self.call("register_voice_source", data), first)
        self.assertEqual(self.registry.sources[self.source].expires, deadline)
        collision = deepcopy(data)
        collision["maintenance"] = True
        self.assertEqual(
            (await self.call("register_voice_source", collision))["reason"], "revision_conflict"
        )
        self.assertFalse(self.registry.sources[self.source].maintenance)
        await self.register()
        self.assertEqual(
            (await self.call("register_voice_source", data))["reason"], "stale_revision"
        )

    async def test_duplicate_publish_does_not_speak_twice(self):
        await self.runtime.async_set_enabled(True)
        await self.register()
        data = self.envelope(center_session=self.registry.center_session, event=self.event())
        await self.call("publish_voice_event", data)
        await self.drain()
        await self.call("publish_voice_event", data)
        await self.drain()
        self.assertEqual(len(self.calls), 2)

    async def test_expiry_removes_pending_and_same_session_can_return(self):
        data = self.registration(active_issues=[dict(key="test", level="ERROR", message="Ошибка")])
        await self.call("register_voice_source", data)
        self.time += 181
        self.registry.expire()
        self.assertFalse(self.runtime.bus.active)
        self.assertNotIn(self.source, self.runtime.bus.sources)
        # Idempotent registration retry does not revive an expired lease.
        await self.call("register_voice_source", data)
        self.assertFalse(self.registry.sources[self.source].registered)
        self.assertEqual((await self.publish())["reason"], "registration_required")
        await self.register()
        self.assertTrue(self.registry.sources[self.source].registered)
        self.assertEqual(self.registry.sources[self.source].session, "producer-one")

    async def test_gate_rechecks_expiry_before_timer_cleanup(self):
        await self.register()
        from custom_components.home_control.voice_events import Notice

        notice = Notice(self.source, "INFO", "x", "x")
        self.assertTrue(self.runtime.bus.allowed(notice))
        self.time += 180
        self.assertFalse(self.runtime.bus.allowed(notice))

    async def test_unload_owner_blocks_voice_and_cleans_registration(self):
        from homeassistant.config_entries import ConfigEntryState

        await self.register()
        await self.publish()
        self.owner.state = ConfigEntryState.UNLOAD_IN_PROGRESS
        notice = self.runtime.bus.active[(self.source, "temperature")]
        self.assertFalse(self.runtime.bus.allowed(notice))
        self.registry.expire()
        self.assertFalse(self.runtime.bus.active)

    async def test_replaced_session_and_late_unregister_cannot_touch_new(self):
        await self.register()
        await self.register(producer_session="producer-two", revision=1)
        result = await self.call(
            "unregister_voice_source", self.envelope(center_session=self.registry.center_session)
        )
        self.assertEqual(result["reason"], "retired_session")
        self.assertEqual((await self.register())["reason"], "retired_session")
        self.assertEqual(self.registry.sources[self.source].session, "producer-two")
        self.assertTrue(self.registry.sources[self.source].registered)

    async def test_unregister_is_idempotent_but_session_cannot_reopen(self):
        await self.register()
        data = self.envelope(center_session=self.registry.center_session)
        self.assertTrue((await self.call("unregister_voice_source", data))["accepted"])
        self.assertTrue((await self.call("unregister_voice_source", data))["accepted"])
        self.assertEqual((await self.register())["reason"], "retired_session")
        self.assertTrue((await self.register(producer_session="new"))["accepted"])

    async def test_wrong_center_session_does_not_modify_registration(self):
        await self.register()
        revision = self.registry.sources[self.source].revision
        self.assertEqual(
            (await self.publish(center_session="old"))["reason"], "registration_required"
        )
        self.assertEqual(self.registry.sources[self.source].revision, revision)
        self.assertFalse(self.runtime.bus.active)

    async def test_maintenance_clears_issues_without_changing_equipment(self):
        await self.register()
        await self.publish()
        before = self.hass.states.get("switch.output")
        await self.register(maintenance=True)
        self.assertFalse(self.runtime.bus.active)
        await self.publish()
        self.assertFalse(self.runtime.bus.active)
        self.assertIs(self.hass.states.get("switch.output"), before)
        self.assertEqual(self.registry.summaries()[0]["status"], "maintenance")
        await self.register(active_issues=[dict(key="test", level="ERROR", message="Ошибка")])
        self.assertEqual(len(self.runtime.bus.active), 1)

    async def test_permissions_reduction_discards_pending_before_reenable(self):
        await self.runtime.async_set_enabled(True)
        await self.register()
        from custom_components.home_control.speaker_queue import speaker_lane

        lane = speaker_lane(self.hass, "media_player.kitchen")
        await lane.lock.acquire()
        try:
            await self.publish(self.event(level="INFO", active=False))
            await self.register(
                preferences=dict(
                    voice_info=False, voice_warning=True, voice_error=True, voice_repeat=True
                )
            )
            self.assertEqual(self.runtime.attributes["queued"], 0)
            await self.register()
        finally:
            lane.lock.release()
        await self.drain()
        self.assertFalse(self.calls)

    async def test_recovery_replaced_by_recurring_problem(self):
        await self.runtime.async_set_enabled(True)
        await self.register()
        from custom_components.home_control.speaker_queue import speaker_lane

        lane = speaker_lane(self.hass, "media_player.kitchen")
        await lane.lock.acquire()
        try:
            await self.publish(self.event(active=False, resolved=True, message=""))
            await self.publish(self.event(level="INFO", active=False, message="Восстановлено"))
            await self.publish()
        finally:
            lane.lock.release()
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("Восстановлено" not in c["media_content_id"] for c in self.calls))

    async def test_strict_schemas_reject_before_mutation(self):
        import voluptuous as vol

        good = self.registration()
        invalid = []
        for field, value in [
            ("protocol_version", True),
            ("protocol_version", 2),
            ("revision", True),
            ("revision", 0),
            ("revision", "2"),
            ("maintenance", 1),
            ("name", " "),
            ("extra", "field"),
        ]:
            invalid.append(good | {field: value})
        invalid += [
            good | {"preferences": good["preferences"] | {"voice_info": 1}},
            good | {"source": self.identity | {"extra": True}},
            good | {"active_issues": [dict(key="k", level="INFO", message="x")]},
            good | {"active_issues": [dict(key="k", level="ERROR", message="x")] * 2},
            good
            | {"maintenance": True, "active_issues": [dict(key="k", level="ERROR", message="x")]},
        ]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(vol.Invalid):
                    await self.call("register_voice_source", data)
                self.assertFalse(self.registry.sources)
        await self.call("register_voice_source", good)
        for event in [
            self.event(active=True, resolved=True),
            self.event(level="INFO"),
            self.event(message=""),
            self.event(key="k" * 81),
            self.event(message="x" * 501),
            self.event(active=1),
        ]:
            with self.assertRaises(vol.Invalid):
                await self.publish(event)
            self.assertFalse(self.runtime.bus.active)

    async def test_ready_discovery_and_stop_remove_services(self):
        seen = []
        from homeassistant.core import callback

        @callback
        def receive(event):
            seen.append(event.data)

        unsub = self.hass.bus.async_listen("home_control_voice_ready", receive)
        try:
            self.hass.bus.async_fire("home_control_voice_discover", {"protocol_version": True})
            await self.hass.async_block_till_done()
            self.assertFalse(seen)
            self.hass.bus.async_fire("home_control_voice_discover", {"protocol_version": 1})
            await self.hass.async_block_till_done()
            self.assertEqual(
                seen, [{"protocol_version": 1, "center_session": self.registry.center_session}]
            )
            await self.register()
            await self.runtime.async_stop()
            self.assertFalse(
                self.hass.services.has_service("home_control", "register_voice_source")
            )
            self.assertNotIn(self.source, self.runtime.bus.sources)
            self.assertIsNone(self.registry._timer)
        finally:
            unsub()

    async def test_capacity_refuses_instead_of_forgetting_old_sessions(self):
        await self.register()
        with patch("custom_components.home_control.voice_external.MAX_SOURCES", 1):
            self.assertEqual(
                (await self.register(source=self.identity | {"process_id": "second"}))["reason"],
                "source_capacity",
            )
        with patch("custom_components.home_control.voice_external.MAX_RETIRED_SESSIONS", 1):
            await self.register(producer_session="two")
            self.assertEqual(
                (await self.register(producer_session="three"))["reason"], "session_capacity"
            )
            self.assertEqual((await self.register())["reason"], "retired_session")
        self.assertEqual(self.registry.sources[self.source].session, "two")

    async def test_enabling_error_level_admits_current_issue_once(self):
        await self.runtime.async_set_enabled(True)
        prefs = dict(voice_info=False, voice_warning=False, voice_error=False, voice_repeat=False)
        snapshot = [dict(key="fault", level="ERROR", message="Ошибка")]
        await self.register(preferences=prefs, active_issues=snapshot)
        await self.drain()
        self.assertFalse(self.calls)
        prefs["voice_error"] = True
        await self.register(preferences=prefs, active_issues=snapshot)
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        await self.register(preferences=prefs, active_issues=snapshot)
        await self.drain()
        self.assertEqual(len(self.calls), 2)

    async def test_repeat_off_removes_queued_reminder_but_keeps_active_issue(self):
        from custom_components.home_control.speaker_queue import speaker_lane

        await self.runtime.async_set_enabled(True)
        snapshot = [dict(key="fault", level="ERROR", message="Ошибка")]
        await self.register(active_issues=snapshot)
        await self.drain()
        lanes = [
            speaker_lane(self.hass, e) for e in ("media_player.kitchen", "media_player.bedroom")
        ]
        for lane in lanes:
            await lane.lock.acquire()
        try:
            self.runtime._repeated[(self.source, "fault")] -= 601
            self.runtime._tick(None)
            self.assertEqual(self.runtime.attributes["queued"], 2)
            prefs = dict(voice_info=True, voice_warning=True, voice_error=True, voice_repeat=False)
            await self.register(preferences=prefs, active_issues=snapshot)
            self.assertEqual(self.runtime.attributes["queued"], 0)
            self.assertIn((self.source, "fault"), self.runtime.bus.active)
        finally:
            for lane in lanes:
                lane.lock.release()
        await self.drain()
        self.assertEqual(len(self.calls), 2)

    async def test_active_issue_capacity_and_oversized_snapshot(self):
        import voluptuous as vol

        snapshot = [dict(key=f"fault_{i}", level="ERROR", message="Ошибка") for i in range(64)]
        await self.register(active_issues=snapshot)
        self.assertEqual((await self.publish())["reason"], "issue_capacity")
        self.assertEqual(len(self.runtime.bus.active), 64)
        with self.assertRaises(vol.Invalid):
            await self.register(
                active_issues=snapshot + [dict(key="extra", level="ERROR", message="Ошибка")]
            )
        self.assertEqual(len(self.runtime.bus.active), 64)

    async def test_expired_duplicate_publish_requires_new_registration(self):
        await self.register()
        data = self.envelope(center_session=self.registry.center_session, event=self.event())
        await self.call("publish_voice_event", data)
        self.time += 181
        self.registry.expire()
        result = await self.call("publish_voice_event", data)
        self.assertEqual(result["reason"], "registration_required")
        self.assertFalse(self.runtime.bus.active)

    async def test_quick_severity_changes_leave_only_latest_pending_event(self):
        from custom_components.home_control.speaker_queue import speaker_lane

        await self.runtime.async_set_enabled(True)
        await self.register()
        lanes = [
            speaker_lane(self.hass, e) for e in ("media_player.kitchen", "media_player.bedroom")
        ]
        for lane in lanes:
            await lane.lock.acquire()
        try:
            await self.publish()
            await self.publish(self.event(level="WARNING", message="Предупреждение"))
            await self.publish()
        finally:
            for lane in lanes:
                lane.lock.release()
        await self.drain()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("Температура повышена" in c["media_content_id"] for c in self.calls))
