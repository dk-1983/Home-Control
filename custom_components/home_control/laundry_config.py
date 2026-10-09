"""Configuration for a washer observer; never controls the appliance."""

import voluptuous as vol
from homeassistant.helpers import selector

from .voice_preferences import voice_fields

DEFAULTS = {
    "reminder_minutes": 30,
    "reminders": True,
    "voice_scope": "all",
    "manual_acknowledge": False,
}
FIELDS = {"washer_status": "sensor", "washer_notification": "event", "washer_error": "event"}


def schema(values, name=False):
    values = DEFAULTS | values
    fields = {vol.Required("name", default=values.get("name", "Laundry")): str} if name else {}
    for key, domain in FIELDS.items():
        marker = vol.Required if key != "washer_error" else vol.Optional
        fields[marker(key, description={"suggested_value": values.get(key)})] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))
        )
    fields[vol.Required("reminders", default=values["reminders"])] = bool
    fields[vol.Required("manual_acknowledge", default=values["manual_acknowledge"])] = bool
    fields[vol.Required("reminder_minutes", default=values["reminder_minutes"])] = vol.All(
        vol.Coerce(int), vol.Range(min=1, max=1440)
    )
    fields.update(voice_fields(values))
    # Errors are single new events, not continuously active alarms.
    fields = {k: v for k, v in fields.items() if str(k) not in ("voice_repeat", "voice_warning")}
    return vol.Schema(fields)


def validate(hass, values, exclude_id=None):
    data = DEFAULTS | dict(values) | {"process_type": "laundry"}
    data.setdefault("voice_area", "")
    data.setdefault("washer_error", "")
    errors = {}
    for key, domain in FIELDS.items():
        entity = data.get(key, "")
        if not entity and key == "washer_error":
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
            and other.get("process_type") == "laundry"
            and (
                other.get("washer_status") == data.get("washer_status")
                or other.get("washer_notification") == data.get("washer_notification")
            )
        ):
            errors["base"] = "laundry_in_use"
    return data, errors


class LaundryFlowMixin:
    async def async_step_laundry(self, user_input=None):
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
            step_id="laundry",
            data_schema=schema(current if user_input is None else user_input, name=not options),
            errors=errors,
        )
