"""Constants for Home Control."""

DOMAIN = "home_control"
GROUP_KEYS = ("group_1", "group_2", "group_3", "group_4")
DEFAULT_WINDOW = 3.0
DEFAULT_FEEDBACK_TIMEOUT = 5.0
SERVICE_TIMEOUT = 10.0


def selected_groups(values):
    """Return configured groups in field order, skipping empty slots."""
    return tuple(values[key] for key in GROUP_KEYS if values.get(key))
