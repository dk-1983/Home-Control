"""Independent multi-speaker doorbell with an event admission gate."""

import asyncio
import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_TIMEOUT
from .doorbell_config import policy
from .speaker_queue import speaker_lane

_LOGGER = logging.getLogger(__name__)


class DoorbellRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.config = dict(entry.data) | dict(entry.options)
        self.controller = self
        self.listeners = set()
        self.enabled = False
        self._stopped = False
        self._generation = 0
        self._until = 0
        self._tasks = set()
        self._unsubscribers = []
        self._toggle_lock = asyncio.Lock()
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.last_result = None
        self.failed_speakers = 0

    @callback
    def _changed(self):
        for listener in tuple(self.listeners):
            listener()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._generation += 1
        self._changed()

    async def async_set_enabled(self, enabled):
        async with self._toggle_lock:
            if self._stopped or (self.enabled == enabled and self.last_result != "storage_failed"):
                return
            self.set_enabled(False)
            try:
                await self.store.async_save({"enabled": enabled})
            except Exception:
                self.last_result = "storage_failed"
                self._changed()
                raise
            if not self._stopped:
                self.set_enabled(enabled)

    async def async_wait_idle(self):
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def async_start(self):
        stored = await self.store.async_load()
        self.set_enabled(isinstance(stored, dict) and stored.get("enabled") is True)
        source = self.config["source"]
        if source == "mqtt":
            self._unsubscribers.append(
                await mqtt.async_subscribe(self.hass, self.config["mqtt_topic"], self._mqtt, qos=0)
            )
        elif source in ("binary_sensor", "input_button"):
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass, [self.config["source_entity"]], self._event
                )
            )

    @callback
    def _event(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None or old.state == new.state:
            return
        if self.config["source"] == "binary_sensor":
            if old.state not in ("on", "off") or new.state != self.config["active_state"]:
                return
        elif new.state in ("unknown", "unavailable") or old.state == "unavailable":
            return
        self.submit()

    @callback
    def _mqtt(self, message):
        if message.retain:
            return
        value = message.payload
        key = self.config.get("mqtt_key")
        if key:
            try:
                value = json.loads(value)
                for part in key.split("."):
                    value = value[part]
            except ValueError, TypeError, KeyError:
                return
        if str(value) == self.config["mqtt_payload"]:
            self.submit()

    @callback
    def submit(self):
        now = self.hass.loop.time()
        if not self.enabled or self._stopped or self._tasks or now < self._until:
            return None
        self._until = now + self.config["cooldown"]
        speakers, volume = policy(self.config, dt_util.now())
        if not speakers:
            self.last_result = "quiet_hours"
            self._changed()
            return None
        generation = self._generation
        self.failed_speakers = 0
        task = self.hass.async_create_task(
            self._ring(list(dict.fromkeys(speakers)), volume, generation)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def async_press(self):
        if task := self.submit():
            await task

    def _allowed(self, generation):
        return self.enabled and not self._stopped and generation == self._generation

    async def _call(self, service, entity, **data):
        async with asyncio.timeout(SERVICE_TIMEOUT):
            await self.hass.services.async_call(
                "media_player", service, {"entity_id": entity, **data}, blocking=True
            )

    async def _speaker(self, entity, volume, generation):
        try:
            async with speaker_lane(self.hass, entity).claim(15):
                speakers, volume = policy(self.config, dt_util.now())
                if entity not in speakers:
                    return True
                return await self._play_speaker(entity, volume, generation)
        except TimeoutError:
            return False

    async def _play_speaker(self, entity, volume, generation):
        state = self.hass.states.get(entity)
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        previous = state.attributes.get("volume_level")
        if not isinstance(previous, (int, float)) or not 0 <= previous <= 1:
            return False
        changed = False
        try:
            if not self._allowed(generation):
                return True
            # Do not change volume if the old value cannot be restored.
            if isinstance(previous, (int, float)) and 0 <= previous <= 1 and previous != volume:
                await self._call("volume_set", entity, volume_level=volume)
                changed = True
            if not self._allowed(generation):
                return True
            speaker_lane(self.hass, entity).reserve(self.config["sound_duration"])
            await self._call(
                "play_media",
                entity,
                media_content_id=self.config["media_url"],
                media_content_type="stream.mp3",
            )
            await asyncio.sleep(self.config["sound_duration"])
            return True
        except Exception:
            _LOGGER.warning("Doorbell speaker command failed for %s", entity, exc_info=True)
            return False
        finally:
            # Maintenance prevents any later equipment commands, including restore.
            current = self.hass.states.get(entity)
            if (
                changed
                and self._allowed(generation)
                and current is not None
                and current.state not in ("unknown", "unavailable")
            ):
                actual = current.attributes.get("volume_level")
                if isinstance(actual, (int, float)) and abs(actual - volume) < 0.001:
                    try:
                        await self._call("volume_set", entity, volume_level=previous)
                    except Exception:
                        _LOGGER.warning("Doorbell volume restore failed for %s", entity)

    async def _ring(self, speakers, volume, generation):
        results = await asyncio.gather(*(self._speaker(e, volume, generation) for e in speakers))
        self.failed_speakers = results.count(False)
        self.last_result = "speaker_failed" if self.failed_speakers else "sent"
        self._changed()

    async def async_stop(self):
        self._stopped = True
        self.set_enabled(False)
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for task in tuple(self._tasks):
            task.cancel()
        await self.async_wait_idle()

    def _states(self):
        return [
            s.state if (s := self.hass.states.get(e)) else None for e in self.config["speakers"]
        ]

    @property
    def attributes(self):
        return {
            "process_type": "local_doorbell",
            "last_result": self.last_result,
            "failed_speakers": self.failed_speakers,
        }
