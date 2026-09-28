"""Diagnostics without exposing configured entity IDs or MQTT topic."""

from homeassistant.components.diagnostics import async_redact_data


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    return {
        "config": async_redact_data(
            runtime.config,
            {
                "speed_25",
                "speed_50",
                "speed_75",
                "speed_100",
                "input_25",
                "input_50",
                "input_75",
                "input_100",
                "group_1",
                "group_2",
                "group_3",
                "group_4",
                "mqtt_topic",
                "media_url",
                "speakers",
                "source_entity",
                "mqtt_payload",
                "input_button",
                "night_light",
                "output",
                "motion_sensors",
                "lights",
                "boost_fans",
                "room_light",
                "humidity_sensor",
                "valves",
                "leak_sensors",
            },
        ),
        "enabled": runtime.controller.enabled,
        "controller": async_redact_data(runtime.attributes, {"active_valve", "valves"}),
        "reported_states": runtime._states(),
    }
