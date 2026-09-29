"""Optional wire-level tests using the actual SRCC client, read-only.

Set SRCC_REPO to an SRCC checkout (0.4.0 or later). Nothing in that checkout is
written. Its event projection is replaced with deterministic process observations;
HA services, the SRCC transport client and Home Control receiver are real.
"""

import asyncio
import importlib
import importlib.util
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace

HAS_HA = importlib.util.find_spec("homeassistant") is not None
SRCC_REPO = os.environ.get("SRCC_REPO")


@unittest.skipUnless(
    HAS_HA and SRCC_REPO, "Set SRCC_REPO for the optional read-only client contract tests"
)
class SrccContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_voice_external import ExternalVoiceTests

        await ExternalVoiceTests.asyncSetUp(self)
        path = Path(SRCC_REPO) / "custom_components" / "fourvrs_server_room"
        package = ModuleType("_home_control_srcc_contract")
        package.__path__ = [str(path)]
        sys.modules[package.__name__] = package
        self.client_module = importlib.import_module(package.__name__ + ".voice")
        self.issues = {}
        self.infos = []
        self.subscribers = set()

        def subscribe(listener):
            self.subscribers.add(listener)
            return lambda: self.subscribers.discard(listener)

        self.client_runtime = SimpleNamespace(
            hass=self.hass,
            entry=SimpleNamespace(entry_id="srcc-entry", title="Серверная"),
            data=dict(
                voice_area="kitchen",
                voice_info=True,
                voice_warning=True,
                voice_error=True,
                voice_repeat=True,
            ),
            subscribe=subscribe,
            notify=lambda: None,
        )
        self.client = self.client_module.ProcessVoice(self.client_runtime)
        self.client.projection.update = lambda runtime: (
            deepcopy(self.issues),
            deepcopy(self.infos),
        )
        await self.client.async_restore()

    async def asyncTearDown(self):
        from test_voice_external import ExternalVoiceTests

        self.client.stop()
        await self.hass.async_block_till_done()
        await ExternalVoiceTests.asyncTearDown(self)
        for name in list(sys.modules):
            if name.startswith("_home_control_srcc_contract"):
                sys.modules.pop(name)

    async def settle(self):
        await self.hass.async_block_till_done()
        await self.runtime.async_wait_idle()
        await asyncio.sleep(0)

    async def restart_center(self):
        from custom_components.home_control.speaker_queue import speaker_lane
        from custom_components.home_control.voice_runtime import VoiceRuntime

        await self.runtime.async_stop()
        self.runtime = VoiceRuntime(self.hass, self.entry)

        async def fast_wait(entity, seconds):
            speaker_lane(self.hass, entity).until = 0

        self.runtime._wait_speech = fast_wait
        self.registry = self.runtime.external
        await self.runtime.async_start()

    async def test_client_first_then_center_active_recovery_and_heartbeat(self):
        await self.runtime.async_stop()
        self.client.start()
        await self.settle()
        self.assertEqual(self.client.connection, "center_unavailable")
        self.issues = {"temperature_high": dict(level="ERROR", message="Повышена температура.")}
        self.client.observe()
        await self.restart_center()
        await self.runtime.async_set_enabled(True)
        await self.settle()
        self.assertTrue(self.client.registered)
        self.assertEqual(self.client.center_session, self.registry.center_session)
        self.assertEqual(len(self.calls), 2)
        self.client._renew(None)
        await self.settle()
        self.assertEqual(len(self.calls), 2)
        self.issues = {}
        self.infos = [
            dict(
                key="temperature_high",
                level="INFO",
                message="Температура восстановлена.",
                active=False,
                resolved=False,
            )
        ]
        self.client.observe()
        await self.settle()
        self.assertFalse(self.runtime.bus.active)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]["entity_id"], "media_player.kitchen")

    async def test_center_first_restart_current_snapshot_and_maintenance(self):
        await self.runtime.async_set_enabled(True)
        self.client.start()
        await self.settle()
        self.assertTrue(self.client.registered)
        self.issues = {"failure": dict(level="ERROR", message="Неисправность.")}
        self.client.observe()
        await self.settle()
        old_center = self.registry.center_session
        await self.restart_center()
        await self.settle()
        self.assertNotEqual(self.registry.center_session, old_center)
        self.assertEqual(self.client.center_session, self.registry.center_session)
        self.assertEqual(len(self.calls), 4)
        await self.client.async_set_maintenance(True)
        await self.settle()
        self.assertFalse(self.runtime.bus.active)
        self.assertEqual(self.registry.summaries()[0]["status"], "maintenance")
        self.client.stop()
        await self.settle()
        self.assertEqual(self.registry.summaries()[0]["status"], "unregistered")

    async def test_lease_expiry_same_client_reregisters_actual_snapshot(self):
        self.client.start()
        await self.settle()
        producer = self.client.session
        self.registry.sources[self.source].expires = self.registry.now() - 1
        self.registry.expire()
        self.issues = {"fault": dict(level="ERROR", message="Ошибка.")}
        self.client.observe()
        await self.settle()
        self.assertFalse(self.client.registered)
        self.assertEqual(self.client.connection, "center_error")
        self.client.retry_at = 0
        self.client._renew(None)
        await self.settle()
        self.assertTrue(self.client.registered)
        self.assertEqual(self.registry.sources[self.source].session, producer)
        self.assertIn((self.source, "fault"), self.runtime.bus.active)
