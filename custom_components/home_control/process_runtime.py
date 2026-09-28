"""HA adapter for event-driven environmental controllers with a persisted gate."""

import asyncio
import logging

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_TIMEOUT
from .environment import MotionLighting, SharedVentilation
from .humidity import HumidityVentilation
from .process_config import DEFAULTS, FIELDS

_LOGGER = logging.getLogger(__name__)
LOGIC = {"motion": MotionLighting, "shared_fan": SharedVentilation, "humidity": HumidityVentilation}


class ProcessRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.config = DEFAULTS | dict(entry.data) | dict(entry.options)
        self.kind = self.config["process_type"]
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.controller = self
        self.listeners = set()
        self.enabled = False
        self.last_error = None
        self.logic = LOGIC[self.kind](self.config)
        self._lock = asyncio.Lock()
        self._toggle_lock = asyncio.Lock()
        self._generation = 0
        self._stopped = False
        self._timer = None
        self._tasks = set()
        self._unsubscribe = None
        self._expected = None
        self._feedback_due = None
        self._failed = False
        self._last_attributes = None
        self._ready_at = 0
        self._utc_offset = dt_util.utcnow().timestamp() - hass.loop.time()

    def _state(self, entity_id):
        state = self.hass.states.get(entity_id)
        return state.state if state else None

    def _states(self):
        return [self._state(self.config["output"])]

    @callback
    def _changed(self):
        attrs = self.attributes
        if attrs != self._last_attributes:
            self._last_attributes = attrs
            for listener in tuple(self.listeners):
                listener()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._generation += 1
        self.logic = LOGIC[self.kind](self.config)
        self._expected = self._feedback_due = None
        self._failed = False
        self.last_error = None
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if enabled and not self._stopped:
            self._schedule()
        self._changed()

    def _schedule(self):
        if self.enabled and not self._stopped and self._timer is None:
            self._timer = self.hass.loop.call_later(1, self._tick)

    @callback
    def _tick(self):
        self._timer = None
        self.submit()
        self._schedule()

    @callback
    def submit(self):
        if not self.enabled or self._stopped:
            return
        # Coalesce notifications; the worker always reads current HA states.
        if self._tasks:
            return
        task = self.hass.async_create_task(self.async_evaluate(self._generation))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @callback
    def _event(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is not None and new is not None and old.state == new.state:
            return
        self.submit()

    async def async_evaluate(self, generation=None):
        generation = self._generation if generation is None else generation
        async with self._lock:
            if not self.enabled or self._stopped or generation != self._generation:
                return
            now = self.hass.loop.time()
            if now < self._ready_at:
                return
            output = self._state(self.config["output"])
            if self.kind == "motion":
                desired = self.logic.evaluate(
                    now, [self._state(e) for e in self.config["motion_sensors"]], output
                )
            elif self.kind == "shared_fan":
                desired = self.logic.evaluate(
                    now,
                    [self._state(e) for e in self.config["lights"]],
                    [self._state(e) for e in self.config["boost_fans"]],
                    output,
                )
            else:
                sensor = self.hass.states.get(self.config["humidity_sensor"])
                age = (
                    (dt_util.utcnow() - sensor.last_reported).total_seconds()
                    if sensor
                    else float("inf")
                )
                desired = self.logic.evaluate(
                    now,
                    self._state(self.config["room_light"]),
                    output,
                    sensor.state if sensor else None,
                    fresh=0 <= age <= self.config["sensor_max_age"],
                    reported_at=sensor.last_reported.timestamp() - self._utc_offset
                    if sensor
                    else None,
                )
            if self._expected is not None:
                if output == self._expected:
                    self._expected = self._feedback_due = None
                elif now >= self._feedback_due:
                    self.last_error = "feedback_timeout"
                    self._expected = self._feedback_due = None
                    self._failed = True
            if output not in ("on", "off"):
                self.last_error = "unavailable_output"
                self._changed()
                return
            if self.last_error == "unavailable_output" and not self._failed:
                self.last_error = None
            target = "on" if desired else "off"
            if desired is None or output == target or self._expected == target or self._failed:
                self._changed()
                return
            # Opposite commands wait for an in-flight expectation to settle.
            if self._expected is not None:
                self._changed()
                return
            try:
                async with asyncio.timeout(SERVICE_TIMEOUT):
                    await self.hass.services.async_call(
                        self.config["output"].split(".")[0],
                        f"turn_{target}",
                        {"entity_id": self.config["output"]},
                        blocking=True,
                    )
            except asyncio.CancelledError:
                self.last_error = "command_cancelled"
                self._failed = True
                self._changed()
                raise
            except Exception:
                _LOGGER.exception("Home Control environmental command failed")
                self.last_error = "command_failed"
                self._failed = True
            else:
                if generation == self._generation and self.enabled:
                    self._expected = target
                    self._feedback_due = self.hass.loop.time() + self.config["feedback_timeout"]
                    self.last_error = None
            self._changed()

    async def async_wait_idle(self):
        async with self._lock:
            pass

    async def async_start(self):
        stored = await self.store.async_load()
        inputs = set()
        for key, (_, multiple) in FIELDS[self.kind].items():
            inputs.update(self.config[key] if multiple else [self.config[key]])
        self._unsubscribe = async_track_state_change_event(self.hass, list(inputs), self._event)
        # A short startup grace does not assume states have become valid.
        self._ready_at = self.hass.loop.time() + 5
        self.set_enabled(isinstance(stored, dict) and stored.get("enabled") is True)

    async def async_set_enabled(self, enabled):
        async with self._toggle_lock:
            if self._stopped or (self.enabled == enabled and self.last_error != "storage_failed"):
                return
            self.set_enabled(False)
            await self.async_wait_idle()
            try:
                await self.store.async_save({"enabled": enabled})
            except Exception:
                self.last_error = "storage_failed"
                self._changed()
                raise
            if not self._stopped:
                self.set_enabled(enabled)
                if enabled:
                    self.submit()

    async def async_stop(self):
        self._stopped = True
        self.set_enabled(False)
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        await self.async_wait_idle()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    @property
    def attributes(self):
        return {
            "process_type": f"local_{self.kind}",
            "enabled": self.enabled,
            **self.logic.attributes,
            "expected_state": self._expected,
            "last_error": self.last_error,
            "commands_blocked": self._failed,
        }
