"""Persisted laundry reminder driven only by explicit HA washer reports."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .laundry_config import DEFAULTS, FIELDS
from .voice_events import Notice, voice_bus

_LOGGER = logging.getLogger(__name__)
RUNNING = {
    "running",
    "detecting",
    "rinsing",
    "spinning",
    "drying",
    "steam_softening",
    "cool_down",
    "refreshing",
}
ERRORS = {
    "water_supply_error": "Ошибка подачи воды.",
    "out_of_balance_error": "Дисбаланс белья.",
    "unable_to_lock_error": "Не удалось заблокировать дверцу.",
    "door_open_error": "Машина сообщает об открытой дверце.",
    "water_drain_error": "Ошибка слива воды.",
    "water_level_sensor_error": "Ошибка датчика уровня воды.",
    "overfill_error": "Машина сообщает о переполнении водой.",
    "locked_motor_error": "Ошибка вращения двигателя.",
    "power_fail_error": "Машина сообщает о сбое питания.",
    "temperature_sensor_error": "Ошибка датчика температуры.",
}


class LaundryRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.config = DEFAULTS | dict(entry.data) | dict(entry.options)
        self.controller = self
        self.listeners = set()
        self.enabled = False
        self._stopped = False
        self._generation = 0
        self._lock = asyncio.Lock()
        self._tasks = set()
        self._unsubs = []
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.bus = voice_bus(hass)
        self.pending = False
        self.armed = False
        self.latched = False
        self.ready_for_cycle = False
        self.completed_at = None
        self.next_reminder = None
        self.announced = False
        self.seen = {}
        self.last_error = None
        self._accept_after = dt_util.utcnow()

    def _acknowledge(self):
        self.pending = False
        self.next_reminder = None
        if self.bus.center:
            self.bus.center.invalidate(self.entry.entry_id, "laundry_waiting")

    def _auto_acknowledge(self):
        state = self.hass.states.get(self.config["washer_status"])
        if (
            self.pending
            and not self.config["manual_acknowledge"]
            and state
            and not state.attributes.get("restored")
            and state.state in {"power_off", "unknown", "unavailable"}
        ):
            self._acknowledge()
            return True
        return False

    @property
    def preferences(self):
        return self.config

    def area(self):
        return self.config.get("voice_area")

    @callback
    def _changed(self):
        for listener in tuple(self.listeners):
            listener()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._generation += 1
        self._accept_after = dt_util.utcnow()
        if not enabled and self.bus.center:
            self.bus.center.invalidate(self.entry.entry_id)
        self._changed()

    async def _save(self):
        try:
            await self.store.async_save(
                {
                    "enabled": self.enabled,
                    "pending": self.pending,
                    "armed": self.armed,
                    "latched": self.latched,
                    "ready_for_cycle": self.ready_for_cycle,
                    "completed_at": self.completed_at,
                    "next_reminder": self.next_reminder,
                    "announced": self.announced,
                    "seen": dict(self.seen),
                }
            )
        except Exception:
            self.last_error = "storage_failed"
            self.set_enabled(False)
            raise
        self.last_error = None
        self._changed()

    async def async_start(self):
        saved = await self.store.async_load()
        if isinstance(saved, dict):
            for key in (
                "pending",
                "armed",
                "latched",
                "ready_for_cycle",
                "completed_at",
                "next_reminder",
                "announced",
                "seen",
            ):
                if key in saved:
                    setattr(self, key, saved[key])
        self.bus.sources[self.entry.entry_id] = self
        status = self.hass.states.get(self.config["washer_status"])
        if status and not status.attributes.get("restored"):
            if status.state in {"power_off", "initial", "reserved"}:
                self.ready_for_cycle = True
            elif status.state in RUNNING and not self.latched:
                self.armed = True
        self.set_enabled(isinstance(saved, dict) and saved.get("enabled") is True)
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.config[k] for k in FIELDS if self.config.get(k)], self._event
            )
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=30)))

    def _spawn(self, event=None):
        if self.enabled and not self._stopped:
            task = self.hass.async_create_task(self._run(self._generation, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @callback
    def _event(self, event):
        self._spawn(event)

    @callback
    def _tick(self, now):
        self._spawn()

    def _fresh_event(self, state):
        stamp = dt_util.parse_datetime(state.state)
        if stamp is None or stamp.tzinfo is None or stamp <= self._accept_after:
            return False
        previous = self.seen.get(state.entity_id)
        if previous and stamp <= dt_util.parse_datetime(previous):
            return False
        if stamp > dt_util.utcnow() + timedelta(seconds=5):
            return False
        self.seen[state.entity_id] = stamp.isoformat()
        return True

    def _complete(self):
        if self.latched:
            return
        self.pending = self.latched = True
        self.ready_for_cycle = False
        self.armed = self.announced = False
        self.completed_at = dt_util.utcnow().isoformat()
        self.next_reminder = None

    async def _run(self, generation, event=None):
        async with self._lock:
            if not self.enabled or self._stopped or generation != self._generation:
                return
            try:
                error_message = None
                if event is not None:
                    state, old = event.data.get("new_state"), event.data.get("old_state")
                    if (
                        state is None
                        or state.attributes.get("restored")
                        or (old and old.state == state.state)
                    ):
                        return
                    if state.entity_id == self.config["washer_status"]:
                        if state.state in {"power_off", "initial", "reserved"}:
                            self.ready_for_cycle = True
                        if state.state in RUNNING:
                            # Resuming from an unavailable report is not a new cycle.
                            if (
                                not self.latched
                                or self.ready_for_cycle
                                or (
                                    old and old.state in {"power_off", "initial", "reserved", "end"}
                                )
                            ):
                                self.armed = True
                                self.latched = False
                                self.ready_for_cycle = False
                                self.pending = False
                                self.next_reminder = None
                                if self.bus.center:
                                    self.bus.center.invalidate(
                                        self.entry.entry_id, "laundry_waiting"
                                    )
                        elif state.state == "end" and self.armed:
                            self._complete()
                    elif self._fresh_event(state):
                        kind = state.attributes.get("event_type")
                        if state.entity_id == self.config["washer_notification"]:
                            if kind == "washing_is_complete":
                                self._complete()
                            elif kind == "error_during_washing" and not self.config.get(
                                "washer_error"
                            ):
                                error_message = "Ошибка во время стирки."
                        elif kind in ERRORS:
                            error_message = ERRORS[kind]
                    self._auto_acknowledge()
                    await self._save()
                elif self._auto_acknowledge():
                    await self._save()
                if generation == self._generation and self.enabled:
                    if error_message:
                        self._error(error_message)
                    await self._remind()
            except Exception:
                _LOGGER.exception("Laundry observer failed for %s", self.entry.entry_id)

    def _error(self, text):
        self.bus.publish(
            Notice(self.entry.entry_id, "ERROR", "washer_error", f"{self.entry.title}. {text}")
        )

    async def _remind(self):
        if not self.pending or (self.announced and not self.config["reminders"]):
            return
        now = dt_util.utcnow()
        due = dt_util.parse_datetime(self.next_reminder) if self.next_reminder else None
        if due and now < due:
            return
        notice = Notice(
            self.entry.entry_id,
            "INFO",
            "laundry_waiting",
            f"{self.entry.title}. Стирка завершена. Заберите бельё из стиральной машины.",
        )
        center = self.bus.center
        if (
            not center
            or not center.enabled
            or not self.bus.allowed(notice)
            or not center.route(notice)[0]
        ):
            # Keep one due reminder through silence, offline speakers or center reload.
            return
        self.announced = True
        self.next_reminder = (now + timedelta(minutes=self.config["reminder_minutes"])).isoformat()
        generation = self._generation
        await self._save()
        if generation == self._generation and self.enabled:
            self.bus.publish(notice)

    async def async_press(self):
        async with self._lock:
            if not self.enabled or self._stopped:
                return
            self._acknowledge()
            await self._save()

    async def async_set_enabled(self, enabled):
        # Close admission immediately, including workers waiting on persistence.
        self.set_enabled(False)
        async with self._lock:
            if self._stopped:
                return
            self.set_enabled(enabled)
            await self._save()
        if enabled:
            self._spawn()

    async def async_wait_idle(self):
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def async_stop(self):
        self._stopped = True
        self.set_enabled(False)
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.async_wait_idle()
        self.bus.remove(self.entry.entry_id)

    def _states(self):
        return [
            s.state if (s := self.hass.states.get(self.config[k])) else None
            for k in FIELDS
            if self.config.get(k)
        ]

    @property
    def attributes(self):
        return {
            "process_type": "local_laundry",
            "laundry_waiting": self.pending,
            "cycle_observed": self.armed,
            "completed_at": self.completed_at,
            "next_reminder": self.next_reminder,
            "last_error": self.last_error,
        }
