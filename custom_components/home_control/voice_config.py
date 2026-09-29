"""Shared notification preferences and voice-center UI."""

from copy import deepcopy

import voluptuous as vol
from homeassistant.helpers import selector

from .doorbell_config import entities, night_schema, slots


def center_schema(values, name=False):
    fields = {vol.Required("name", default=values.get("name", "Voice center")): str} if name else {}
    for key, default, limits in (
        ("info_volume", 0.3, (0, 1)),
        ("warning_volume", 0.4, (0, 1)),
        ("error_volume", 0.5, (0, 1)),
        ("repeat_minutes", 10, (1, 1440)),
        ("speech_gap", 10, (1, 120)),
        ("motion_minutes", 5, (0, 60)),
    ):
        fields[vol.Required(key, default=values.get(key, default))] = vol.All(
            vol.Coerce(float), vol.Range(min=limits[0], max=limits[1])
        )
    fields[
        vol.Optional("fallback_area", description={"suggested_value": values.get("fallback_area")})
    ] = selector.AreaSelector()
    return vol.Schema(fields)


def room_schema(values):
    return vol.Schema(
        {
            vol.Required(
                "area", **({"default": values["area"]} if values.get("area") else {})
            ): selector.AreaSelector(),
            vol.Optional("speakers", default=values.get("speakers", [])): entities(
                "media_player", True
            ),
            vol.Optional("presence", default=values.get("presence", [])): entities(
                "binary_sensor", True
            ),
            vol.Optional("motion", default=values.get("motion", [])): entities(
                "binary_sensor", True
            ),
            vol.Optional(
                "alternatives", default=values.get("alternatives", [])
            ): selector.AreaSelector(selector.AreaSelectorConfig(multiple=True)),
        }
    )


class VoiceFlowMixin:
    async def async_step_voice_center(self, user_input=None):
        options = hasattr(self, "config_entry")
        if not options and any(
            e.data.get("process_type") == "voice_center"
            for e in self.hass.config_entries.async_entries("home_control")
        ):
            return self.async_abort(reason="voice_center_exists")
        current = dict(self.config_entry.data) | dict(self.config_entry.options) if options else {}
        errors = {}
        if user_input is not None:
            if not options and not user_input.get("name", "").strip():
                errors["name"] = "invalid_name"
            else:
                self._voice = deepcopy(user_input) | {
                    "process_type": "voice_center",
                    "fallback_area": user_input.get("fallback_area", ""),
                    "rooms": deepcopy(current.get("rooms", [])),
                    "night_intervals": deepcopy(
                        current.get(
                            "night_intervals",
                            [
                                {
                                    "start": "23:00:00",
                                    "end": "09:00:00",
                                    "days": list(range(7)),
                                    "mode": "silent",
                                    "speakers": [],
                                    "volume": 0.2,
                                }
                            ],
                        )
                    ),
                }
                return await self.async_step_voice_menu()
        return self.async_show_form(
            step_id="voice_center",
            data_schema=center_schema(user_input or current, name=not options),
            errors=errors,
        )

    async def async_step_voice_menu(self, user_input=None):
        from homeassistant.helpers import area_registry

        registry = area_registry.async_get(self.hass)
        rooms = [
            (
                registry.async_get_area(r["area"]).name
                if registry.async_get_area(r["area"])
                else r["area"]
            )
            + f" ({len(r['speakers'])})"
            for r in self._voice["rooms"]
        ]
        intervals = [
            f"{r['start']}–{r['end']} ({r['mode']})" for r in self._voice["night_intervals"]
        ]
        return self.async_show_menu(
            step_id="voice_menu",
            menu_options=[
                "voice_room",
                "voice_remove_room",
                "voice_night",
                "voice_remove_night",
                "voice_finish",
            ],
            description_placeholders={
                "rooms": ", ".join(rooms) or "—",
                "intervals": ", ".join(intervals) or "—",
            },
        )

    async def async_step_voice_room(self, user_input=None):
        errors = {}
        if user_input is not None:
            room = dict(user_input)
            for key in ("speakers", "presence", "motion", "alternatives"):
                room.setdefault(key, [])
            if any(
                self.hass.states.get(e) is None
                for key in ("speakers", "presence", "motion")
                for e in room[key]
            ):
                errors["base"] = "missing_entity"
            else:
                self._voice["rooms"] = [
                    r for r in self._voice["rooms"] if r["area"] != room["area"]
                ] + [room]
                return await self.async_step_voice_menu()
        return self.async_show_form(
            step_id="voice_room", data_schema=room_schema(user_input or {}), errors=errors
        )

    async def async_step_voice_remove_room(self, user_input=None):
        return await self._voice_remove("rooms", "voice_remove_room", user_input)

    async def async_step_voice_remove_night(self, user_input=None):
        return await self._voice_remove("night_intervals", "voice_remove_night", user_input)

    async def _voice_remove(self, key, step, user_input):
        items = self._voice[key]
        if user_input is not None:
            items.pop(int(user_input["item"]))
            return await self.async_step_voice_menu()
        if not items:
            return await self.async_step_voice_menu()
        choices = [
            {"value": str(i), "label": r.get("area") or f"{r['start']}–{r['end']}"}
            for i, r in enumerate(items)
        ]
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required("item"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices)
                    )
                }
            ),
        )

    async def async_step_voice_night(self, user_input=None):
        errors = {}
        if user_input is not None:
            rule = deepcopy(user_input)
            rule.setdefault("speakers", [])
            try:
                rule["days"] = [int(d) for d in rule["days"]]
                selected = slots(rule)
                if any(selected & slots(r) for r in self._voice["night_intervals"]):
                    errors["base"] = "voice_overlap"
                elif any(self.hass.states.get(e) is None for e in rule["speakers"]):
                    errors["base"] = "missing_entity"
            except ValueError, KeyError, TypeError:
                errors["base"] = "voice_interval"
            if not errors:
                self._voice["night_intervals"].append(rule)
                return await self.async_step_voice_menu()
        return self.async_show_form(
            step_id="voice_night", data_schema=night_schema(user_input or {}), errors=errors
        )

    async def async_step_voice_finish(self, user_input=None):
        if not hasattr(self, "config_entry") and any(
            e.data.get("process_type") == "voice_center"
            for e in self.hass.config_entries.async_entries("home_control")
        ):
            return self.async_abort(reason="voice_center_exists")
        if not any(r["speakers"] for r in self._voice["rooms"]):
            return self.async_show_form(
                step_id="voice_room",
                data_schema=room_schema({}),
                errors={"base": "voice_speakers_required"},
            )
        areas = {r["area"] for r in self._voice["rooms"]}
        if (
            self._voice.get("fallback_area")
            and self._voice["fallback_area"] not in areas
            or any(a not in areas for r in self._voice["rooms"] for a in r["alternatives"])
        ):
            return self.async_show_form(
                step_id="voice_room",
                data_schema=room_schema({}),
                errors={"base": "voice_unknown_room"},
            )
        data = deepcopy(self._voice)
        return self.async_create_entry(title=data.pop("name", "").strip(), data=data)
