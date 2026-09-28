"""Independent humidity ventilation state machine; no light or shared-fan commands."""

import math
from collections import deque


def trend(points, config):
    """Least-squares slope in RH percentage points per actual minute."""
    if len(points) < config["minimum_samples"]:
        return "WARMUP", 0.0
    xs = [(at - points[0][0]) / 60 for at, _ in points]
    ys = [value for _, value in points]
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    divisor = sum((x - xm) ** 2 for x in xs)
    slope = (
        sum((x - xm) * (y - ym) for x, y in zip(xs, ys, strict=True)) / divisor if divisor else 0.0
    )
    if slope >= config["rising_slope"]:
        return "RISING", slope
    if slope <= config["falling_slope"]:
        return "FALLING", slope
    if abs(slope) <= config["stable_slope"] and max(ys) - min(ys) <= config["stable_range"]:
        return "STABLE", slope
    return "FLUCTUATING", slope


class HumidityVentilation:
    def __init__(self, config):
        self.config = config
        self.history = deque(maxlen=config["window_size"])
        self.phase = "IDLE"
        self.trend = "WARMUP"
        self.slope = 0.0
        self.peak = None
        self.stable = 0
        self.falling_seen = False
        self.last_sample = None
        self.last_report = None
        self.fan_since = None
        self.previous_fan = None
        self.stop_reason = None
        self.inhibited = False
        self.sensor_valid = False
        self.stable_since = None
        self.uncertain_since = None

    def _reset_episode(self):
        self.phase = "IDLE"
        self.peak = None
        self.stable = 0
        self.falling_seen = False
        self.stable_since = None

    def _stop(self, reason):
        self._reset_episode()
        self.stop_reason = reason
        self.inhibited = True
        return False

    def evaluate(self, now, light, fan, humidity, *, fresh=True, reported_at=None):
        if fan == "on":
            if self.previous_fan == "off" and self.inhibited:
                # A later manual ON is a new run and may be adopted normally.
                self._reset_episode()
                self.inhibited = False
                self.stop_reason = None
            if self.fan_since is None:
                self.fan_since = now
            if self.phase == "STARTING":
                self.phase = "VENTILATING"
            if self.phase == "IDLE" and not self.inhibited:
                self.phase = "ADOPTED"
        elif fan == "off":
            self.fan_since = None
            if self.previous_fan == "on":
                # Respect external/manual shutdown; do not immediately fight it.
                self._reset_episode()
                self.inhibited = True
                self.stop_reason = self.stop_reason or "external_off"
        if fan in ("on", "off"):
            self.previous_fan = fan
        try:
            value = float(humidity)
            self.sensor_valid = fresh and math.isfinite(value) and 0 <= value <= 100
        except TypeError, ValueError:
            self.sensor_valid = False
        if not self.sensor_valid:
            self.history.clear()
            self.trend = "UNKNOWN"
            self.slope = 0.0
            self.stable = 0
            self.stable_since = None
        sample_due = (
            self.last_sample is None or now - self.last_sample >= self.config["sample_interval"]
        )
        if sample_due:
            self.last_sample = now
            if self.sensor_valid and (reported_at is None or reported_at != self.last_report):
                if (
                    reported_at is not None
                    and self.last_report is not None
                    and reported_at < self.last_report
                ):
                    self.history.clear()
                self.last_report = reported_at
                self.history.append((now if reported_at is None else reported_at, value))
                self.trend, self.slope = trend(list(self.history), self.config)
            else:
                sample_due = False
        # Useful rising/falling trends take priority over the fallback timer.
        # After renewed useful drying, a brief transition cannot trigger an old timeout.
        uncertain = self.trend not in ("RISING", "FALLING")
        if fan == "on" and uncertain:
            if self.uncertain_since is None:
                self.uncertain_since = now
        else:
            self.uncertain_since = None
        if (
            fan == "on"
            and self.uncertain_since is not None
            and now - self.uncertain_since >= self.config["uncertain_timeout_minutes"] * 60
            and self.trend != "STABLE"
        ):
            return self._stop("uncertain_trend_timeout")
        if self.inhibited:
            # A stable/falling observation after confirmed OFF rearms future episodes.
            if (
                fan == "off"
                and sample_due
                and self.sensor_valid
                and self.trend in ("STABLE", "FALLING")
            ):
                self.inhibited = False
                self.history.clear()
            return None
        if not self.sensor_valid or not sample_due or self.trend == "WARMUP":
            return None
        if self.phase == "IDLE":
            if self.trend != "RISING":
                return None
            self.phase = "RISING"
            self.peak = value
        if self.peak is None or value > self.peak:
            self.peak = value
        if self.phase == "RISING":
            if self.trend in ("STABLE", "FALLING"):
                self._reset_episode()
                return None
            if light == "on" and self.trend == "RISING" and fan == "off":
                self.phase = "STARTING"
                self.stop_reason = None
                return True
            return None
        if self.phase == "STARTING":
            if fan == "on":
                self.phase = "VENTILATING"
            else:
                return None
        if fan != "on":
            return None
        if self.trend == "RISING":
            self.phase = "VENTILATING"
            self.falling_seen = False
            self.stable = 0
            self.stable_since = None
            return None
        if self.trend == "FALLING":
            self.phase = "RECOVERY"
            self.falling_seen = True
            self.stable = 0
            self.stable_since = None
            return None
        # A sustained plateau is evidence of no measurable further reduction,
        # including while the room light remains on or before a clear fall.
        self.stable = self.stable + 1 if self.trend == "STABLE" else 0
        if self.trend == "STABLE":
            if self.stable_since is None:
                self.stable_since = now
        else:
            self.stable_since = None
        if (
            self.stable >= self.config["stable_windows_required"]
            and self.stable_since is not None
            and now - self.stable_since >= self.config["plateau_duration"]
        ):
            return self._stop("humidity_plateau")
        return None

    @property
    def attributes(self):
        return {
            "phase": self.phase,
            "trend": self.trend,
            "slope": round(self.slope, 4),
            "peak": self.peak,
            "stable_windows": self.stable,
            "falling_seen": self.falling_seen,
            "sensor_valid": self.sensor_valid,
            "restart_inhibited": self.inhibited,
            "stop_reason": self.stop_reason,
        }
