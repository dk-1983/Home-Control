"""Entity mapping and exclusive relay ownership for the hood."""

import voluptuous as vol
from homeassistant.helpers import selector

from .hood import DEFAULTS, INPUT_KEYS, OUTPUT_KEYS
from .hood_readback import ReadbackError, resolve_binding
from .process_config import owned_outputs


def hood_schema(values, *, name=False):
    fields = {}
    if name:
        fields[vol.Required("name", default=values.get("name", ""))] = str
    for key in OUTPUT_KEYS:
        marker = vol.Required(key, default=values[key]) if values.get(key) else vol.Required(key)
        fields[marker] = selector.EntitySelector(selector.EntitySelectorConfig(domain="switch"))
    for key in INPUT_KEYS:
        fields[vol.Optional(key, description={"suggested_value": values.get(key, "")})] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor"))
        )
    for key, limits in {
        "feedback_timeout": (2, 120),
        "break_delay": (0.1, 10),
        "input_settle": (0.1, 30),
    }.items():
        fields[vol.Required(key, default=values.get(key, DEFAULTS[key]))] = vol.All(
            vol.Coerce(float), vol.Range(min=limits[0], max=limits[1])
        )
    return vol.Schema(fields)


def validate_hood(hass, values, *, exclude_id=None):
    data = DEFAULTS | dict(values) | {"process_type": "hood"}
    errors = {}
    for key in INPUT_KEYS:
        data[key] = data.get(key) or ""
    outputs = [data.get(key) for key in OUTPUT_KEYS]
    inputs = [data[key] for key in INPUT_KEYS if data[key]]
    if len(set(outputs)) != 4 or len(set(inputs)) != len(inputs):
        errors["base"] = "duplicate_groups"
    if inputs and len(inputs) != 4:
        errors["base"] = "hood_inputs_complete"
    for key in (*OUTPUT_KEYS, *INPUT_KEYS):
        entity = data.get(key)
        if not entity and key in INPUT_KEYS:
            continue
        state = hass.states.get(entity) if isinstance(entity, str) else None
        domain = "switch" if key in OUTPUT_KEYS else "binary_sensor"
        if state is None or entity.split(".")[0] != domain:
            errors[key] = "missing_entity"
        elif str(state.attributes.get("process_type", "")).startswith("local_"):
            errors[key] = "automation_not_light"

    if not errors:
        try:
            resolve_binding(hass, outputs)
        except ReadbackError:
            errors["base"] = "hood_readback_required"
    for entry in hass.config_entries.async_entries("home_control"):
        if entry.entry_id == exclude_id:
            continue
        other = dict(entry.data) | dict(entry.options)
        if set(outputs) & owned_outputs(other):
            errors["base"] = "groups_in_use"
        if other.get("process_type") == "hood" and set(inputs) & {
            other.get(key) for key in INPUT_KEYS
        }:
            errors["base"] = "source_in_use"
    return data, errors
