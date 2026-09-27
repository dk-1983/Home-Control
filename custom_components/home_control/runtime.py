"""Home Assistant event, persistence and service adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import parse_datetime

from .const import DOMAIN, GROUP_KEYS, SERVICE_TIMEOUT
from .controller import ChandelierController, is_single_press


class HomeControlRuntime:
    """One local process per config entry, with its own maintenance gate."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.config = dict(entry.data) | dict(entry.options)
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.listeners: set[Callable[[], None]] = set()
        self._unsubscribers: list[Callable[[], None]] = []
        self._timer: asyncio.TimerHandle | None = None
        self._selection_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()
        self._toggle_lock = asyncio.Lock()
        self._stopped = False
        self.controller = ChandelierController(
            [self.config[k] for k in GROUP_KEYS],
            self._states,
            self._send,
            window=self.config["selection_window"],
            feedback_timeout=self.config["feedback_timeout"],
            clock=hass.loop.time,
            changed=self._changed,
        )

    def _states(self):
        return [
            state.state if (state := self.hass.states.get(entity_id)) else None
            for entity_id in self.controller.groups
        ]

    async def _send(self, on: bool, targets: tuple[str, ...]) -> None:
        async with asyncio.timeout(SERVICE_TIMEOUT):
            await self.hass.services.async_call(
                "switch",
                "turn_on" if on else "turn_off",
                {"entity_id": list(targets)},
                blocking=True,
            )

    @callback
    def _changed(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self.controller.feedback_due is not None and not self._stopped:
            self._timer = self.hass.loop.call_at(
                self.controller.feedback_due, self._feedback_expired
            )
        if self._selection_timer:
            self._selection_timer.cancel()
            self._selection_timer = None
        deadline = self.controller.deadline
        if deadline is not None and deadline >= self.hass.loop.time() and not self._stopped:
            self._selection_timer = self.hass.loop.call_at(deadline + 0.001, self._changed)
        for listener in tuple(self.listeners):
            listener()

    @callback
    def _feedback_expired(self) -> None:
        self._timer = None
        self.controller.check_feedback()
        # call_at can fire a fraction early on some event loops.
        if self.controller.feedback_due is not None:
            self._timer = self.hass.loop.call_later(0.01, self._feedback_expired)

    @callback
    def submit_press(self, source: str) -> None:
        if self._stopped or (press := self.controller.capture_press()) is None:
            return
        task = self.hass.async_create_task(
            self.controller.async_press(press, source),
            f"Home Control button: {self.entry.entry_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def async_press(self) -> None:
        if not self._stopped:
            await self.controller.async_press(self.controller.capture_press(), "button")

    @callback
    def _mqtt_message(self, message) -> None:
        if is_single_press(message.payload, self.config["mqtt_button"], retain=message.retain):
            self.submit_press("mqtt")

    @callback
    def _input_button_changed(self, event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if (
            old is None
            or new is None
            or old.state == new.state
            or new.state in ("unknown", "unavailable")
        ):
            return
        pressed_at = parse_datetime(new.state)
        if pressed_at is None or pressed_at.tzinfo is None:
            return
        if abs((event.time_fired - pressed_at).total_seconds()) > 5:
            return
        self.submit_press("input_button")

    async def async_start(self) -> None:
        stored = await self.store.async_load()
        # Absent/invalid persisted state must not activate automation.
        enabled = isinstance(stored, dict) and stored.get("enabled") is True
        self.controller.set_enabled(enabled)
        self._unsubscribers.append(
            await mqtt.async_subscribe(
                self.hass, self.config["mqtt_topic"], self._mqtt_message, qos=0
            )
        )
        if entity_id := self.config.get("input_button"):
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, [entity_id], self._input_button_changed)
            )

    async def async_set_enabled(self, enabled: bool) -> None:
        async with self._toggle_lock:
            if self._stopped:
                return
            if (
                enabled == self.controller.enabled
                and self.controller.last_error != "storage_failed"
            ):
                return
            # Close admission before any await, invalidate queued presses.
            self.controller.set_enabled(False)
            await self.controller.async_wait_idle()
            try:
                await self.store.async_save({"enabled": enabled})
            except Exception:
                self.controller.last_error = "storage_failed"
                self._changed()
                raise
            if not self._stopped:
                self.controller.set_enabled(enabled)

    async def async_stop(self) -> None:
        self._stopped = True
        self.controller.set_enabled(False)
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        await self.controller.async_wait_idle()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    @property
    def attributes(self) -> dict[str, Any]:
        controller = self.controller
        active = controller.deadline is not None and self.hass.loop.time() <= controller.deadline
        return {
            "process_type": "local_chandelier",
            "selection_step": controller.step if active else None,
            "expected_states": list(controller.expected) if controller.expected else None,
            "last_error": controller.last_error,
            "last_source": controller.last_source,
        }
