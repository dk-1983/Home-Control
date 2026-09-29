"""Scalable valve exercise groups and fixed monthly calendar."""

import calendar
from datetime import timedelta

import voluptuous as vol
from homeassistant.helpers import selector

from .voice_preferences import voice_fields

DEFAULTS = {
    "schedule_day": 1,
    "schedule_time": "03:00:00",
    "retry_hours": 1.0,
    "movement_timeout": 15.0,
    "closed_hold": 5.0,
    "between_valves": 10.0,
}


def next_schedule(now, config):
    hour, minute, second = map(int, config["schedule_time"].split(":"))
    candidate = now.replace(
        day=min(config["schedule_day"], calendar.monthrange(now.year, now.month)[1]),
        hour=hour,
        minute=minute,
        second=second,
        microsecond=0,
    )
    if candidate <= now:
        following = now.replace(day=28) + timedelta(days=4)
        candidate = following.replace(
            day=min(
                config["schedule_day"], calendar.monthrange(following.year, following.month)[1]
            ),
            hour=hour,
            minute=minute,
            second=second,
            microsecond=0,
        )
    return candidate


def protection_state(states):
    if "on" in states:
        return "on"
    return "off" if states and all(s == "off" for s in states) else "unknown"


def schema(values, name=False):
    fields = (
        {vol.Required("name", default=values.get("name", "Valve exercise")): str} if name else {}
    )
    for key, domain in (("valves", "switch"), ("leak_sensors", "binary_sensor")):
        fields[vol.Required(key, default=values.get(key, []))] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=domain, multiple=True)
        )
    for key, bounds in (
        ("schedule_day", (1, 31)),
        ("retry_hours", (0.1, 168)),
        ("movement_timeout", (1, 300)),
        ("closed_hold", (0, 300)),
        ("between_valves", (0, 300)),
    ):
        fields[vol.Required(key, default=values.get(key, DEFAULTS[key]))] = vol.All(
            vol.Coerce(int if key == "schedule_day" else float),
            vol.Range(min=bounds[0], max=bounds[1]),
        )
    fields[
        vol.Required(
            "schedule_time", default=values.get("schedule_time", DEFAULTS["schedule_time"])
        )
    ] = selector.TimeSelector()
    fields.update(voice_fields(values))
    return vol.Schema(fields)


def validate(hass, values, exclude_id=None):
    from .process_config import owned_outputs

    data = DEFAULTS | dict(values) | {"process_type": "valve_exercise"}
    data.setdefault("voice_area", "")
    errors = {}
    for key, domain in (("valves", "switch"), ("leak_sensors", "binary_sensor")):
        ids = data.get(key, [])
        if not ids or len(ids) != len(set(ids)):
            errors[key] = "valve_entities"
        for entity in ids:
            state = hass.states.get(entity)
            if (
                not entity.startswith(domain + ".")
                or state is None
                or str(state.attributes.get("process_type", "")).startswith("local_")
            ):
                errors[key] = "valve_entities"
    for entry in hass.config_entries.async_entries("home_control"):
        if entry.entry_id != exclude_id and set(data.get("valves", [])) & owned_outputs(
            dict(entry.data) | dict(entry.options)
        ):
            errors["valves"] = "groups_in_use"
    return data, errors


class ValveFlowMixin:
    async def async_step_valve_exercise(self, user_input=None):
        options = hasattr(self, "config_entry")
        current = dict(self.config_entry.data) | dict(self.config_entry.options) if options else {}
        errors = {}
        if user_input is not None:
            data, errors = validate(
                self.hass, user_input, self.config_entry.entry_id if options else None
            )
            name = data.pop("name", "").strip()
            if not options and not name:
                errors["name"] = "invalid_name"
            if not errors:
                return self.async_create_entry(title=name, data=data)
        return self.async_show_form(
            step_id="valve_exercise",
            data_schema=schema(current if user_input is None else user_input, name=not options),
            errors=errors,
        )
