"""Diagnostics without exposing configured entity IDs or MQTT topic."""

from homeassistant.components.diagnostics import async_redact_data


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    return {
        "config": async_redact_data(
            runtime.config,
            {
                "group_1",
                "group_2",
                "group_3",
                "group_4",
                "mqtt_topic",
                "input_button",
                "night_light",
                "output",
                "motion_sensors",
                "lights",
                "boost_fans",
                "room_light",
                "humidity_sensor",
            },
        ),
        "enabled": runtime.controller.enabled,
        "controller": runtime.attributes,
        "reported_states": runtime._states(),
    }
