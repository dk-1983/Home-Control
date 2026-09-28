"""Modbus Devices coordinator boundary simulated using the real HA refresh lock."""

import asyncio
import logging

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator


class FakeCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, owner):
        super().__init__(hass, logging.getLogger(__name__), name="hood test", config_entry=None)
        self.owner = owner
        self.device = FakeDevice()
        self._write_generation = 0
        self._pending_write_patches = {}
        self.entered = asyncio.Event()
        self.read_count = 0
        self.data = self.snapshot()

    def snapshot(self):
        return {"outputs": {i + 1: {"state": value} for i, value in enumerate(self.owner.hardware)}}

    async def _async_update_data(self):
        self.entered.set()
        while not self.owner.polling:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0)
        self.read_count += 1
        self._pending_write_patches.clear()
        return self.snapshot()


class FakeDevice:
    pass


class FakeRelay:
    def __init__(self, coordinator, channel):
        self.coordinator = coordinator
        self._output_number = channel
        self.enabled = True


FakeDevice.__module__ = "custom_components.modbus_devices.equipment.bolid"
FakeDevice.__name__ = "M3000BB1020"
FakeRelay.__module__ = "custom_components.modbus_devices.switch"
FakeRelay.__name__ = "ModBusSwitchEntity"
