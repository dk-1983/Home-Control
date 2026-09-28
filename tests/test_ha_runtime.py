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
        self.assertEqual(form["type"], "menu")
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
            self.assertEqual(
                any(s.entity_id.startswith("button.lifecycle_") for s in entities),
                self.config.get("process_type") not in ("motion", "shared_fan", "humidity", "hood"),
            )
            group_lights = [s for s in entities if s.entity_id.startswith("light.lifecycle")]
            has_group_light = (
                self.config.get("process_type") is None and self.config.get("mode") != "kitchen"
            )
            self.assertEqual(bool(group_lights), has_group_light)
            if has_group_light:
                self.assertEqual(
                    group_lights[0].attributes["supported_color_modes"], ["brightness"]
                )
            if self.config.get("process_type") == "valve_exercise":
                protection = [
                    s for s in entities if s.entity_id.startswith("binary_sensor.lifecycle")
                ]
                self.assertEqual(len(protection), 1)
                self.assertEqual(protection[0].state, "off")
            loaded_runtime = entry.runtime_data
            self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
            self.assertFalse(loaded_runtime.controller.enabled)
            self.assertIsNone(getattr(loaded_runtime, "_timer", None))
            if self.config.get("process_type") == "hood":
                self.assertTrue(any(s.entity_id.startswith("fan.lifecycle") for s in entities))
                self.assertIsNone(loaded_runtime._input_timer)

    def _configure_night_light(self):
        self.runtime.config["night_light"] = "light.night_light"
        self.hass.states.async_set("light.night_light", "off")
        calls = []
        self.night_services = []

        async def turn_on(call):
            calls.append(dict(call.data))
            self.night_services.append(call.service)

        self.hass.services.async_register("light", "turn_on", turn_on)
        self.hass.services.async_register("light", "turn_off", turn_on)
        return calls

    def _hold_message(self, action="HOLD", retain=False):
        import json

        self.runtime._mqtt_message(
            SimpleNamespace(payload=json.dumps({"Button4": {"Action": action}}), retain=retain)
        )

    async def test_hold_turns_on_only_night_light_and_preserves_selection(self):
        calls = self._configure_night_light()
        await self.runtime.async_set_enabled(True)
        await self.runtime.async_press()
        deadline = self.runtime.controller.deadline
        self._hold_message()
        self._hold_message("CLEAR")
        await self.hass.async_block_till_done()
        self.assertEqual(calls, [{"entity_id": "light.night_light"}])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.runtime.controller.step, 4)
        self.assertEqual(self.runtime.controller.deadline, deadline)
        self.assertEqual(self.runtime.controller.last_source, "mqtt_hold")

    async def test_hold_retained_disabled_and_other_actions_are_ignored(self):
        calls = self._configure_night_light()
        self._hold_message()
        await self.runtime.async_set_enabled(True)
        self._hold_message(retain=True)
        self._hold_message("CLEAR")
        self._hold_message("DOUBLE")
        self._hold_message({"unexpected": "object"})
        await self.hass.async_block_till_done()
        self.assertEqual(calls, [])
        self.assertEqual(self.commands, [])

    async def test_old_config_without_night_light_still_handles_single(self):
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        self._hold_message("SINGLE")
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.commands), 1)
        self.assertIsNone(self.runtime.controller.last_error)

    async def test_repeated_hold_toggles_from_on_despite_stale_feedback(self):
        calls = self._configure_night_light()
        self.hass.states.async_set("light.night_light", "on")
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        self._hold_message()
        await self.hass.async_block_till_done()
        self.assertEqual(calls, [{"entity_id": "light.night_light"}] * 2)
        self.assertEqual(self.night_services, ["turn_off", "turn_on"])
        self.assertEqual(self.commands, [])

    async def test_maintenance_drops_queued_hold_and_waits_for_inflight_hold(self):
        import asyncio

        calls = self._configure_night_light()
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked_turn_on(call):
            calls.append(dict(call.data))
            started.set()
            await release.wait()

        self.hass.services.async_register("light", "turn_on", blocked_turn_on)
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        await started.wait()
        self._hold_message()
        disabling = asyncio.create_task(self.runtime.async_set_enabled(False))
        await asyncio.sleep(0)
        self.assertFalse(self.runtime.controller.enabled)
        self.assertFalse(disabling.done())
        release.set()
        await disabling
        await self.hass.async_block_till_done()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.commands, [])

    async def test_unavailable_night_light_does_not_block_chandelier(self):
        calls = self._configure_night_light()
        self.hass.states.async_set("light.night_light", "unavailable")
        await self.runtime.async_set_enabled(True)
        with self.assertLogs("custom_components.home_control.controller", level="ERROR"):
            self._hold_message()
            await self.hass.async_block_till_done()
        self.assertEqual(self.runtime.controller.last_error, "night_light_command_failed")
        self.assertEqual(calls, [])
        await self.runtime.async_press()
        self.assertEqual(len(self.commands), 1)

    async def test_night_light_service_error_and_timeout_are_handled(self):
        import asyncio

        self._configure_night_light()
        await self.runtime.async_set_enabled(True)

        async def failed(call):
            raise RuntimeError("service failed")

        async def blocked(call):
            await asyncio.Event().wait()

        for handler in (failed, blocked):
            self.hass.services.async_register("light", "turn_on", handler)
            with (
                patch("custom_components.home_control.runtime.SERVICE_TIMEOUT", 0.01),
                self.assertLogs("custom_components.home_control.controller", level="ERROR"),
            ):
                self._hold_message()
                await self.hass.async_block_till_done()
            self.assertEqual(self.runtime.controller.last_error, "night_light_command_failed")
        self.assertTrue(self.runtime.controller.enabled)
        self.assertEqual(self.commands, [])

    async def test_night_light_validation_clear_and_diagnostics_redaction(self):
        from custom_components.home_control.config_flow import _validate
        from custom_components.home_control.diagnostics import async_get_config_entry_diagnostics

        self._configure_night_light()
        fake_hass = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        _, errors = _validate(fake_hass, self.config | {"night_light": "light.missing"})
        self.assertEqual(errors["night_light"], "missing_entity")
        self.entry.options = {"night_light": "light.night_light"}
        fake_hass.config_entries.async_entries = lambda domain: [self.entry]
        _, errors = _validate(fake_hass, self.config | self.entry.options)
        self.assertEqual(errors["night_light"], "night_light_in_use")
        cleaned, errors = _validate(fake_hass, self.config, exclude_id="test")
        self.assertEqual(cleaned["night_light"], "")
        self.assertEqual(errors, {})
        self.entry.runtime_data = self.runtime
        diagnostics = await async_get_config_entry_diagnostics(self.hass, self.entry)
        self.assertNotEqual(diagnostics["config"]["night_light"], "light.night_light")

    async def test_three_holds_toggle_from_off_despite_stale_feedback(self):
        self._configure_night_light()
        await self.runtime.async_set_enabled(True)
        for _ in range(3):
            self._hold_message()
        await self.hass.async_block_till_done()
        self.assertEqual(self.night_services, ["turn_on", "turn_off", "turn_on"])
        self.assertEqual(self.commands, [])

    async def test_expired_night_light_expectation_uses_actual_state(self):
        self._configure_night_light()
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        await self.hass.async_block_till_done()
        self.runtime._night_expected_until = self.hass.loop.time() - 1
        self._hold_message()
        await self.hass.async_block_till_done()
        self.assertEqual(self.night_services, ["turn_on", "turn_on"])

    async def test_maintenance_resets_night_light_expectation(self):
        self._configure_night_light()
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        await self.hass.async_block_till_done()
        await self.runtime.async_set_enabled(False)
        self.assertIsNone(self.runtime._night_expected)
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        await self.hass.async_block_till_done()
        self.assertEqual(self.night_services, ["turn_on", "turn_on"])

    async def test_failed_turn_off_discards_night_light_expectation(self):
        from homeassistant.exceptions import HomeAssistantError

        self._configure_night_light()
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        await self.hass.async_block_till_done()

        async def failed(call):
            raise HomeAssistantError("relay rejected off")

        self.hass.services.async_register("light", "turn_off", failed)
        with self.assertLogs("custom_components.home_control.controller", level="ERROR"):
            self._hold_message()
            await self.hass.async_block_till_done()
        self.assertIsNone(self.runtime._night_expected)
        self.assertEqual(self.runtime.controller.last_error, "night_light_command_failed")
        self._hold_message()
        await self.hass.async_block_till_done()
        self.assertEqual(self.night_services, ["turn_on", "turn_on"])

    async def test_switch_night_light_toggles_without_touching_chandelier(self):
        self.runtime.config["night_light"] = "switch.night_relay"
        self.hass.states.async_set("switch.night_relay", "off")
        await self.runtime.async_set_enabled(True)
        self._hold_message()
        self._hold_message()
        await self.hass.async_block_till_done()
        self.assertEqual(
            self.commands, [("turn_on", "switch.night_relay"), ("turn_off", "switch.night_relay")]
        )
        self.assertIsNone(self.runtime.controller.step)
        self.assertIsNone(self.runtime.controller.last_error)

    async def test_switch_night_light_selector_and_validation(self):
        import voluptuous as vol

        from custom_components.home_control.config_flow import _schema, _validate

        self.hass.states.async_set("switch.night_relay", "off")
        data = self.config | {"night_light": "switch.night_relay"}
        validated = vol.Schema(_schema(data))(data)
        fake_hass = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        _, errors = _validate(fake_hass, validated)
        self.assertEqual(errors, {})
        self.hass.states.async_set("sensor.not_a_light", "off")
        _, errors = _validate(fake_hass, data | {"night_light": "sensor.not_a_light"})
        self.assertEqual(errors["night_light"], "missing_entity")

    async def test_night_relay_cannot_overlap_groups_or_automation_switch(self):
        from custom_components.home_control.config_flow import _validate

        fake_hass = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        _, errors = _validate(fake_hass, self.config | {"night_light": "switch.group_1"})
        self.assertEqual(errors["night_light"], "night_light_in_use")
        self.hass.states.async_set("switch.automation", "on", {"process_type": "local_chandelier"})
        _, errors = _validate(fake_hass, self.config | {"night_light": "switch.automation"})
        self.assertEqual(errors["night_light"], "automation_not_light")

    async def test_cross_process_night_relay_ownership_in_both_directions(self):
        from custom_components.home_control.config_flow import _validate

        other = SimpleNamespace(
            entry_id="other",
            data={
                **self.config,
                **{f"group_{i}": f"switch.other_{i}" for i in range(1, 5)},
                "night_light": "switch.other_night",
                "mqtt_topic": "other/RESULT",
                "input_button": "",
            },
            options={},
        )
        for entity_id in ("switch.other_1", "switch.other_night"):
            self.hass.states.async_set(entity_id, "off")
        fake_hass = SimpleNamespace(
            states=self.hass.states,
            config_entries=SimpleNamespace(async_entries=lambda domain: [other]),
        )
        _, errors = _validate(fake_hass, self.config | {"night_light": "switch.other_1"})
        self.assertEqual(errors["night_light"], "night_light_in_use")
        _, errors = _validate(fake_hass, self.config | {"group_1": "switch.other_night"})
        self.assertEqual(errors["base"], "groups_in_use")

    async def test_group_form_clears_optional_slots_without_restoring_old_defaults(self):
        import voluptuous as vol

        from custom_components.home_control.config_flow import _schema, _validate
        from custom_components.home_control.const import selected_groups

        schema = vol.Schema(_schema(self.config))
        data = {k: v for k, v in self.config.items() if k not in ("group_2", "group_3", "group_4")}
        submitted = schema(data)
        self.assertNotIn("group_2", submitted)
        fake_hass = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        options, errors = _validate(fake_hass, submitted)
        self.assertEqual(errors, {})
        self.assertEqual(selected_groups(self.config | options), ("switch.group_1",))
        for key in ("group_2", "group_3", "group_4"):
            self.assertEqual(options[key], "")
        _, errors = _validate(fake_hass, data | {"group_1": ""})
        self.assertEqual(errors["group_1"], "group_required")

    async def test_sparse_group_fields_keep_order_and_ignore_blank_ownership(self):
        from custom_components.home_control.config_flow import _validate
        from custom_components.home_control.const import selected_groups

        other = SimpleNamespace(
            entry_id="other",
            data={
                "group_1": "switch.other",
                "mqtt_topic": "other/RESULT",
                "mqtt_button": "Button4",
            },
            options={},
        )
        fake_hass = SimpleNamespace(
            states=self.hass.states,
            config_entries=SimpleNamespace(async_entries=lambda domain: [other]),
        )
        data = self.config | {"group_2": "", "group_4": ""}
        cleaned, errors = _validate(fake_hass, data)
        self.assertEqual(errors, {})
        self.assertEqual(selected_groups(cleaned), ("switch.group_1", "switch.group_3"))

    async def test_runtime_reduces_existing_four_group_entry_through_options(self):
        from custom_components.home_control.runtime import HomeControlRuntime

        self.entry.options = {"group_2": "", "group_4": ""}
        await self.runtime.async_stop()
        self.runtime = HomeControlRuntime(self.hass, self.entry)
        await self.runtime.async_start()
        await self.runtime.async_set_enabled(True)
        for _ in range(4):
            await self.runtime.async_press()
        self.assertEqual(
            self.commands,
            [
                ("turn_on", ["switch.group_1", "switch.group_3"]),
                ("turn_off", ["switch.group_1"]),
                ("turn_off", ["switch.group_3"]),
                ("turn_on", ["switch.group_1", "switch.group_3"]),
            ],
        )

    async def test_kitchen_config_flow_mode_defaults_and_four_required(self):
        import voluptuous as vol

        from custom_components.home_control.config_flow import (
            HomeControlConfigFlow,
            _schema,
            _validate,
        )

        fake_hass = SimpleNamespace(
            states=self.hass.states, config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        submitted = vol.Schema(_schema({}))(
            {k: v for k, v in self.config.items() if k != "selection_window"} | {"mode": "kitchen"}
        )
        self.assertEqual(submitted["selection_window"], 3)
        flow = HomeControlConfigFlow()
        flow.hass = fake_hass
        result = await flow.async_step_user(submitted | {"name": "Kitchen"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["mode"], "kitchen")
        _, errors = _validate(fake_hass, submitted | {"group_4": ""})
        self.assertEqual(errors["base"], "kitchen_requires_four")
        old, errors = _validate(fake_hass, self.config)
        self.assertEqual(errors, {})
        self.assertEqual(old["mode"], "chandelier")
        self.hass.states.async_set(
            "switch.kitchen_automation", "on", {"process_type": "local_kitchen"}
        )
        _, errors = _validate(fake_hass, submitted | {"night_light": "switch.kitchen_automation"})
        self.assertEqual(errors["night_light"], "automation_not_light")

    async def test_kitchen_mqtt_cycle_gate_and_cleanup(self):
        from custom_components.home_control.runtime import HomeControlRuntime

        await self.runtime.async_stop()
        self.entry.options = {"mode": "kitchen", "mqtt_button": "Button1"}
        self.runtime = HomeControlRuntime(self.hass, self.entry)
        await self.runtime.async_start()
        message = SimpleNamespace(payload='{"Button1":{"Action":"SINGLE"}}', retain=False)
        self.runtime._mqtt_message(message)
        await self.hass.async_block_till_done()
        self.assertEqual(self.commands, [])
        await self.runtime.async_set_enabled(True)
        for _ in range(7):
            self.runtime._mqtt_message(message)
        await self.hass.async_block_till_done()
        groups = [f"switch.group_{i}" for i in range(1, 5)]
        self.assertEqual(
            self.commands,
            [
                ("turn_on", groups[:1]),
                ("turn_on", groups[1:2]),
                ("turn_on", groups[2:3]),
                ("turn_off", groups[:2]),
                ("turn_off", groups[2:3]),
                ("turn_on", groups[3:]),
                ("turn_off", groups),
                ("turn_on", groups[:1]),
            ],
        )
        self.assertEqual(self.runtime.attributes["process_type"], "local_kitchen")
        self.assertEqual(self.runtime.attributes["selection_step"], 1)
        await self.runtime.async_stop()
        self.assertIsNone(self.runtime._timer)
        self.assertIsNone(self.runtime._selection_timer)
        self.runtime._mqtt_message(message)
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.commands), 8)

    async def test_kitchen_loads_real_switch_and_button_platforms(self):
        self.config["mode"] = "kitchen"
        await self.test_config_entry_loads_real_switch_and_button_platforms()
