"""Per-process voice preferences, independent of hardware."""

import voluptuous as vol
from homeassistant.helpers import selector


def voice_fields(values):
    fields = {
        vol.Optional(
            "voice_area", description={"suggested_value": values.get("voice_area")}
        ): selector.AreaSelector()
    }
    for key in ("voice_info", "voice_warning", "voice_error", "voice_repeat"):
        fields[vol.Required(key, default=values.get(key, False))] = bool
    return fields
