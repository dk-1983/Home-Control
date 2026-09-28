"""Event-driven aggregate leak state for each valve exercise group."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import HomeControlEntity
from .valve import ValveRuntime


async def async_setup_entry(hass, entry, async_add_entities):
    if isinstance(entry.runtime_data, ValveRuntime):
        async_add_entities([GroupLeakSensor(entry.runtime_data)])


class GroupLeakSensor(HomeControlEntity, BinarySensorEntity):
    _attr_translation_key = "group_leak"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(self, runtime):
        super().__init__(runtime, "group_leak")

    @property
    def is_on(self):
        value = self.runtime.protection
        return None if value == "unknown" else value == "on"

    @property
    def extra_state_attributes(self):
        return {"process_type": "local_valve_protection"}
