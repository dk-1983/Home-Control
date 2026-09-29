"""Shared speaker admission for Home Control speech and doorbell audio."""

import asyncio
from contextlib import asynccontextmanager


class SpeakerLane:
    def __init__(self, hass, entity):
        self.hass = hass
        self.entity = entity
        self.lock = asyncio.Lock()
        self.until = 0

    def reserve(self, seconds):
        self.until = max(self.until, self.hass.loop.time() + seconds)

    @asynccontextmanager
    async def claim(self, timeout):
        # Timeout limits waiting only; cancellation does not erase a reservation
        # for audio already handed to the speaker.
        acquired = False
        try:
            async with asyncio.timeout(timeout):
                await self.lock.acquire()
                acquired = True
                await asyncio.sleep(max(0, self.until - self.hass.loop.time()))
                while (
                    state := self.hass.states.get(self.entity)
                ) is not None and state.attributes.get("alice_state") in ("SPEAKING", "NONE"):
                    await asyncio.sleep(0.25)
            yield self
        finally:
            if acquired:
                self.lock.release()


def speaker_lane(hass, entity):
    lanes = hass.data.setdefault("home_control_speaker_lanes", {})
    if entity not in lanes:
        lanes[entity] = SpeakerLane(hass, entity)
    return lanes[entity]
