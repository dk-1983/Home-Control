"""Doorbell schedules and setup forms."""

from copy import deepcopy
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.components import mqtt
from homeassistant.helpers import selector

DEFAULT_NIGHTS = [
    {
        "start": "23:00:00",
        "end": "09:00:00",
        "days": list(range(7)),
        "mode": "silent",
        "speakers": [],
        "volume": 0.2,
    }
]


def minutes(value):
    parts = str(value).split(":")
    if len(parts) not in (2, 3) or (len(parts) == 3 and int(parts[2]) != 0):
        raise ValueError("Use whole minutes")
    h, m = int(parts[0]), int(parts[1])
    if not 0 <= h < 24 or not 0 <= m < 60:
        raise ValueError("Invalid time")
    return h * 60 + m


def slots(rule):
    start, end = minutes(rule["start"]), minutes(rule["end"])
    duration = (end - start) % 1440
    if not duration or not rule["days"]:
        raise ValueError("Empty interval")
    if any(type(d) is not int or not 0 <= d <= 6 for d in rule["days"]):
        raise ValueError("Invalid weekday")
    return {(d * 1440 + start + offset) % 10080 for d in rule["days"] for offset in range(duration)}


def policy(config, now):
    current = now.weekday() * 1440 + now.hour * 60 + now.minute
    for rule in config.get("night_intervals", []):
        if current in slots(rule):
            return ([], None) if rule["mode"] == "silent" else (rule["speakers"], rule["volume"])
    return config["speakers"], config["volume"]


def entities(domain, multiple=False):
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, multiple=multiple))


def schema(values, name=False):
    fields = {vol.Required("name", default=values.get("name", "Doorbell")): str} if name else {}
    for key, default, validator in (
        ("speakers", [], entities("media_player", True)),
        ("media_url", "", str),
        ("volume", 0.4, vol.All(vol.Coerce(float), vol.Range(min=0, max=1))),
        ("sound_duration", 10, vol.All(vol.Coerce(float), vol.Range(min=1, max=120))),
        ("cooldown", 10, vol.All(vol.Coerce(float), vol.Range(min=1, max=300))),
        (
            "source",
            "virtual",
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["virtual", "binary_sensor", "input_button", "mqtt"],
                    translation_key="doorbell_source",
                )
            ),
        ),
        (
            "active_state",
            "on",
            selector.SelectSelector(selector.SelectSelectorConfig(options=["on", "off"])),
        ),
        ("mqtt_topic", "", str),
        ("mqtt_payload", "", str),
        ("mqtt_key", "", str),
    ):
        marker = vol.Optional if key.startswith("mqtt_") else vol.Required
        fields[marker(key, default=values.get(key, default))] = validator
    fields[
        vol.Optional("source_entity", description={"suggested_value": values.get("source_entity")})
    ] = entities(["binary_sensor", "input_button"])
    return vol.Schema(fields)


def night_schema(values):
    fields = {}
    for key, default, validator in (
        ("start", "23:00:00", selector.TimeSelector()),
        ("end", "09:00:00", selector.TimeSelector()),
        (
            "days",
            [str(i) for i in range(7)],
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[str(i) for i in range(7)],
                    multiple=True,
                    translation_key="doorbell_days",
                )
            ),
        ),
        (
            "mode",
            "silent",
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["silent", "sound"], translation_key="doorbell_night_mode"
                )
            ),
        ),
        ("volume", 0.2, vol.All(vol.Coerce(float), vol.Range(min=0, max=1))),
        ("speakers", [], entities("media_player", True)),
    ):
        marker = vol.Optional if key == "speakers" else vol.Required
        fields[marker(key, default=values.get(key, default))] = validator
    return vol.Schema(fields)


