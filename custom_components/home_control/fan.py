"""One fan entity for all hood speeds and voice commands."""

from homeassistant.components.fan import FanEntity, FanEntityFeature

from .entity import HomeControlEntity
from .hood import HoodRuntime


async def async_setup_entry(hass, entry, async_add_entities):
    if isinstance(entry.runtime_data, HoodRuntime):
        async_add_entities([HoodFan(entry.runtime_data)])


class HoodFan(HomeControlEntity, FanEntity):
    _attr_name = None
    _attr_icon = "mdi:stove"
    _attr_speed_count = 4
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )

    def __init__(self, runtime):
        super().__init__(runtime, "fan")

    @property
    def available(self):
        return self.runtime.enabled

    @property
    def percentage(self):
        return self.runtime.percentage

    @property
    def is_on(self):
        value = self.percentage
        return None if value is None else value > 0

    @property
    def extra_state_attributes(self):
        return self.runtime.attributes

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        await self.runtime.async_request(percentage)

    async def async_turn_off(self, **kwargs):
        await self.runtime.async_request(0)

    async def async_set_percentage(self, percentage):
        await self.runtime.async_request(percentage)
