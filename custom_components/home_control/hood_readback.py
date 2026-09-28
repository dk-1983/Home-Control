"""Request fresh M3000 snapshots from the existing Modbus Devices coordinator.

Home Control consumes driver data only. It does not open connections, issue
Modbus protocol requests, patch the other integration, or trust optimistic HA
states as confirmation. The adapter is limited to the reviewed M3000 layout.
"""

from dataclasses import dataclass

from homeassistant.helpers.entity_component import DATA_INSTANCES


class ReadbackError(Exception):
    """The requested controller read cannot be trusted."""


@dataclass(frozen=True)
class Binding:
    entities: tuple
    coordinator: object
    device: object
    channels: tuple[int, ...]


def resolve_binding(hass, outputs):
    """Resolve the loaded provider of the configured relay entities."""
    component = hass.data.get(DATA_INSTANCES, {}).get("switch")
    entities = tuple(component.get_entity(entity) if component else None for entity in outputs)
    if len(entities) != 4 or any(
        entity is None
        or type(entity).__module__ != "custom_components.modbus_devices.switch"
        or type(entity).__name__ != "ModBusSwitchEntity"
        or not entity.enabled
        for entity in entities
    ):
        raise ReadbackError("unsupported_hood_relays")
    coordinator = entities[0].coordinator
    device = coordinator.device
    channels = tuple(getattr(entity, "_output_number", None) for entity in entities)
    if (
        type(device).__module__ != "custom_components.modbus_devices.equipment.bolid"
        or type(device).__name__ != "M3000BB1020"
        or any(entity.coordinator is not coordinator for entity in entities)
        or len(set(channels)) != 4
        or any(type(channel) is not int or channel not in range(1, 7) for channel in channels)
        or not callable(getattr(coordinator, "async_refresh", None))
        or type(getattr(coordinator, "_write_generation", None)) is not int
        or not isinstance(getattr(coordinator, "_pending_write_patches", None), dict)
        or getattr(coordinator, "_shutdown_requested", True)
    ):
        raise ReadbackError("unsupported_hood_relays")
    return Binding(entities, coordinator, device, channels)


class HoodReadback:
    def __init__(self, hass, outputs):
        self.hass = hass
        self.outputs = outputs

    def resolve(self):
        return resolve_binding(self.hass, self.outputs)

    async def async_read(self):
        binding = self.resolve()
        coordinator = binding.coordinator
        previous = coordinator.data
        writes_before = coordinator._write_generation
        # Unlike async_request_refresh/update_entity, this awaits an actual refresh
        # under HA's coordinator lock, including a poll already in progress.
        await coordinator.async_refresh()
        if self.resolve() != binding or self.hass.is_stopping:
            raise ReadbackError("readback_binding_changed")
        if not coordinator.last_update_success or coordinator.data is previous:
            raise ReadbackError("readback_failed")
        # A concurrent completed write may have overlaid a snapshot or mutated
        # the driver's shared dictionaries. Such a result cannot authorize ON.
        if coordinator._write_generation != writes_before:
            raise ReadbackError("readback_concurrent_write")
        patches = coordinator._pending_write_patches
        outputs = coordinator.data.get("outputs", {})
        values = []
        for channel in binding.channels:
            if ("outputs", channel, "state") in patches:
                raise ReadbackError("readback_pending_write")
            output = outputs.get(channel)
            value = output.get("state") if isinstance(output, dict) else None
            if type(value) is not bool:
                raise ReadbackError("invalid_readback_data")
            # No awaits between generation checks and copying the entire vector.
            values.append(value)
        return tuple(values)
