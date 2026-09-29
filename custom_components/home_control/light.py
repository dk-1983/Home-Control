"""One stepped-brightness light for each chandelier process."""

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity

from .entity import HomeControlEntity
from .hood import HoodRuntime
from .runtime import HomeControlRuntime


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = entry.runtime_data
    if isinstance(runtime, HoodRuntime) and runtime.config.get("hood_light"):
        async_add_entities([HoodLight(runtime)])
    if isinstance(runtime, HomeControlRuntime) and runtime.has_group_light:
        async_add_entities([GroupLight(runtime)])


class GroupLight(HomeControlEntity, LightEntity):
    _attr_name = None
    _attr_icon = "mdi:ceiling-light"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, runtime):
        super().__init__(runtime, "light")

    @property
    def available(self):
        return self.runtime.controller.enabled and all(
            s in ("on", "off") for s in self.runtime._states()
        )

    @property
    def is_on(self):
        states = self.runtime._states()
        return None if any(s not in ("on", "off") for s in states) else "on" in states

    @property
    def brightness(self):
        states = self.runtime._states()
        if any(s not in ("on", "off") for s in states):
            return None
        return round(255 * states.count("on") / len(states))

    async def async_turn_on(self, **kwargs):
        await self.runtime.async_light(True, kwargs.get(ATTR_BRIGHTNESS))

    async def async_turn_off(self, **kwargs):
        await self.runtime.async_light(False)


class HoodLight(HomeControlEntity, LightEntity):
    _attr_translation_key = "hood_light"
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_icon = "mdi:lightbulb"

    def __init__(self, runtime):
        super().__init__(runtime, "hood_light")

    @property
    def available(self):
        return self.runtime.enabled and self.runtime.light_state() in ("on", "off")

    @property
    def is_on(self):
        value = self.runtime.light_state()
        return None if value not in ("on", "off") else value == "on"

    @property
    def extra_state_attributes(self):
        return {"process_type": "local_hood_light"}

    async def async_turn_on(self, **kwargs):
        await self.runtime.async_light(True)

    async def async_turn_off(self, **kwargs):
        await self.runtime.async_light(False)
