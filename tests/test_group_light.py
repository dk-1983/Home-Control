"""Stepped lighting, memory and existing button control share one runtime."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant is not installed")
class GroupLightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.light import GroupLight
        from custom_components.home_control.runtime import HomeControlRuntime

        self.temp = TemporaryDirectory(
            dir=Path(__file__).parents[1] / "work", prefix="group-light-"
        )
        self.hass = HomeAssistant(self.temp.name)
        self.groups = [f"switch.group_{i}" for i in range(1, 5)]
        self.config = {f"group_{i}": entity for i, entity in enumerate(self.groups, 1)} | {
            "selection_window": 3,
            "feedback_timeout": 5,
            "mqtt_topic": "room/stat/RESULT",
            "mqtt_button": "Button1",
            "night_light": "switch.night",
        }
        self.commands = []
        self.feedback = True
        for entity in self.groups + ["switch.night"]:
            self.hass.states.async_set(entity, "off")

        async def service(call):
            self.commands.append((call.service, call.data["entity_id"]))
            ids = call.data["entity_id"]
            if isinstance(ids, str):
                ids = [ids]
            if self.feedback:
                for entity in ids:
                    self.hass.states.async_set(entity, "on" if call.service == "turn_on" else "off")

        for name in ("turn_on", "turn_off"):
            self.hass.services.async_register("switch", name, service)
        self.patch = patch(
            "custom_components.home_control.runtime.mqtt.async_subscribe",
            AsyncMock(return_value=lambda: None),
        )
        self.patch.start()
        self.entry = SimpleNamespace(data=self.config, options={}, entry_id="room", title="Room")
        self.runtime = HomeControlRuntime(self.hass, self.entry)
        await self.runtime.async_start()
        self.light = GroupLight(self.runtime)

    async def asyncTearDown(self):
        await self.runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.patch.stop()
        self.temp.cleanup()

    async def test_steps_and_on_off_restore(self):
        await self.runtime.async_set_enabled(True)
        for brightness, count in [(64, 1), (128, 2), (191, 3), (255, 4)]:
            await self.light.async_turn_on(brightness=brightness)
            self.assertEqual(self.runtime._states(), ["off"] * (4 - count) + ["on"] * count)
            self.assertEqual(self.light.brightness, round(255 * count / 4))
        await self.light.async_turn_on(brightness=191)
        await self.light.async_turn_off()
        self.assertFalse(self.light.is_on)
        await self.light.async_turn_on()
        self.assertEqual(self.runtime._states(), ["off", "on", "on", "on"])
        before = list(self.commands)
        await self.light.async_turn_on()
        self.assertEqual(self.commands, before)

    async def test_button_updates_saved_pattern_and_continues_brightness(self):
        await self.runtime.async_set_enabled(True)
        await self.light.async_turn_on(brightness=191)
        await self.runtime.async_press()
        await self.hass.async_block_till_done()
        self.assertEqual(self.runtime._states(), ["off", "off", "on", "on"])
        await self.light.async_turn_off()
        await self.light.async_turn_on()
        self.assertEqual(self.runtime._states(), ["off", "off", "on", "on"])

    async def test_restart_keeps_pattern_without_commands(self):
        from custom_components.home_control.light import GroupLight
        from custom_components.home_control.runtime import HomeControlRuntime

        await self.runtime.async_set_enabled(True)
        await self.light.async_turn_on(brightness=128)
        await self.light.async_turn_off()
        await self.runtime.async_stop()
        self.commands.clear()
        self.runtime = HomeControlRuntime(self.hass, self.entry)
        await self.runtime.async_start()
        self.light = GroupLight(self.runtime)
        self.assertTrue(self.runtime.controller.enabled)
        self.assertEqual(self.commands, [])
        await self.light.async_turn_on()
        self.assertEqual(self.runtime._states(), ["off", "off", "on", "on"])

    async def test_disabled_and_unknown_block_commands(self):
        with self.assertRaises(Exception):
            await self.light.async_turn_on()
        self.assertEqual(self.commands, [])
        await self.runtime.async_set_enabled(True)
        self.hass.states.async_set(self.groups[0], "unavailable")
        self.assertFalse(self.light.available)
        with self.assertRaises(Exception):
            await self.light.async_turn_on(brightness=128)
        self.assertEqual(self.commands, [])

    async def test_hold_is_independent_of_group_light(self):
        await self.runtime.async_set_enabled(True)
        await self.light.async_turn_on(brightness=64)
        self.runtime.submit_press("mqtt", "HOLD")
        await self.hass.async_block_till_done()
        await self.light.async_turn_off()
        self.assertEqual(self.hass.states.get("switch.night").state, "on")
        await self.light.async_turn_on()
        self.assertEqual(self.runtime._states(), ["off", "off", "off", "on"])

    async def test_partial_manual_pattern_is_restored(self):
        await self.runtime.async_set_enabled(True)
        self.hass.states.async_set(self.groups[0], "on")
        self.hass.states.async_set(self.groups[2], "on")
        await self.light.async_turn_off()
        await self.light.async_turn_on()
        self.assertEqual(self.runtime._states(), ["on", "off", "on", "off"])

    async def test_unconfirmed_command_does_not_replace_memory(self):
        await self.runtime.async_set_enabled(True)
        await self.light.async_turn_on(brightness=64)
        self.feedback = False
        await self.light.async_turn_on(brightness=191)
        self.assertEqual(self.runtime.controller.last_pattern, ("off", "off", "off", "on"))
        self.runtime.controller.feedback_due = 0
        self.runtime.controller.check_feedback()
        self.assertEqual(self.runtime.controller.last_error, "feedback_timeout")

    async def test_one_two_three_groups_and_changed_mapping(self):
        from custom_components.home_control.light import GroupLight
        from custom_components.home_control.runtime import HomeControlRuntime

        for n in (1, 2, 3):
            await self.runtime.async_stop()
            for key in self.groups:
                self.hass.states.async_set(key, "off")
            self.entry.options = {
                f"group_{i}": self.groups[i - 1] if i <= n else "" for i in range(1, 5)
            }
            self.runtime = HomeControlRuntime(self.hass, self.entry)
            await self.runtime.async_start()
            await self.runtime.async_set_enabled(True)
            self.light = GroupLight(self.runtime)
            for count in range(1, n + 1):
                await self.light.async_turn_on(brightness=round(255 * count / n))
                self.assertEqual(self.runtime._states(), ["off"] * (n - count) + ["on"] * count)

    async def test_disable_between_off_and_on_cancels_remaining_command(self):
        await self.runtime.async_set_enabled(True)
        self.hass.states.async_set(self.groups[0], "on")
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(call):
            self.commands.append((call.service, call.data["entity_id"]))
            started.set()
            await release.wait()

        self.hass.services.async_register("switch", "turn_off", blocked)
        task = asyncio.create_task(self.light.async_turn_on(brightness=64))
        await started.wait()
        disable = asyncio.create_task(self.runtime.async_set_enabled(False))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, disable)
        self.assertEqual(len(self.commands), 1)

    async def test_kitchen_has_no_brightness_entity(self):
        from custom_components.home_control.light import async_setup_entry
        from custom_components.home_control.runtime import HomeControlRuntime

        entry = SimpleNamespace(
            data=self.config | {"mode": "kitchen"}, options={}, entry_id="k", title="Kitchen"
        )
        entry.runtime_data = HomeControlRuntime(self.hass, entry)
        added = []
        await async_setup_entry(self.hass, entry, added.extend)
        self.assertEqual(added, [])
