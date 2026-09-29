"""Home Control: local automation processes for Home Assistant."""

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .doorbell import DoorbellRuntime
from .hood import HoodRuntime
from .process_config import PROCESS_TYPES
from .process_runtime import ProcessRuntime
from .runtime import HomeControlRuntime
from .valve import ValveRuntime
from .voice_events import ProcessVoice
from .voice_runtime import VoiceRuntime

PLATFORMS = [Platform.SWITCH, Platform.BUTTON, Platform.FAN, Platform.LIGHT, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    environmental = (dict(entry.data) | dict(entry.options)).get("process_type") in PROCESS_TYPES
    hood = (dict(entry.data) | dict(entry.options)).get("process_type") == "hood"
    config = dict(entry.data) | dict(entry.options)
    doorbell = config.get("process_type") == "doorbell"
    valve = config.get("process_type") == "valve_exercise"
    voice = config.get("process_type") == "voice_center"
    needs_mqtt = (
        config.get("source") == "mqtt"
        if doorbell
        else not environmental and not hood and not valve and not voice
    )
    if needs_mqtt and not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("Configure MQTT before Home Control")
    runtime = (
        VoiceRuntime(hass, entry)
        if voice
        else ValveRuntime(hass, entry)
        if valve
        else DoorbellRuntime(hass, entry)
        if doorbell
        else HoodRuntime(hass, entry)
        if hood
        else ProcessRuntime(hass, entry)
        if environmental
        else HomeControlRuntime(hass, entry)
    )
    entry.runtime_data = runtime
    try:
        await runtime.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if not voice:
            runtime.voice_observer = ProcessVoice(runtime)
            runtime.voice_observer.start()
    except Exception:
        if observer := getattr(runtime, "voice_observer", None):
            observer.stop()
        await runtime.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = entry.runtime_data
    # Close admission before unloading entities. Preserve the persisted mode.
    enabled = runtime.controller.enabled
    runtime.controller.set_enabled(False)
    await runtime.controller.async_wait_idle()
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if observer := getattr(runtime, "voice_observer", None):
            observer.stop()
        await runtime.async_stop()
        return True
    runtime.controller.set_enabled(enabled)
    return False


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
