"""Version 1 of the public voice-source service schemas.

Keep wire compatibility with docs/voice-protocol-v1.md. Changes requiring a
producer to change its payload belong to a new protocol version.
"""

import voluptuous as vol

VERSION = 1
LEASE_SECONDS = 180
REGISTER = "register_voice_source"
PUBLISH = "publish_voice_event"
UNREGISTER = "unregister_voice_source"
READY = "home_control_voice_ready"
DISCOVER = "home_control_voice_discover"
SERVICES = (REGISTER, PUBLISH, UNREGISTER)
PREFERENCES = ("voice_info", "voice_warning", "voice_error", "voice_repeat")
SOURCE_FIELDS = ("domain", "config_entry_id", "process_id")


def strict_integer(value):
    if type(value) is not int:
        raise vol.Invalid("Expected an integer, not a boolean or coerced value")
    return value


def nonempty(limit):
    def validate(value):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise vol.Invalid(f"Expected a nonempty string of at most {limit} characters")
        return value

    return validate


SOURCE_SCHEMA = vol.Schema({vol.Required(key): nonempty(128) for key in SOURCE_FIELDS})
ISSUE_SCHEMA = vol.Schema(
    {
        vol.Required("key"): nonempty(80),
        vol.Required("level"): vol.In(("WARNING", "ERROR")),
        vol.Required("message"): nonempty(500),
    }
)


def snapshot(value):
    if not isinstance(value, list) or len(value) > 64:
        raise vol.Invalid("At most 64 active issues are allowed")
    result = [ISSUE_SCHEMA(issue) for issue in value]
    if len({issue["key"] for issue in result}) != len(result):
        raise vol.Invalid("Issue keys must be unique")
    return result


def register_rules(data):
    if data["maintenance"] and data["active_issues"]:
        raise vol.Invalid("Maintenance requires an empty active snapshot")
    return data


def event_rules(data):
    event = data["event"]
    if event["active"] and (event["resolved"] or event["level"] == "INFO"):
        raise vol.Invalid("Only unresolved WARNING/ERROR can be active")
    if not event["resolved"] and not event["message"].strip():
        raise vol.Invalid("An unresolved event needs a message")
    return data


COMMON = {
    vol.Required("protocol_version"): vol.All(strict_integer, vol.Equal(VERSION)),
    vol.Required("source"): SOURCE_SCHEMA,
    vol.Required("producer_session"): nonempty(128),
    vol.Required("revision"): vol.All(strict_integer, vol.Range(min=1)),
}
REGISTER_SCHEMA = vol.All(
    vol.Schema(
        COMMON
        | {
            vol.Required("name"): nonempty(200),
            vol.Required("area_id"): vol.Any(None, vol.All(str, vol.Length(max=128))),
            vol.Required("preferences"): vol.Schema(
                {vol.Required(key): bool for key in PREFERENCES}
            ),
            vol.Required("maintenance"): bool,
            vol.Required("active_issues"): snapshot,
        }
    ),
    register_rules,
)
PUBLISH_SCHEMA = vol.All(
    vol.Schema(
        COMMON
        | {
            vol.Required("center_session"): nonempty(128),
            vol.Required("event"): vol.Schema(
                {
                    vol.Required("key"): nonempty(80),
                    vol.Required("level"): vol.In(("INFO", "WARNING", "ERROR")),
                    vol.Required("message"): vol.All(str, vol.Length(max=500)),
                    vol.Required("active"): bool,
                    vol.Required("resolved"): bool,
                }
            ),
        }
    ),
    event_rules,
)
UNREGISTER_SCHEMA = vol.Schema(COMMON | {vol.Required("center_session"): nonempty(128)})
SCHEMAS = {REGISTER: REGISTER_SCHEMA, PUBLISH: PUBLISH_SCHEMA, UNREGISTER: UNREGISTER_SCHEMA}
