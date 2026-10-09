"""Read-only refrigerator observer with a persisted maintenance gate."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers import device_registry
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .fridge_config import DEFAULTS
from .voice_events import Notice, voice_bus

_LOGGER = logging.getLogger(__name__)
FILTER_MESSAGES = {
    "water_filter": "Пора заменить водяной фильтр холодильника.",
    "deodorizer": "Пора очистить дезодоратор холодильника. Не мойте его водой — по инструкции его нужно извлечь и просушить.",
}
FILTER_EVENTS = {
    "time_to_change_water_filter": ("water_filter", True),
    "water_filter_reset_complete": ("water_filter", False),
    "time_to_change_filter": ("deodorizer", True),
    "filter_reset_complete": ("deodorizer", False),
}


class FridgeRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.config = DEFAULTS | dict(entry.data) | dict(entry.options)
        self.controller = self
        self.listeners = set()
        self.enabled = False
        self._stopped = False
        self._generation = 0
        self._lock = asyncio.Lock()
        self._toggle_lock = asyncio.Lock()
        self._tasks = set()
        self._unsubs = []
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.bus = voice_bus(hass)
        self.last_error = None
        self.seen = None
        self.pending_filters = set()
        self._accept_after = dt_util.utcnow()
        self._door = None
        self._opened_at = None
        self._last_warning = None
        self._warned = False

    @property
    def preferences(self):
        return self.config

    def area(self):
        if area := self.config.get("voice_area"):
            return area
        device = device_registry.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.entry.entry_id)}
        )
        return device.area_id if device else None

    @callback
    def _changed(self):
        for listener in tuple(self.listeners):
            listener()

    def _invalidate(self, key=None):
        if self.bus.center:
            self.bus.center.invalidate(self.entry.entry_id, key)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._generation += 1
        self._accept_after = dt_util.utcnow()
        self._door = self._opened_at = self._last_warning = None
        self._warned = False
        if not enabled:
            self._invalidate()
        self._changed()

    async def _save(self):
        try:
            await self.store.async_save(
                {
                    "enabled": self.enabled,
                    "seen": self.seen,
                    "pending_filters": sorted(self.pending_filters),
                    "notification_entity": self.config.get("fridge_notification", ""),
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
        if isinstance(saved, dict) and saved.get("notification_entity") == self.config.get(
            "fridge_notification", ""
        ):
            self.seen = saved.get("seen")
            self.pending_filters = {
                k
                for k in saved.get("pending_filters", [])
                if k in FILTER_MESSAGES and self.config[k]
            }
        self.bus.sources[self.entry.entry_id] = self
        self.set_enabled(isinstance(saved, dict) and saved.get("enabled") is True)
        entities = [self.config["fridge_door"]]
        if self.config.get("fridge_notification"):
            entities.append(self.config["fridge_notification"])
        self._unsubs.append(async_track_state_change_event(self.hass, entities, self._event))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=1)))
        self._sync_door()

    def _publish(self, level, key, message):
        notice = Notice(self.entry.entry_id, level, key, f"{self.entry.title}. {message}")
        center = self.bus.center
        if (
            not self.enabled
            or not center
            or not center.enabled
            or not self.bus.allowed(notice)
            or not center.route(notice)[0]
        ):
            return False
        self.bus.publish(notice)
        return True

    def _sync_door(self, event=None):
        if not self.enabled or self._stopped:
            return
        state = (
            event.data.get("new_state")
            if event
            else self.hass.states.get(self.config["fridge_door"])
        )
        value = state.state if state and not state.attributes.get("restored") else None
        if value == self._door:
            return
        self._door = value
        self._invalidate("door_closed")
        if value == "on":
            self._opened_at = self.hass.loop.time()
        else:
            self._opened_at = self._last_warning = None
            self._invalidate("door_open")
            if value == "off":
                if self._warned and self.config["announce_closed"]:
                    self._publish("INFO", "door_closed", "Дверь холодильника закрыта.")
                self._warned = False
        self._changed()

    @callback
    def _event(self, event):
        if not self.enabled or self._stopped:
            return
        if event.data["entity_id"] == self.config["fridge_door"]:
            self._sync_door(event)
        else:
            self._spawn(event.data.get("new_state"))

    @callback
    def _tick(self, now):
        self._sync_door()
        if not self.enabled or self._stopped:
            return
        now = self.hass.loop.time()
        if (
            self._opened_at is not None
            and now - self._opened_at >= self.config["door_delay"]
            and (
                self._last_warning is None
                or (
                    self.config["door_repeat"]
                    and now - self._last_warning >= self.config["door_repeat_minutes"] * 60
                )
            )
        ):
            if self._publish(
                "WARNING", "door_open", "Дверь холодильника открыта. Проверьте, пожалуйста."
            ):
                self._last_warning = now
                self._warned = True
                self._changed()
        if self.pending_filters and not self._tasks:
            self._spawn()

    def _spawn(self, state=None):
        if self.enabled and not self._stopped:
            task = self.hass.async_create_task(self._filters(self._generation, state))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _filters(self, generation, state=None):
        async with self._lock:
            if not self.enabled or self._stopped or generation != self._generation:
                return
            try:
                if state is not None:
                    stamp = dt_util.parse_datetime(state.state)
                    previous = dt_util.parse_datetime(self.seen) if self.seen else None
                    if (
                        state.attributes.get("restored")
                        or stamp is None
                        or stamp.tzinfo is None
                        or stamp <= self._accept_after
                        or (previous and stamp <= previous)
                        or stamp > dt_util.utcnow() + timedelta(seconds=5)
                    ):
                        return
                    self.seen = stamp.isoformat()
                    action = FILTER_EVENTS.get(state.attributes.get("event_type"))
                    if action:
                        key, active = action
                        if active and self.config[key]:
                            self.pending_filters.add(key)
                        elif not active:
                            self.pending_filters.discard(key)
                            self._invalidate(key)
                    await self._save()
                for key in tuple(self.pending_filters):
                    if generation != self._generation or not self.enabled:
                        return
                    if self._publish("INFO", key, FILTER_MESSAGES[key]):
                        self.pending_filters.discard(key)
                        await self._save()
            except Exception:
                _LOGGER.exception("Refrigerator observer failed for %s", self.entry.entry_id)

    async def async_set_enabled(self, enabled):
        async with self._toggle_lock:
            self.set_enabled(False)
            async with self._lock:
                if self._stopped:
                    return
                # Persist before reopening admission.
                try:
                    await self.store.async_save(
                        {
                            "enabled": enabled,
                            "seen": self.seen,
                            "pending_filters": sorted(self.pending_filters),
                            "notification_entity": self.config.get("fridge_notification", ""),
                        }
                    )
                except Exception:
                    self.last_error = "storage_failed"
                    self._changed()
                    raise
                if self._stopped:
                    return
                self.last_error = None
                self.set_enabled(enabled)
                self._sync_door()

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
            state.state if (state := self.hass.states.get(self.config["fridge_door"])) else None
        ]

    @property
    def attributes(self):
        return {
            "process_type": "local_fridge",
            "door_state": self._door,
            "door_warning": self._warned,
            "pending_filters": sorted(self.pending_filters),
            "last_error": self.last_error,
        }
