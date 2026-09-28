"""Refresh completion, race rejection and snapshot ownership for the hood."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant is not installed")
class ReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers.entity_component import DATA_INSTANCES
        from hood_fakes import FakeCoordinator, FakeRelay

        from custom_components.home_control.hood_readback import HoodReadback

        self.temp = TemporaryDirectory(dir=Path(__file__).parents[1] / "work", prefix="readback-")
        self.hass = HomeAssistant(self.temp.name)
        self.owner = SimpleNamespace(hardware=[False] * 4, polling=True)
        self.coordinator = FakeCoordinator(self.hass, self.owner)
        self.ids = [f"switch.relay_{i}" for i in range(1, 5)]
        self.entities = {key: FakeRelay(self.coordinator, i) for i, key in enumerate(self.ids, 1)}
        self.hass.data[DATA_INSTANCES] = {"switch": SimpleNamespace(get_entity=self.entities.get)}
        self.adapter = HoodReadback(self.hass, self.ids)

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def test_new_poll_runs_after_already_running_poll(self):
        first_entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def update():
            calls.append(1)
            if len(calls) == 1:
                first_entered.set()
                await release.wait()
                return {"outputs": {i: {"state": True} for i in range(1, 5)}}
            return self.coordinator.snapshot()

        self.coordinator._async_update_data = update
        first = asyncio.create_task(self.coordinator.async_refresh())
        await first_entered.wait()
        second = asyncio.create_task(self.adapter.async_read())
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        release.set()
        await first
        self.assertEqual(await second, (False,) * 4)
        self.assertEqual(len(calls), 2)

    async def test_uses_refresh_not_debounced_request_or_ha_state(self):
        self.owner.hardware[2] = True
        for entity in self.ids:
            self.hass.states.async_set(entity, "off")
        self.coordinator.async_request_refresh = AsyncMock(side_effect=AssertionError)
        self.assertEqual(await self.adapter.async_read(), (False, False, True, False))
        self.coordinator.async_request_refresh.assert_not_called()

    async def test_result_is_an_independent_tuple(self):
        result = await self.adapter.async_read()
        self.coordinator.data["outputs"][1]["state"] = True
        self.assertEqual(result, (False,) * 4)

    async def test_failed_poll_does_not_accept_old_off_data(self):
        async def failed():
            raise OSError("offline")

        self.coordinator._async_update_data = failed
        with self.assertRaisesRegex(Exception, "readback_failed"):
            await self.adapter.async_read()

    async def test_noop_refresh_is_rejected(self):
        self.coordinator.async_refresh = AsyncMock()
        with self.assertRaisesRegex(Exception, "readback_failed"):
            await self.adapter.async_read()

    async def test_concurrent_write_invalidates_snapshot(self):
        async def update():
            self.coordinator._write_generation += 1
            return self.coordinator.snapshot()

        self.coordinator._async_update_data = update
        with self.assertRaisesRegex(Exception, "readback_concurrent_write"):
            await self.adapter.async_read()

    async def test_pending_patch_invalidates_snapshot(self):
        async def update():
            self.coordinator._pending_write_patches[("outputs", 1, "state")] = (1, False)
            return self.coordinator.snapshot()

        self.coordinator._async_update_data = update
        with self.assertRaisesRegex(Exception, "readback_pending_write"):
            await self.adapter.async_read()

    async def test_missing_channel_or_non_boolean_is_rejected(self):
        for invalid in [None, 0, "off"]:
            self.owner.hardware[1] = invalid
            with self.assertRaisesRegex(Exception, "invalid_readback_data"):
                await self.adapter.async_read()

    async def test_reload_during_read_is_rejected(self):
        async def update():
            self.entities.pop(self.ids[0])
            return self.coordinator.snapshot()

        self.coordinator._async_update_data = update
        with self.assertRaises(Exception):
            await self.adapter.async_read()

    async def test_different_controllers_cannot_form_one_motor(self):
        from hood_fakes import FakeCoordinator

        other = FakeCoordinator(self.hass, self.owner)
        self.entities[self.ids[1]].coordinator = other
        with self.assertRaisesRegex(Exception, "unsupported_hood_relays"):
            self.adapter.resolve()
        await other.async_shutdown()
