"""Pure motion-lighting and shared-extraction state machines."""


def any_on(states):
    """Three-valued OR: missing inputs never prove absence."""
    if "on" in states:
        return True
    if states and all(value == "off" for value in states):
        return False
    return None


class MotionLighting:
    def __init__(self, config):
        self.delay = config["light_off_delay"]
        self.since = None
        self.phase = "IDLE"

    def evaluate(self, now, inputs, output):
        occupied = any_on(inputs)
        if occupied is True:
            self.since = None
            self.phase = "OCCUPIED"
            return True
        if occupied is None or output != "on":
            self.since = None
            self.phase = "UNKNOWN" if occupied is None else "IDLE"
            return None
        if self.since is None:
            self.since = now
        self.phase = "WAITING_OFF"
        return False if now - self.since >= self.delay else None

    @property
    def attributes(self):
        return {"phase": self.phase}


class SharedVentilation:
    def __init__(self, config):
        self.on_delay = config["fan_on_delay"]
        self.off_delay = config["fan_off_delay"]
        self.on_since = None
        self.off_since = None
        self.phase = "IDLE"

    def evaluate(self, now, lights, fans, output):
        used = any_on(lights)
        boost = any_on(fans)
        if used is True:
            if self.on_since is None:
                self.on_since = now
        else:
            self.on_since = None
        if boost is True:
            self.off_since = None
            self.phase = "BOOST"
            return True
        if used is True:
            self.off_since = None
            self.phase = "RUNNING" if output == "on" else "WAITING_ON"
            return True if now - self.on_since >= self.on_delay else None
        if used is None or boost is None:
            self.off_since = None
            self.phase = "UNKNOWN"
            return None
        if output != "on":
            self.off_since = None
            self.phase = "IDLE"
            return None
        if self.off_since is None:
            self.off_since = now
        self.phase = "WAITING_OFF"
        return False if now - self.off_since >= self.off_delay else None

    @property
    def attributes(self):
        return {"phase": self.phase}
