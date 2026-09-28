"""Configuration of independent environmental processes and output ownership."""

import voluptuous as vol
from homeassistant.helpers import selector

from .const import GROUP_KEYS

PROCESS_TYPES = ("motion", "shared_fan", "humidity")
DEFAULTS = {
    "feedback_timeout": 5.0,
    "light_off_delay": 600.0,
    "fan_on_delay": 120.0,
    "fan_off_delay": 60.0,
    "sample_interval": 60.0,
    "window_size": 5,
    "minimum_samples": 3,
    "rising_slope": 0.30,
    "falling_slope": -0.20,
    "stable_slope": 0.12,
    "stable_range": 0.8,
    "stable_windows_required": 3,
    "plateau_duration": 300.0,
    "uncertain_timeout_minutes": 20.0,
    "sensor_max_age": 600.0,
}
FIELDS = {
    "motion": {"motion_sensors": ("binary_sensor", True), "output": (["light", "switch"], False)},
    "shared_fan": {
        "lights": (["light", "switch"], True),
        "boost_fans": ("switch", True),
        "output": ("switch", False),
    },
    "humidity": {
        "room_light": (["light", "switch"], False),
        "humidity_sensor": ("sensor", False),
        "output": ("switch", False),
    },
}
TIMINGS = {
    "motion": {"light_off_delay": (0, 7200)},
    "shared_fan": {"fan_on_delay": (0, 3600), "fan_off_delay": (0, 7200)},
    "humidity": {
        "sample_interval": (10, 600),
        "window_size": (3, 30),
        "minimum_samples": (3, 30),
        "rising_slope": (0.01, 10),
        "falling_slope": (-10, -0.01),
        "stable_slope": (0, 5),
        "stable_range": (0, 10),
        "stable_windows_required": (1, 30),
        "plateau_duration": (60, 3600),
        "uncertain_timeout_minutes": (1, 180),
        "sensor_max_age": (30, 3600),
    },
}
INTEGERS = {"window_size", "minimum_samples", "stable_windows_required"}


def owned_outputs(data):
    if data.get("process_type") == "hood":
        return {
            data[key] for key in ("speed_25", "speed_50", "speed_75", "speed_100") if data.get(key)
        }
    if data.get("process_type") in PROCESS_TYPES:
        return {data["output"]} if data.get("output") else set()
    return {data[key] for key in (*GROUP_KEYS, "night_light") if data.get(key)}


def process_schema(kind, values, *, name=False):
    fields = {}
    if name:
        fields[vol.Required("name", default=values.get("name", ""))] = str
    for key, (domain, multiple) in FIELDS[kind].items():
        marker = vol.Required(key, default=values[key]) if values.get(key) else vol.Required(key)
        fields[marker] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=domain, multiple=multiple)
        )
    for key, (low, high) in TIMINGS[kind].items():
        fields[vol.Required(key, default=values.get(key, DEFAULTS[key]))] = vol.All(
            vol.Coerce(int if key in INTEGERS else float), vol.Range(min=low, max=high)
        )
    fields[vol.Required("feedback_timeout", default=values.get("feedback_timeout", 5))] = vol.All(
        vol.Coerce(float), vol.Range(min=1, max=60)
    )
    return vol.Schema(fields)


def validate_process(hass, kind, values, *, exclude_id=None):
    data = dict(values)
    errors = {}
    data["process_type"] = kind
    for key in (*TIMINGS[kind], "feedback_timeout"):
        data.setdefault(key, DEFAULTS[key])
    for key, (domains, multiple) in FIELDS[kind].items():
        ids = data.get(key, []) if multiple else [data.get(key)]
        domains = [domains] if isinstance(domains, str) else domains
        if not ids or (multiple and not isinstance(ids, list)):
            errors[key] = "missing_entity"
            continue
        if len(set(ids)) != len(ids):
            errors[key] = "duplicate_groups"
        for entity_id in ids:
            state = hass.states.get(entity_id) if isinstance(entity_id, str) else None
            if state is None or entity_id.split(".")[0] not in domains:
                errors[key] = "missing_entity"
            elif str(state.attributes.get("process_type", "")).startswith("local_"):
                errors[key] = "automation_not_light"
            if key != "output" and entity_id == data.get("output"):
                errors[key] = "output_is_input"
    if kind == "humidity" and (
        data["minimum_samples"] > data["window_size"]
        or data["stable_slope"] >= min(data["rising_slope"], -data["falling_slope"])
        or data["sensor_max_age"] < data["sample_interval"]
    ):
        errors["base"] = "invalid_parameters"
    for entry in hass.config_entries.async_entries("home_control"):
        if entry.entry_id != exclude_id and data.get("output") in owned_outputs(
            dict(entry.data) | dict(entry.options)
        ):
            errors["output"] = "groups_in_use"
    return data, errors
