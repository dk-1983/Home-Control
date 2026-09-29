"""One apartment voice center, with room routing and bounded speaker queues."""

import asyncio
import heapq
import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_TIMEOUT
from .doorbell_config import slots
from .speaker_queue import speaker_lane
from .voice_events import Notice, voice_bus
from .voice_external import ExternalVoiceRegistry

_LOGGER = logging.getLogger(__name__)
PRIORITY = {"ERROR": 0, "WARNING": 1, "INFO": 2}
TTL = {"ERROR": 120, "WARNING": 60, "INFO": 30}


class VoiceRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.config = dict(entry.data) | dict(entry.options)
        self.controller = self
        self.listeners = set()
        self.enabled = False
        self._stopped = False
        self._toggle_lock = asyncio.Lock()
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.bus = voice_bus(hass)
        self.external = ExternalVoiceRegistry(self)
        self._tasks = {}
        self._queues = {}
        self._serial = 0
        self._latest = {}
        self._dedup = {}
        self._repeated = {}
        self._motion = {}
        self._unsubs = []
        self.last_result = None
        self.last_error = None
        self.last_source = None
        self.speaker_results = {}
        self._night_slots = [(slots(r), r) for r in self.config.get("night_intervals", [])]

    @callback
    def _changed(self):
        for listener in tuple(self.listeners):
            listener()

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not enabled:
            self._queues.clear()
            self._latest.clear()
            self._dedup.clear()
            # In-flight audio has already been handed to the station. Keep its
            # lane reservation; cancel only our pending service calls / waits.
            for task in self._tasks.values():
                task.cancel()
        self._changed()

    async def async_set_enabled(self, enabled):
        async with self._toggle_lock:
            if self._stopped or (self.enabled == enabled and not self.last_error):
                return
            self.set_enabled(False)
            await self.async_wait_idle()
            try:
                await self.store.async_save({"enabled": enabled})
            except Exception:
                self.last_error = "storage_failed"
                self._changed()
                raise
            self.last_error = None
            self.set_enabled(enabled)
            if enabled:
                for notice in self.bus.active.values():
                    self.submit(notice)

    async def async_start(self):
        if self.bus.center is not None:
            raise HomeAssistantError("Only one voice center is supported")
        stored = await self.store.async_load()
        self.bus.center = self
        self.set_enabled(isinstance(stored, dict) and stored.get("enabled") is True)
        motion = {e for r in self.config["rooms"] for e in r.get("motion", [])}
        if motion:
            self._unsubs.append(
                async_track_state_change_event(self.hass, motion, self._motion_event)
            )
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=30)))
        self.hass.services.async_register(
            DOMAIN,
            "announce",
            self._service,
            schema=vol.Schema(
                {
                    vol.Required("source"): str,
                    vol.Required("key"): vol.All(str, vol.Length(min=1, max=80)),
                    vol.Optional("level", default="INFO"): vol.In(PRIORITY),
                    vol.Optional("message", default=""): vol.All(str, vol.Length(max=500)),
                    vol.Optional("active", default=False): bool,
                    vol.Optional("resolved", default=False): bool,
                }
            ),
        )
        self.external.start()
        for notice in self.bus.active.values():
            self.submit(notice)

    async def _service(self, call):
        data = call.data
        if data["source"] not in self.bus.sources:
            raise HomeAssistantError("Source must be a loaded Home Control process entry ID")
        # Prefix service keys to keep external notifications separate from built-ins.
        key = "custom:" + data["key"]
        if data["resolved"]:
            self.bus.resolve(data["source"], key)
            return
        if not data["message"].strip():
            raise HomeAssistantError("Message must not be empty")
        self.bus.publish(
            Notice(data["source"], data["level"], key, data["message"].strip(), data["active"])
        )

    @callback
    def _motion_event(self, event):
        state = event.data.get("new_state")
        if state and state.state == "on":
            self._motion[state.entity_id] = self.hass.loop.time()

    def _available(self, entity):
        state = self.hass.states.get(entity)
        # Temporary TTS volume is a local YandexStation feature. Do not fall
        # back to cloud speech at an uncontrolled volume when local mode drops.
        return (
            state is not None
            and state.state not in ("unavailable", "unknown")
            and "alice_state" in state.attributes
        )

    def _occupied(self, room):
        for entity in room.get("presence", []) + room.get("motion", []):
            state = self.hass.states.get(entity)
            if state and state.state == "on":
                return True
        window = self.config.get("motion_minutes", 5) * 60
        return any(
            (state := self.hass.states.get(e)) is not None
            and state.state == "off"
            and e in self._motion
            and self.hass.loop.time() - self._motion[e] < window
            for e in room.get("motion", [])
        )

    def route(self, notice):
        rooms = {r["area"]: r for r in self.config["rooms"]}
        allowed = {e for r in rooms.values() for e in r["speakers"] if self._available(e)}
        volume = self.config.get(notice.level.lower() + "_volume", 0.3)
        now = dt_util.now()
        slot = now.weekday() * 1440 + now.hour * 60 + now.minute
        for selected, rule in self._night_slots:
            if slot in selected:
                if rule["mode"] == "silent":
                    return [], volume
                volume = rule["volume"]
                if rule.get("speakers"):
                    allowed.intersection_update(rule["speakers"])
                break
        if notice.level != "INFO":
            return sorted(allowed), volume
        source = self.bus.sources.get(notice.source)
        area = source.area() if source else None
        room = rooms.get(area, {})
        local = [e for e in room.get("speakers", []) if e in allowed]
        if local:
            return list(dict.fromkeys(local)), volume
        for alternate in room.get("alternatives", []):
            other = rooms.get(alternate, {})
            speakers = [e for e in other.get("speakers", []) if e in allowed]
            if speakers and self._occupied(other):
                return list(dict.fromkeys(speakers)), volume
        fallback = rooms.get(self.config.get("fallback_area"), {})
        return list(dict.fromkeys(e for e in fallback.get("speakers", []) if e in allowed)), volume

    def _valid(self, notice):
        return (
            self.enabled
            and not self._stopped
            and (notice.source == self.entry.entry_id or self.bus.allowed(notice))
            and (not notice.active or self.bus.active.get((notice.source, notice.key)) is notice)
        )

    @callback
    def submit(self, notice, repeat=False):
        if not self._valid(notice):
            return
        now = self.hass.loop.time()
        identity = (notice.source, notice.key)
        fingerprint = (notice.source, notice.key, notice.level, notice.message)
        if not repeat and now - self._dedup.get(fingerprint, -1000) < 10:
            return
        self._dedup = {k: v for k, v in self._dedup.items() if now - v < 10}
        self._dedup[fingerprint] = now
        self._repeated[identity] = now
        self._serial += 1
        self._latest[identity] = self._serial
        speakers, _ = self.route(notice)
        self.last_source = notice.source
        self.last_result = "queued" if speakers else "silent_or_no_route"
        for entity in speakers:
            queue = self._queues.setdefault(entity, [])
            item = (PRIORITY[notice.level], self._serial, now + TTL[notice.level], notice, repeat)
            heapq.heappush(queue, item)
            if len(queue) > 32:
                queue.remove(max(queue, key=lambda i: (i[0], -i[1])))
                heapq.heapify(queue)
            if entity not in self._tasks:
                task = self.hass.async_create_background_task(
                    self._worker(entity), f"Voice {entity}"
                )
                self._tasks[entity] = task
                task.add_done_callback(lambda done, e=entity: self._finished(e, done))
        self._changed()

    def invalidate(self, source, key=None):
        for queue in self._queues.values():
            queue[:] = [
                item
                for item in queue
                if item[3].source != source or (key is not None and item[3].key != key)
            ]
            heapq.heapify(queue)
        for identity in list(self._latest):
            if identity[0] == source and (key is None or identity[1] == key):
                del self._latest[identity]
        self._dedup = {
            k: v
            for k, v in self._dedup.items()
            if k[0] != source or (key is not None and k[1] != key)
        }

    def invalidate_levels(self, source, levels):
        keys = {
            item[3].key
            for queue in self._queues.values()
            for item in queue
            if item[3].source == source and item[3].level in levels
        }
        for key in keys:
            self.invalidate(source, key)
        self._dedup = {k: v for k, v in self._dedup.items() if k[0] != source or k[2] not in levels}

    def cancel_repeats(self, source):
        for queue in self._queues.values():
            queue[:] = [item for item in queue if not (item[3].source == source and item[4])]
            heapq.heapify(queue)

    def _finished(self, entity, task):
        if self._tasks.get(entity) is task:
            del self._tasks[entity]
        if self._queues.get(entity) and self.enabled and not self._stopped:
            new = self.hass.async_create_background_task(self._worker(entity), f"Voice {entity}")
            self._tasks[entity] = new
            new.add_done_callback(lambda done, e=entity: self._finished(e, done))

    async def _worker(self, entity):
        while self.enabled and self._queues.get(entity):
            queue = self._queues[entity]
            # Acquire the shared lane BEFORE taking the next item, so a queued
            # ERROR overtakes INFO while the station is occupied by a doorbell.
            try:
                async with speaker_lane(self.hass, entity).claim(120) as lane:
                    if not self.enabled or not queue:
                        continue
                    _, serial, expires, notice, repeat = heapq.heappop(queue)
                    identity = (notice.source, notice.key)
                    if (
                        not self._valid(notice)
                        or (repeat and not self.bus.allowed(notice, repeat=True))
                        or self._latest.get(identity) != serial
                        or self.hass.loop.time() > expires
                    ):
                        continue
                    speakers, volume = self.route(notice)
                    if entity not in speakers:
                        continue
                    # Local TTS accepts the whole phrase. Cloud delivery is
                    # excluded above because it cannot honor temporary volume.
                    message = self.bus.message(notice)
                    seconds = max(self.config.get("speech_gap", 10), len(message) / 8)
                    lane.reserve(seconds)
                    async with asyncio.timeout(SERVICE_TIMEOUT):
                        await self.hass.services.async_call(
                            "media_player",
                            "play_media",
                            {
                                "entity_id": entity,
                                "media_content_id": message,
                                "media_content_type": "text",
                                "extra": {"volume_level": volume},
                            },
                            blocking=True,
                        )
                    await self._wait_speech(entity, seconds)
                    self.last_result = "submitted"
            except TimeoutError:
                self.last_result = "speaker_timeout"
                self._queues.pop(entity, None)
            except Exception:
                self.last_result = "speaker_failed"
                _LOGGER.warning("Voice delivery failed for %s", entity, exc_info=True)
            self.speaker_results[entity] = self.last_result
            self._changed()

    async def _wait_speech(self, entity, seconds):
        await asyncio.sleep(seconds)
        # Read HA's cached public state, never poll the device. A stuck SPEAKING
        # state gets a bounded wait and the next delivery rechecks availability.
        for _ in range(60):
            state = self.hass.states.get(entity)
            if state is None or state.attributes.get("alice_state") not in ("SPEAKING", "NONE"):
                return
            speaker_lane(self.hass, entity).reserve(1)
            await asyncio.sleep(1)
        raise TimeoutError("Station did not finish speaking")

    @callback
    def _tick(self, now):
        self.external.expire()
        active = self.bus.active
        self._repeated = {k: v for k, v in self._repeated.items() if k in active}
        if not self.enabled:
            return
        for identity, notice in list(active.items()):
            if (
                notice.level == "ERROR"
                and self.bus.allowed(notice, repeat=True)
                and self.hass.loop.time() - self._repeated.get(identity, 0)
                >= self.config.get("repeat_minutes", 10) * 60
            ):
                self.submit(notice, repeat=True)

    async def async_press(self):
        self.submit(
            Notice(self.entry.entry_id, "WARNING", "test", "Проверка центра голосовых оповещений.")
        )

    async def async_wait_idle(self):
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    async def async_stop(self):
        self._stopped = True
        self.set_enabled(False)
        self.external.stop()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.bus.center is self:
            self.bus.center = None
            self.hass.services.async_remove(DOMAIN, "announce")
        await self.async_wait_idle()

    def _states(self):
        return [
            state.state if (state := self.hass.states.get(entity)) else None
            for entity in sorted({e for r in self.config["rooms"] for e in r["speakers"]})
        ]

    @property
    def attributes(self):
        return {
            "process_type": "global_voice_center",
            "last_result": self.last_result,
            "last_error": self.last_error,
            "last_source": self.last_source,
            "speaker_results": dict(self.speaker_results),
            "queued": sum(len(q) for q in self._queues.values()),
            "active_issues": len(self.bus.active),
            "external_sources": self.external.summaries(),
        }
