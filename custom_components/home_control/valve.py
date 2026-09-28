"""Persisted valve exercise queue; no device or leak-sensor polling."""

import asyncio
import logging
from copy import deepcopy
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_TIMEOUT
from .valve_config import DEFAULTS, next_schedule, protection_state

_LOGGER = logging.getLogger(__name__)


class ExerciseAborted(Exception):
    """Stop commands to this valve; never perform an unconditional reopen."""


class ValveRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.config = DEFAULTS | dict(entry.data) | dict(entry.options)
        self.controller = self
        self.listeners = set()
        self.enabled = False
        self._persisted_enabled = False
        self._generation = 0
        self._stopped = False
        self._task = None
        self._timer = None
        self._unsubscribers = []
        self._store_lock = asyncio.Lock()
        self._toggle_lock = asyncio.Lock()
        self._global_lock = hass.data.setdefault(f"{DOMAIN}_valve_exercise_lock", asyncio.Lock())
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.records = {}
        self.next_due = None
        self.protection = "unknown"
        self.active_valve = None
        self.phase = "idle"
        self._abort_reason = None
        self.last_error = None
        self._schedule_signature = [
            self.config["schedule_day"],
            self.config["schedule_time"],
            str(dt_util.DEFAULT_TIME_ZONE),
        ]

    def _state(self, entity):
        state = self.hass.states.get(entity)
        return state.state if state else None

    def _states(self):
        return [self._state(e) for e in self.config["valves"]]

    @callback
    def _changed(self):
        for listener in tuple(self.listeners):
            listener()

    def _snapshot_protection(self):
        self.protection = protection_state([self._state(e) for e in self.config["leak_sensors"]])
        return self.protection

    @callback
    def _event(self, event):
        entity = event.data["entity_id"]
        new_state = event.data.get("new_state")
        reported = new_state.state if new_state else None
        if entity in self.config["leak_sensors"]:
            self._snapshot_protection()
            if self.active_valve and (self.protection != "off" or reported != "off"):
                self._abort_reason = "protection_changed"
        elif entity == self.active_valve:
            value = reported
            if value not in ("on", "off"):
                self._abort_reason = "valve_unavailable"
            elif self.phase == "holding" and value != "on":
                self._abort_reason = "unexpected_position"
        self._changed()

    async def _save(self):
        try:
            await self._save_data()
        except Exception:
            self.last_error = "storage_failed"
            self._changed()
            raise

    async def _save_data(self):
        async with self._store_lock:
            await self.store.async_save(
                deepcopy(
                    {
                        "enabled": self._persisted_enabled,
                        "next_due": self.next_due,
                        "schedule": self._schedule_signature,
                        "records": self.records,
                    }
                )
            )

        self._changed()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._generation += 1
        if not enabled and self.active_valve:
            self._abort_reason = "disabled"
        self._changed()

    async def async_set_enabled(self, enabled):
        async with self._toggle_lock:
            if self._stopped or (enabled == self.enabled and self.last_error != "storage_failed"):
                return
            self.set_enabled(False)
            self._persisted_enabled = enabled
            try:
                await self._save()
            except Exception:
                self._persisted_enabled = False
                self.last_error = "storage_failed"
                self._changed()
                raise
            self.last_error = None
            if self._stopped:
                return
            self.set_enabled(enabled)
            if enabled:
                self.submit()

    async def async_start(self):
        stored = await self.store.async_load()
        stored = stored if isinstance(stored, dict) else {}
        self._persisted_enabled = stored.get("enabled") is True
        now = dt_util.now()
        candidate = stored.get("next_due")
        self.next_due = (
            candidate
            if stored.get("schedule") == self._schedule_signature
            and isinstance(candidate, str)
            and dt_util.parse_datetime(candidate) is not None
            else next_schedule(now, self.config).isoformat()
        )
        old = stored.get("records", {})
        for entity in self.config["valves"]:
            record = old.get(entity, {}) if isinstance(old, dict) else {}
            self.records[entity] = {
                "due": None,
                "retry_at": None,
                "last_success": None,
                "status": "idle",
                "error": None,
            } | (record if isinstance(record, dict) else {})
            if self.records[entity]["status"] in ("closing", "holding", "opening"):
                self.records[entity].update(
                    status="interrupted", error="restart_during_cycle", retry_at=None
                )
        self._snapshot_protection()
        self._unsubscribers.append(
            async_track_state_change_event(
                self.hass, self.config["leak_sensors"] + self.config["valves"], self._event
            )
        )
        await self._save()
        self.set_enabled(self._persisted_enabled)
        # Startup itself never sends a valve command. Scheduler resumes after grace.
        self._timer = self.hass.loop.call_later(60, self._tick)

    @callback
    def _tick(self):
        self._timer = None
        if not self._stopped:
            self.submit()
            self._timer = self.hass.loop.call_later(60, self._tick)

    @callback
    def submit(self, manual=False):
        if (
            self._stopped
            or not self.enabled
            or self.last_error == "storage_failed"
            or (self._task and not self._task.done())
        ):
            return None
        self._task = self.hass.async_create_task(self._run(self._generation, manual))
        return self._task

    async def async_press(self):
        # Native button schedules work and returns; duplicate presses are ignored.
        self.submit(manual=True)

    def _check(self, generation):
        if self.last_error == "storage_failed":
            raise ExerciseAborted("storage_failed")
        if self._stopped or not self.enabled or generation != self._generation:
            raise ExerciseAborted("disabled")
        if self._abort_reason:
            raise ExerciseAborted(self._abort_reason)

    async def _wait(self, seconds, generation, expected=None):
        deadline = self.hass.loop.time() + seconds
        while True:
            self._check(generation)
            if expected is not None:
                value = self._state(self.active_valve)
                if value not in ("on", "off"):
                    raise ExerciseAborted("valve_unavailable")
                if value == expected:
                    return
            remaining = deadline - self.hass.loop.time()
            if remaining <= 0:
                if expected is not None:
                    raise ExerciseAborted("movement_timeout")
                return
            await asyncio.sleep(min(0.25, remaining))

    async def _command(self, close, generation):
        self._check(generation)
        async with asyncio.timeout(SERVICE_TIMEOUT):
            await self.hass.services.async_call(
                "switch",
                "turn_on" if close else "turn_off",
                {"entity_id": self.active_valve},
                blocking=True,
            )
        self._check(generation)

    async def _defer(self, record, reason):
        record.update(
            status="pending",
            error=reason,
            retry_at=(dt_util.now() + timedelta(hours=self.config["retry_hours"])).isoformat(),
        )
        await self._save()

    async def _exercise(self, entity, generation):
        record = self.records[entity]
        # Closed/unavailable valves are inspected before looking at leak sensors.
        if self._state(entity) != "off":
            await self._defer(record, "waiting_open")
            return
        if self._snapshot_protection() != "off":
            await self._defer(record, "waiting_protection")
            return
        self.active_valve = entity
        self._abort_reason = None
        try:
            record.update(status="closing", error=None, retry_at=None)
            self.phase = "closing"
            await self._save()
            self._check(generation)
            if self._state(entity) != "off":
                raise ExerciseAborted("position_changed_before_start")
            await self._command(True, generation)
            await self._wait(self.config["movement_timeout"], generation, "on")
            self.phase = "holding"
            record["status"] = "holding"
            await self._save()
            await self._wait(self.config["closed_hold"], generation)
            # Only this snapshot and the pre-start snapshot select sensor states.
            if self._snapshot_protection() != "off":
                raise ExerciseAborted("protection_changed")
            if self._state(entity) != "on":
                raise ExerciseAborted("unexpected_position")
            record["status"] = "opening"
            await self._save()
            self._check(generation)
            if self._state(entity) != "on":
                raise ExerciseAborted("unexpected_position")
            self.phase = "opening"
            await self._command(False, generation)
            await self._wait(self.config["movement_timeout"], generation, "off")
            record.update(
                status="completed",
                due=None,
                retry_at=None,
                error=None,
                last_success=dt_util.now().isoformat(),
            )
            await self._save()
        except ExerciseAborted as err:
            record.update(
                status="interrupted"
                if str(err) in ("disabled", "protection_changed")
                else "failed",
                error=str(err),
                retry_at=None,
            )
            await self._save()
        except asyncio.CancelledError:
            # Last persisted phase is recovered as interrupted on the next load.
            raise
        except Exception:
            record.update(status="failed", error="command_or_storage_failed", retry_at=None)
            _LOGGER.exception("Valve exercise failed for %s", entity)
            await self._save()
        finally:
            self.active_valve = None
            self.phase = "idle"
            self._abort_reason = None
            self._changed()

    async def _run(self, generation, manual):
        try:
            now = dt_util.now()
            scheduled = dt_util.parse_datetime(self.next_due)
            calendar_due = now >= scheduled
            if calendar_due:
                self.next_due = next_schedule(now, self.config).isoformat()
            if calendar_due or manual:
                for record in self.records.values():
                    if manual or record["status"] not in ("failed", "interrupted"):
                        record.update(
                            due=record["due"] or (scheduled if calendar_due else now).isoformat(),
                            retry_at=now.isoformat(),
                            status="pending",
                            error=None,
                        )
                await self._save()
            for entity, record in self.records.items():
                self._check(generation)
                if record["status"] != "pending" or not record["due"]:
                    continue
                retry_at = dt_util.parse_datetime(record["retry_at"]) if record["retry_at"] else now
                if dt_util.now() < retry_at:
                    continue
                async with self._global_lock:
                    self._check(generation)
                    await self._exercise(entity, generation)
                    await self._wait(self.config["between_valves"], generation)
        except ExerciseAborted:
            pass
        except Exception:
            self.last_error = "storage_failed"
            _LOGGER.exception("Valve exercise queue could not be persisted")
        finally:
            self._changed()

    async def async_wait_idle(self):
        if self._task and not self._task.done():
            await self._task

    async def async_stop(self):
        self._stopped = True
        self.set_enabled(False)
        if self._timer:
            self._timer.cancel()
            self._timer = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        await self.async_wait_idle()

    @property
    def attributes(self):
        return {
            "process_type": "local_valve_exercise",
            "phase": self.phase,
            "active_valve": self.active_valve,
            "next_scheduled": self.next_due,
            "last_error": self.last_error,
            "valves": deepcopy(self.records),
        }
