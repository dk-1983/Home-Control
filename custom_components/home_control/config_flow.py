"""UI setup and options for an ordered one-to-four-group chandelier."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DEFAULT_FEEDBACK_TIMEOUT, DEFAULT_WINDOW, DOMAIN, GROUP_KEYS, selected_groups


def _schema(values):
    fields = {}
    for key in GROUP_KEYS:
        if key == GROUP_KEYS[0]:
            marker = (
                vol.Required(key, default=values[key]) if values.get(key) else vol.Required(key)
            )
        else:
            marker = vol.Optional(key, description={"suggested_value": values.get(key, "")})
        fields[marker] = selector.EntitySelector(selector.EntitySelectorConfig(domain="switch"))
    fields[vol.Required("mqtt_topic", default=values.get("mqtt_topic", ""))] = str
    fields[vol.Required("mqtt_button", default=values.get("mqtt_button", "Button4"))] = str
    fields[
        vol.Optional(
            "input_button", description={"suggested_value": values.get("input_button", "")}
        )
    ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="input_button"))
    fields[
        vol.Required("selection_window", default=values.get("selection_window", DEFAULT_WINDOW))
    ] = vol.All(vol.Coerce(float), vol.Range(min=0.2, max=30))
    fields[
        vol.Required(
            "feedback_timeout", default=values.get("feedback_timeout", DEFAULT_FEEDBACK_TIMEOUT)
        )
    ] = vol.All(vol.Coerce(float), vol.Range(min=1, max=60))
    fields[
        vol.Optional("night_light", description={"suggested_value": values.get("night_light", "")})
    ] = selector.EntitySelector(selector.EntitySelectorConfig(domain=["light", "switch"]))
    return fields


def _validate(hass, values, *, exclude_id=None):
    data = dict(values)
    for key in GROUP_KEYS:
        data[key] = data.get(key) or ""
    data["input_button"] = data.get("input_button", "")
    data["night_light"] = data.get("night_light") or ""
    data["mqtt_topic"] = data["mqtt_topic"].strip()
    data["mqtt_button"] = data["mqtt_button"].strip()
    errors = {}
    try:
        mqtt.valid_publish_topic(data["mqtt_topic"])
    except vol.Invalid:
        errors["mqtt_topic"] = "invalid_topic"
    if not data["mqtt_button"]:
        errors["mqtt_button"] = "invalid_button"
    configured = selected_groups(data)
    groups = set(configured)
    if not data["group_1"]:
        errors["group_1"] = "group_required"
    if len(groups) != len(configured):
        errors["base"] = "duplicate_groups"
    for entity_id in groups:
        if not entity_id.startswith("switch.") or hass.states.get(entity_id) is None:
            errors["base"] = "missing_entity"
    if data["input_button"] and hass.states.get(data["input_button"]) is None:
        errors["input_button"] = "missing_entity"
    if data["night_light"] and (
        not data["night_light"].startswith(("light.", "switch."))
        or hass.states.get(data["night_light"]) is None
    ):
        errors["night_light"] = "missing_entity"
    if data["night_light"] in groups:
        errors["night_light"] = "night_light_in_use"
    if (night_state := hass.states.get(data["night_light"])) is not None:
        if night_state.attributes.get("process_type") == "local_chandelier":
            errors["night_light"] = "automation_not_light"
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_id:
            continue
        other = dict(entry.data) | dict(entry.options)
        other_groups = set(selected_groups(other))
        if data["night_light"] and (
            data["night_light"] == other.get("night_light") or data["night_light"] in other_groups
        ):
            errors["night_light"] = "night_light_in_use"
        if groups.intersection(other_groups) or other.get("night_light") in groups:
            errors["base"] = "groups_in_use"
        if (data["mqtt_topic"], data["mqtt_button"]) == (other["mqtt_topic"], other["mqtt_button"]):
            errors["mqtt_topic"] = "source_in_use"
        if data["input_button"] and data["input_button"] == other.get("input_button"):
            errors["input_button"] = "source_in_use"
    return data, errors


class HomeControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HomeControlOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            data, errors = _validate(self.hass, user_input)
            name = data.pop("name").strip()
            if not name:
                errors["name"] = "invalid_name"
            if not errors:
                return self.async_create_entry(title=name, data=data)
        values = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=values.get("name", "Hall chandelier")): str,
                    **_schema(values),
                }
            ),
            errors=errors,
        )


class HomeControlOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            data, errors = _validate(self.hass, user_input, exclude_id=self.config_entry.entry_id)
            if not errors:
                return self.async_create_entry(title="", data=data)
        values = (
            user_input
            if user_input is not None
            else dict(self.config_entry.data) | dict(self.config_entry.options)
        )
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(_schema(values)), errors=errors
        )
