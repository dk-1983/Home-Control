"""A persisted automation enable switch for each local process."""

from homeassistant.components.switch import SwitchEntity

from .entity import HomeControlEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([AutomationSwitch(entry.runtime_data)])


class AutomationSwitch(HomeControlEntity, SwitchEntity):
    _attr_translation_key = "automation"
    _attr_icon = "mdi:robot"

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "automation")

    @property
    def is_on(self) -> bool:
        return self.runtime.controller.enabled

    @property
    def extra_state_attributes(self):
        return self.runtime.attributes

    async def async_turn_on(self, **kwargs) -> None:
        await self.runtime.async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.runtime.async_set_enabled(False)