def validate(hass, values):
    data = deepcopy(values)
    data["process_type"] = "doorbell"
    errors = {}
    url = urlparse(data.get("media_url", ""))
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or not url.path.lower().endswith(".mp3")
    ):
        errors["media_url"] = "doorbell_url"
    if not data.get("speakers"):
        errors["speakers"] = "missing_entity"
    for entity in data.get("speakers", []):
        if not entity.startswith("media_player.") or hass.states.get(entity) is None:
            errors["speakers"] = "missing_entity"
    source = data.get("source", "virtual")
    if source in ("binary_sensor", "input_button"):
        entity = data.get("source_entity", "")
        if not entity.startswith(source + ".") or hass.states.get(entity) is None:
            errors["source_entity"] = "missing_entity"
    elif source == "mqtt":
        try:
            mqtt.valid_publish_topic(data.get("mqtt_topic", ""))
        except vol.Invalid:
            errors["mqtt_topic"] = "invalid_topic"
        if not data.get("mqtt_payload"):
            errors["mqtt_payload"] = "doorbell_payload"
    elif source != "virtual":
        errors["source"] = "invalid_parameters"
    return data, errors


class DoorbellFlowMixin:
    async def async_step_doorbell(self, user_input=None):
        options = hasattr(self, "config_entry")
        current = (
            (dict(self.config_entry.data) | dict(self.config_entry.options)) if options else {}
        )
        errors = {}
        if user_input is not None:
            data, errors = validate(self.hass, user_input)
            if not options and not data.get("name", "").strip():
                errors["name"] = "invalid_name"
            if not errors:
                self._doorbell = data
                self._doorbell["night_intervals"] = deepcopy(
                    current.get("night_intervals", DEFAULT_NIGHTS)
                )
                return await self.async_step_doorbell_schedule()
        return self.async_show_form(
            step_id="doorbell",
            data_schema=schema(user_input or current, name=not options),
            errors=errors,
        )

    async def async_step_doorbell_schedule(self, user_input=None):
        rules = self._doorbell["night_intervals"]
        summary = (
            "\n".join(
                f"{i + 1}. {r['start']}–{r['end']} ({r['mode']})" for i, r in enumerate(rules)
            )
            or "—"
        )
        return self.async_show_menu(
            step_id="doorbell_schedule",
            menu_options=["doorbell_night", "doorbell_remove", "doorbell_finish"],
            description_placeholders={"intervals": summary},
        )

    async def async_step_doorbell_night(self, user_input=None):
        errors = {}
        if user_input is not None:
            rule = deepcopy(user_input)
            try:
                rule["days"] = [int(d) for d in rule["days"]]
                selected = slots(rule)
                if any(selected & slots(r) for r in self._doorbell["night_intervals"]):
                    raise ValueError("Overlap")
                if rule["mode"] == "sound" and (
                    not rule["speakers"]
                    or any(self.hass.states.get(e) is None for e in rule["speakers"])
                ):
                    raise ValueError("Missing speakers")
            except ValueError, KeyError, TypeError:
                errors["base"] = "doorbell_interval"
            if not errors:
                self._doorbell["night_intervals"].append(rule)
                return await self.async_step_doorbell_schedule()
        return self.async_show_form(
            step_id="doorbell_night", data_schema=night_schema(user_input or {}), errors=errors
        )

    async def async_step_doorbell_remove(self, user_input=None):
        rules = self._doorbell["night_intervals"]
        if user_input is not None:
            rules.pop(int(user_input["interval"]))
            return await self.async_step_doorbell_schedule()
        if not rules:
            return await self.async_step_doorbell_schedule()
        choices = [
            {"value": str(i), "label": f"{i + 1}: {r['start']}–{r['end']} ({r['mode']})"}
            for i, r in enumerate(rules)
        ]
        return self.async_show_form(
            step_id="doorbell_remove",
            data_schema=vol.Schema(
                {
                    vol.Required("interval"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices)
                    )
                }
            ),
        )

    async def async_step_doorbell_finish(self, user_input=None):
        data = deepcopy(self._doorbell)
        return self.async_create_entry(title=data.pop("name", "").strip(), data=data)
