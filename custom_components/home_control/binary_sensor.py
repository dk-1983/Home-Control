"""Event-driven aggregate leak state for each valve exercise group."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import HomeControlEntity
from .laundry import LaundryRuntime
from .valve import ValveRuntime


async def async_setup_entry(hass, entry, async_add_entities):
    if isinstance(entry.runtime_data, LaundryRuntime):
        async_add_entities([LaundryWaitingSensor(entry.runtime_data)])
    if isinstance(entry.runtime_data, ValveRuntime):
        async_add_entities([GroupLeakSensor(entry.runtime_data)])


class LaundryWaitingSensor(HomeControlEntity, BinarySensorEntity):
    _attr_translation_key = "laundry_waiting"
    _attr_icon = "mdi:washing-machine-alert"

    def __init__(self, runtime):
        super().__init__(runtime, "laundry_waiting")

    @property
    def is_on(self):
        return self.runtime.pending


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
