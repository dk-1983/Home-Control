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
                "hood_light",
                "output",
                "motion_sensors",
                "lights",
                "boost_fans",
                "room_light",
                "humidity_sensor",
                "washer_status",
                "washer_notification",
                "washer_error",
                "fridge_door",
                "fridge_notification",
                "valves",
                "leak_sensors",
                "rooms",
                "voice_area",
                "fallback_area",
            },
        ),
        "enabled": runtime.controller.enabled,
        "controller": async_redact_data(
            runtime.attributes,
            {"active_valve", "valves", "last_source", "speaker_results", "external_sources"},
        ),
        "reported_states": runtime._states(),
    }
