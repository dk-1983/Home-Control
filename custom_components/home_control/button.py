"""Native virtual button; existing input_button helpers are also supported."""

from homeassistant.components.button import ButtonEntity

from .entity import HomeControlEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    if hasattr(entry.runtime_data, "async_press"):
        async_add_entities([ChandelierButton(entry.runtime_data)])


class ChandelierButton(HomeControlEntity, ButtonEntity):
    _attr_translation_key = "press"
    _attr_icon = "mdi:ceiling-light"

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "press")
        if runtime.config.get("process_type") == "valve_exercise":
            self._attr_translation_key = "exercise"
            self._attr_icon = "mdi:valve"
        if runtime.config.get("process_type") == "doorbell":
            self._attr_translation_key = "ring"
            self._attr_icon = "mdi:doorbell"

    @property
    def available(self) -> bool:
        return self.runtime.controller.enabled

    async def async_press(self) -> None:
        await self.runtime.async_press()
