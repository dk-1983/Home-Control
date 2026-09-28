"""Home Assistant event, persistence and service adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import parse_datetime

from .const import DOMAIN, SERVICE_TIMEOUT, selected_groups
from .controller import ChandelierController, KitchenController, button_action


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
        self._store_lock = asyncio.Lock()
        self._stored_enabled = False
        self._loading = True
        self._memory_seen = None
        self._stopped = False
        self._night_expected: str | None = None
        self._night_expected_until = 0.0
        controller_type = (
            KitchenController if self.config.get("mode") == "kitchen" else ChandelierController
        )
        self.controller = controller_type(
            selected_groups(self.config),
            self._states,
            self._send,
            window=self.config["selection_window"],
            feedback_timeout=self.config["feedback_timeout"],
            clock=hass.loop.time,
            changed=self._changed,
        )

    @property
    def has_group_light(self):
        return not isinstance(self.controller, KitchenController)

    async def _save_state(self):
        async with self._store_lock:
            data = {"enabled": self._stored_enabled}
            if self.has_group_light:
                data.update(
                    groups=list(self.controller.groups),
                    last_pattern=list(self.controller.last_pattern),
                )
            await self.store.async_save(data)

    async def _save_memory(self):
        try:
            await self._save_state()
        except Exception:
            self.controller.last_error = "storage_failed"
            self._changed()

    @callback
    def _groups_changed(self, event):
        self.controller.check_feedback()
        self._changed()

    async def async_light(self, on, brightness=None):
        if not self.has_group_light or self._stopped or not self.controller.enabled:
            raise HomeAssistantError("Lighting automation is disabled")
        await self.controller.async_light(self.controller.capture_press(), on, brightness)
        # Persist confirmed changes before completing a user-facing service call.
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks))
        if self.controller.last_error:
            raise HomeAssistantError(self.controller.last_error)

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

    async def _send_night_light(self) -> None:
        entity_id = self.config["night_light"]
        domain = entity_id.split(".", 1)[0]
        if domain not in ("light", "switch"):
            self._night_expected = None
            raise HomeAssistantError("Night light must be a light or switch")
        state = self.hass.states.get(entity_id)
        if state is None or state.state not in ("on", "off"):
            self._night_expected = None
            raise HomeAssistantError("Night light is unavailable")
        now = self.hass.loop.time()
        if state.state == self._night_expected or now >= self._night_expected_until:
            self._night_expected = None
        baseline = self._night_expected or state.state
        target = "off" if baseline == "on" else "on"
        try:
            async with asyncio.timeout(SERVICE_TIMEOUT):
                await self.hass.services.async_call(
                    domain, f"turn_{target}", {"entity_id": entity_id}, blocking=True
                )
        except BaseException:
            self._night_expected = None
            raise
        if self.controller.enabled and not self._stopped:
            self._night_expected = target
            self._night_expected_until = self.hass.loop.time() + self.config["feedback_timeout"]

    @callback
    def _changed(self) -> None:
        if (
            self.has_group_light
            and not self._loading
            and self._memory_seen != self.controller.last_pattern
        ):
            self._memory_seen = self.controller.last_pattern
            task = self.hass.async_create_task(self._save_memory())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if not self.controller.enabled:
            self._night_expected = None
            self._night_expected_until = 0.0
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
    def submit_press(self, source: str, action: str = "SINGLE") -> None:
        if self._stopped or (press := self.controller.capture_press()) is None:
            return
        if action == "HOLD" and not self.config.get("night_light"):
            return
        operation = (
            self.controller.async_hold(press, self._send_night_light)
            if action == "HOLD"
            else self.controller.async_press(press, source)
        )
        task = self.hass.async_create_task(
            operation,
            f"Home Control button: {self.entry.entry_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def async_press(self) -> None:
        if not self._stopped:
            await self.controller.async_press(self.controller.capture_press(), "button")

    @callback
    def _mqtt_message(self, message) -> None:
        action = button_action(message.payload, self.config["mqtt_button"], retain=message.retain)
        if action is not None:
            self.submit_press("mqtt", action)

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
        self._stored_enabled = enabled
        if (
            self.has_group_light
            and isinstance(stored, dict)
            and stored.get("groups") == list(self.controller.groups)
        ):
            pattern = stored.get("last_pattern")
            if (
                isinstance(pattern, list)
                and len(pattern) == len(self.controller.groups)
                and all(s in ("on", "off") for s in pattern)
                and "on" in pattern
            ):
                self.controller.last_pattern = tuple(pattern)
        self._memory_seen = self.controller.last_pattern
        self._loading = False
        self.controller.set_enabled(enabled)
        if self.has_group_light:
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass, list(self.controller.groups), self._groups_changed
                )
            )
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
                self._stored_enabled = enabled
                await self._save_state()
            except Exception:
                self._stored_enabled = False
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
            "process_type": "local_kitchen"
            if isinstance(controller, KitchenController)
            else "local_chandelier",
            "selection_step": controller.step if active else None,
            "expected_states": list(controller.expected) if controller.expected else None,
            "last_error": controller.last_error,
            "last_source": controller.last_source,
        }
