"""Door monitoring and optional LG filter notices."""

import voluptuous as vol
from homeassistant.helpers import selector

from .voice_preferences import voice_fields

DEFAULTS = {
    "door_delay": 120,
    "door_repeat": True,
    "door_repeat_minutes": 5,
    "announce_closed": False,
    "water_filter": False,
    "deodorizer": False,
}


def schema(values, name=False):
    values = DEFAULTS | values
    fields = {vol.Required("name", default=values.get("name", "Refrigerator")): str} if name else {}
    fields[
        vol.Required("fridge_door", description={"suggested_value": values.get("fridge_door")})
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="binary_sensor", device_class="door")
    )
    fields[
        vol.Optional(
            "fridge_notification",
            description={"suggested_value": values.get("fridge_notification")},
        )
    ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="event"))
    for key in ("door_repeat", "announce_closed", "water_filter", "deodorizer"):
        fields[vol.Required(key, default=values[key])] = bool
    for key, maximum in (("door_delay", 3600), ("door_repeat_minutes", 1440)):
        fields[vol.Required(key, default=values[key])] = vol.All(
            vol.Coerce(int), vol.Range(min=1, max=maximum)
        )
    fields.update(voice_fields(values))
    return vol.Schema(
        {k: v for k, v in fields.items() if str(k) not in ("voice_error", "voice_repeat")}
    )


def validate(hass, values, exclude_id=None):
    data = DEFAULTS | dict(values) | {"process_type": "fridge"}
    data.setdefault("fridge_notification", "")
    data.setdefault("voice_area", "")
    errors = {}
    for key, domain in (("fridge_door", "binary_sensor"), ("fridge_notification", "event")):
        entity = data.get(key)
        if (
            not entity
            and key == "fridge_notification"
            and not (data["water_filter"] or data["deodorizer"])
        ):
            continue
        if (
            not isinstance(entity, str)
            or not entity.startswith(domain + ".")
            or hass.states.get(entity) is None
        ):
            errors[key] = "missing_entity"
    for entry in hass.config_entries.async_entries("home_control"):
        other = dict(entry.data) | dict(entry.options)
        if (
            entry.entry_id != exclude_id
            and other.get("process_type") == "fridge"
            and other.get("fridge_door") == data.get("fridge_door")
        ):
            errors["base"] = "fridge_in_use"
    return data, errors


class FridgeFlowMixin:
    async def async_step_fridge(self, user_input=None):
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
            step_id="fridge",
            data_schema=schema(current if user_input is None else user_input, name=not options),
            errors=errors,
        )
