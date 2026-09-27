"""Real HA adapters with fake MQTT transport and fake relay services.

Runs when Home Assistant is installed; otherwise skipped. No real broker or
hardware is contacted. All test state goes to the repository's ignored work/.
"""

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

HAS_HA = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(HAS_HA, "Home Assistant is not installed in this interpreter")
class HomeAssistantRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant

        from custom_components.home_control.runtime import HomeControlRuntime

        work = Path(__file__).parents[1] / "work"
        work.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=work, prefix="ha-test-")
        self.hass = HomeAssistant(self.temp.name)
        self.config = {
            **{f"group_{i}": f"switch.group_{i}" for i in range(1, 5)},
            "mqtt_topic": "test/stat/RESULT",
            "mqtt_button": "Button4",
            "input_button": "input_button.test",
            "selection_window": 3.0,
            "feedback_timeout": 5.0,
        }
        self.entry = SimpleNamespace(data=self.config, options={}, entry_id="test", title="Test")
        self.commands = []
        for i in range(1, 5):
            self.hass.states.async_set(f"switch.group_{i}", "off")
        self.hass.states.async_set("input_button.test", "unknown")

        async def service(call):
            self.commands.append((call.service, call.data["entity_id"]))

        self.hass.services.async_register("switch", "turn_on", service)
        self.hass.services.async_register("switch", "turn_off", service)
        self.unsubscribe = MagicMock()
        self.subscription = AsyncMock(return_value=self.unsubscribe)
        self.patch = patch(
            "custom_components.home_control.runtime.mqtt.async_subscribe", self.subscription
        )
        self.patch.start()
        self.runtime = HomeControlRuntime(self.hass, self.entry)
        await self.runtime.async_start()

    async def asyncTearDown(self):
        await self.runtime.async_stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.patch.stop()
        self.temp.cleanup()

    async def test_startup_disabled_and_mqtt_subscription_correct(self):
        self.assertFalse(self.runtime.controller.enabled)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.subscription.await_args.args[1], "test/stat/RESULT")
        self.assertEqual(self.subscription.await_args.kwargs, {"qos": 0})

    async def test_enabled_and_disabled_persist_across_runtime_reload(self):
        from custom_components.home_control.runtime import HomeControlRuntime

        for enabled in (True, False):
            await self.runtime.async_set_enabled(enabled)
            await self.runtime.async_stop()
            self.runtime = HomeControlRuntime(self.hass, self.entry)
            await self.runtime.async_start()
            self.assertEqual(self.runtime.controller.enabled, enabled)
        self.assertEqual(self.commands, [])

    async def test_mqtt_to_service_with_unchanged_ha_feedback(self):
        await self.runtime.async_set_enabled(True)
        message = SimpleNamespace(payload='{"Button4": {"Action": "SINGLE"}}', retain=False)
        self.runtime._mqtt_message(message)
        self.runtime._mqtt_message(message)
        await self.hass.async_block_till_done()
        self.assertEqual(
            self.commands,
            [
                ("turn_on", [f"switch.group_{i}" for i in range(1, 5)]),
                ("turn_off", ["switch.group_1"]),
            ],
        )

    async def test_retained_message_and_disabled_native_button_do_nothing(self):
        await self.runtime.async_press()
        await self.runtime.async_set_enabled(True)
        self.runtime._mqtt_message(
            SimpleNamespace(payload='{"Button4":{"Action":"SINGLE"}}', retain=True)
        )
        await self.hass.async_block_till_done()
        self.assertEqual(self.commands, [])

    async def test_first_real_input_button_press_from_unknown_is_accepted(self):
        from homeassistant.util import dt as dt_util

        await self.runtime.async_set_enabled(True)
        self.hass.states.async_set("input_button.test", dt_util.utcnow().isoformat())
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.commands), 1)

    async def test_restored_input_button_timestamp_and_attribute_update_are_ignored(self):
        await self.runtime.async_set_enabled(True)
        self.hass.states.async_set("input_button.test", "2020-01-01T00:00:00+00:00")
        self.hass.states.async_set(
            "input_button.test", "2020-01-01T00:00:00+00:00", {"friendly_name": "Changed"}
        )
        await self.hass.async_block_till_done()
        self.assertEqual(self.commands, [])

    async def test_unload_unsubscribes_and_cancels_timers(self):
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        await self.runtime.async_stop()
        self.unsubscribe.assert_called_once()
        self.assertIsNone(self.runtime._timer)
        self.assertIsNone(self.runtime._selection_timer)
        self.hass.states.async_set("input_button.test", "2026-09-27T00:00:00+00:00")
        await self.runtime.async_press()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.commands), 1)

    async def test_storage_failure_keeps_automation_disabled(self):
        with patch.object(
            self.runtime.store, "async_save", AsyncMock(side_effect=OSError("disk full"))
        ):
            with self.assertRaises(OSError):
                await self.runtime.async_set_enabled(True)
        self.assertFalse(self.runtime.controller.enabled)
        self.assertEqual(self.runtime.controller.last_error, "storage_failed")
        self.assertEqual(self.commands, [])

    async def test_repeated_enable_does_not_reset_selection(self):
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        await self.runtime.async_set_enabled(True)
        self.assertEqual(self.runtime.controller.step, 4)

    async def test_switch_and_button_entities_use_runtime_gate(self):
        from custom_components.home_control.button import ChandelierButton
        from custom_components.home_control.switch import AutomationSwitch

        switch = AutomationSwitch(self.runtime)
        button = ChandelierButton(self.runtime)
        self.assertFalse(switch.is_on)
        self.assertFalse(button.available)
        await switch.async_turn_on()
        self.assertTrue(button.available)
        await button.async_press()
        await switch.async_turn_off()
        self.assertFalse(switch.is_on)
        self.assertEqual(len(self.commands), 1)

    async def test_config_validation_rejects_duplicate_groups_and_sources(self):
        from custom_components.home_control.config_flow import _validate

        fake_hass = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        data, errors = _validate(fake_hass, self.config)
        self.assertEqual(errors, {})
        _, errors = _validate(fake_hass, self.config | {"group_2": "switch.group_1"})
        self.assertEqual(errors["base"], "duplicate_groups")
        fake_hass.config_entries.async_entries = lambda domain: [self.entry]
        _, errors = _validate(fake_hass, self.config)
        self.assertEqual(errors["base"], "groups_in_use")
        self.assertEqual(errors["mqtt_topic"], "source_in_use")
        _, errors = _validate(fake_hass, self.config, exclude_id="test")
        self.assertEqual(errors, {})

    async def test_user_config_flow_returns_form_and_creates_entry(self):
        from custom_components.home_control.config_flow import HomeControlConfigFlow

        flow = HomeControlConfigFlow()
        flow.hass = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        form = await flow.async_step_user()
        self.assertEqual(form["type"], "form")
        result = await flow.async_step_user(self.config | {"name": "Hall"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Hall")

    async def test_config_entry_loads_real_switch_and_button_platforms(self):
        import shutil
        from types import MappingProxyType

        from homeassistant import loader
        from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
        from homeassistant.setup import async_setup_component

        shutil.copytree(
            Path(__file__).parents[1] / "custom_components",
            Path(self.temp.name) / "custom_components",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        loader.async_setup(self.hass)
        descriptions = await loader.async_get_integration_descriptions(self.hass)
        self.assertIn("home_control", descriptions["custom"]["integration"])
        self.assertNotIn("home_control", descriptions["custom"]["helper"])
        from homeassistant.helpers import area_registry, device_registry, entity_registry

        await area_registry.async_load(self.hass)
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        entry = ConfigEntry(
            domain="home_control",
            title="Lifecycle",
            data=self.config,
            options={},
            version=1,
            minor_version=1,
            source="user",
            unique_id=None,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        with (
            patch(
                "custom_components.home_control.mqtt.async_wait_for_mqtt_client",
                AsyncMock(return_value=True),
            ),
            patch("homeassistant.setup.async_process_deps_reqs", AsyncMock()),
        ):
            await async_setup_component(self.hass, "homeassistant", {})
            await self.hass.config_entries.async_add(entry)
            await self.hass.async_block_till_done()
            self.assertEqual(entry.state, ConfigEntryState.LOADED)
            entities = self.hass.states.async_all()
            self.assertTrue(any(s.entity_id.startswith("switch.lifecycle_") for s in entities))
            self.assertTrue(any(s.entity_id.startswith("button.lifecycle_") for s in entities))
            loaded_runtime = entry.runtime_data
            self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
            self.assertFalse(loaded_runtime.controller.enabled)
            self.assertIsNone(loaded_runtime._timer)
