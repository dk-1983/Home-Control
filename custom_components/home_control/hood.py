"""Four-speed hood with readback barriers and a persistent resume speed."""

import asyncio
import logging
import math

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SERVICE_TIMEOUT
from .hood_readback import HoodReadback, ReadbackError

_LOGGER = logging.getLogger(__name__)
SPEEDS = (25, 50, 75, 100)
OUTPUT_KEYS = tuple(f"speed_{speed}" for speed in SPEEDS)
INPUT_KEYS = tuple(f"input_{speed}" for speed in SPEEDS)
DEFAULTS = {"feedback_timeout": 15.0, "break_delay": 0.5, "input_settle": 1.0}


def percentage_step(value):
    """Map HA percentages to four discrete speeds, with zero meaning stop."""
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise HomeAssistantError("Invalid hood percentage")
    return math.ceil(value / 25) * 25


class HoodFault(Exception):
    """No further start is allowed until the process is explicitly reset."""


class HoodRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.config = DEFAULTS | dict(entry.data) | dict(entry.options)
        self.outputs = [self.config[key] for key in OUTPUT_KEYS]
        self.inputs = [self.config[key] for key in INPUT_KEYS if self.config.get(key)]
        self.readback = HoodReadback(hass, self.outputs)
        self.controller = self
        self.listeners = set()
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self.enabled = False
        self.last_speed = 25
        self.last_error = None
        self.input_error = None
        self._failed = False
        self._stopped = False
        self._generation = 0
        self._target = None
        self._worker = None
        self._binding = None
        self._wake = asyncio.Event()
        self._toggle_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        self._stored_enabled = False
        self._unsubscribe = None
        self._input_timer = None
        self._input_baseline = None
        self.phase = "idle"
        self._light_lock = asyncio.Lock()
        self._light_running = None
        self.light_error = None

    def _states(self):
        return [
            self.hass.states.get(entity).state if self.hass.states.get(entity) else None
            for entity in self.outputs
        ]

    def _reported(self):
        """HA states are for display and conservative fault detection only."""
        states = self._states()
        if any(state not in ("on", "off") for state in states):
            return None
        return tuple(state == "on" for state in states)

    @property
    def percentage(self):
        values = self._reported()
        if values is None or sum(values) > 1:
            return None
        return SPEEDS[values.index(True)] if any(values) else 0

    @property
    def attributes(self):
        return {
            "process_type": "local_hood",
            "enabled": self.enabled,
            "last_speed": self.last_speed,
            "requested_percentage": self._target,
            "phase": self.phase,
            "last_error": self.last_error,
            "input_error": self.input_error,
            "light_error": self.light_error,
            "commands_blocked": self._failed,
        }

    @callback
    def _changed(self):
        for listener in tuple(self.listeners):
            listener()

    def _input_states(self):
        states = [self.hass.states.get(entity) for entity in self.inputs]
        if len(states) != 4 or any(s is None or s.state not in ("on", "off") for s in states):
            return None
        return tuple(s.state == "on" for s in states)

    def _cancel_input(self):
        if self._input_timer:
            self._input_timer.cancel()
            self._input_timer = None

    @callback
    def _event(self, event):
        self._wake.set()
        self._changed()
        if event.data["entity_id"] == self.config.get("hood_light"):
            return
        if event.data["entity_id"] not in self.inputs:
            values = self._reported()
            if (
                self.enabled
                and not self._stopped
                and not self._failed
                and (self._worker is None or self._worker.done())
                and values is not None
                and sum(values) > 1
            ):
                self._failed = True
                self.last_error = "multiple_active_outputs"
                self._submit(0)
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is not None and new is not None and old.state == new.state:
            return
        values = self._input_states()
        self._cancel_input()
        if not self.enabled or values is None or self._input_baseline is None:
            self._input_baseline = values
            return
        self._input_timer = self.hass.loop.call_later(self.config["input_settle"], self._panel)

    @callback
    def _panel(self):
        self._input_timer = None
        values = self._input_states()
        previous, self._input_baseline = self._input_baseline, values
        if not self.enabled or values is None or previous is None or values == previous:
            return
        if sum(values) > 1:
            self.input_error = "multiple_panel_inputs"
            self._changed()
            return
        self.input_error = None
        if not self._failed:
            self._submit(SPEEDS[values.index(True)] if any(values) else 0)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._generation += 1
        self._target = None
        self._cancel_input()
        self._input_baseline = self._input_states()
        percentage = self.percentage
        self._light_running = None if percentage is None else percentage > 0
        self._wake.set()
        if enabled:
            self.last_error = self.input_error = None
            self._failed = False
        self._changed()

    async def _save(self):
        async with self._store_lock:
            await self.store.async_save(
                {"enabled": self._stored_enabled, "last_speed": self.last_speed}
            )

    async def async_start(self):
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            if stored.get("last_speed") in SPEEDS:
                self.last_speed = stored["last_speed"]
            self._stored_enabled = stored.get("enabled") is True
        self._unsubscribe = async_track_state_change_event(
            self.hass,
            self.outputs
            + self.inputs
            + ([self.config["hood_light"]] if self.config.get("hood_light") else []),
            self._event,
        )
        # Restore permission and memory, never a motor command or a static panel level.
        self.set_enabled(self._stored_enabled)

    async def async_set_enabled(self, enabled):
        async with self._toggle_lock:
            if self._stopped:
                return
            self.set_enabled(False)
            await self.async_wait_idle()
            self._stored_enabled = enabled
            try:
                await self._save()
            except Exception:
                self._stored_enabled = False
                self.last_error = "storage_failed"
                self._changed()
                raise
            if not self._stopped:
                self.set_enabled(enabled)

    async def async_stop(self):
        self._stopped = True
        self.set_enabled(False)
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        await self.async_wait_idle()

    async def async_wait_idle(self):
        if self._worker:
            await asyncio.shield(self._worker)

    def _active(self, generation):
        return self.enabled and not self._stopped and generation == self._generation

    def _submit(self, target):
        try:
            binding = self.readback.resolve()
        except ReadbackError as exc:
            self._target = None
            self.last_error = str(exc)
            self._failed = True
            self._changed()
            return
        self._target = target
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._binding = binding
            self._worker = self.hass.async_create_task(self._run(self._generation))
        self._changed()

    async def async_request(self, percentage=None):
        if not self.enabled or self._stopped:
            raise HomeAssistantError("Hood automation is disabled")
        target = self.last_speed if percentage is None else percentage_step(percentage)
        if self._failed and target != 0:
            raise HomeAssistantError("Hood is blocked; inspect feedback and reset automation")
        # A newer voice/UI request supersedes a panel transition still being settled.
        self._cancel_input()
        self._input_baseline = self._input_states()
        self._submit(target)
        await self.async_wait_idle()
        if self._failed:
            raise HomeAssistantError(self.last_error or "Hood command failed")

    async def _command(self, entity, on, generation):
        if not self._active(generation):
            return False
        self._check_binding()
        async with asyncio.timeout(SERVICE_TIMEOUT):
            await self.hass.services.async_call(
                "switch", "turn_on" if on else "turn_off", {"entity_id": entity}, blocking=True
            )
        return self._active(generation)

    def _check_binding(self):
        if self.readback.resolve() != self._binding:
            raise HoodFault("readback_binding_changed")

    async def _read(self, generation):
        if not self._active(generation):
            return None
        try:
            self._check_binding()
            async with asyncio.timeout(self.config["feedback_timeout"]):
                while self._active(generation):
                    try:
                        values = await self.readback.async_read()
                        break
                    except ReadbackError as exc:
                        if str(exc) not in ("readback_concurrent_write", "readback_pending_write"):
                            raise
                        # Lighting on the same controller may write during a poll.
                        # Discard that snapshot and request another within the deadline.
                        await asyncio.sleep(0.05)
                else:
                    return None
            self._check_binding()
        except TimeoutError as exc:
            if not self._active(generation):
                return None
            raise HoodFault("feedback_timeout") from exc
        except Exception as exc:
            if not self._active(generation):
                return None
            if isinstance(exc, (ReadbackError, HoodFault)):
                raise HoodFault(str(exc)) from exc
            raise HoodFault("readback_failed") from exc
        if not self._active(generation):
            return None
        return values

    async def _wait(self, expected, generation, *, target=None):
        deadline = self.hass.loop.time() + self.config["feedback_timeout"]
        while self._active(generation):
            if target is not None and self._target != target:
                return False
            remaining = deadline - self.hass.loop.time()
            if remaining <= 0:
                raise HoodFault("feedback_timeout")
            try:
                async with asyncio.timeout(remaining):
                    values = await self._read(generation)
            except TimeoutError as exc:
                if not self._active(generation):
                    return False
                raise HoodFault("feedback_timeout") from exc
            if values is None or (target is not None and self._target != target):
                return False
            if values == expected:
                return True
            if sum(values) > 1:
                raise HoodFault("multiple_active_outputs")
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), min(max(0, deadline - self.hass.loop.time()), 0.2)
                )
            except TimeoutError:
                pass
        return False

    async def _off(self, generation):
        self.phase = "stopping"
        self._changed()
        for entity in self.outputs:
            if not await self._command(entity, False, generation):
                return False
        # A new physical read is requested only AFTER all off services complete.
        return await self._wait((False,) * 4, generation)

    async def _break(self, generation):
        self.phase = "break"
        self._changed()
        deadline = self.hass.loop.time() + self.config["break_delay"]
        while self._active(generation):
            if self._reported() != (False,) * 4:
                raise HoodFault("off_state_lost")
            if self.hass.loop.time() >= deadline:
                # The pause is not proof. Read again immediately before allowing ON.
                values = await self._read(generation)
                if values is None:
                    return False
                if values != (False,) * 4:
                    raise HoodFault("off_state_lost")
                return True
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), min(0.1, deadline - self.hass.loop.time())
                )
            except TimeoutError:
                pass
        return False

    def light_state(self):
        state = self.hass.states.get(self.config.get("hood_light", ""))
        return state.state if state else None

    async def async_light(self, on):
        generation = self._generation
        async with self._light_lock:
            if not self._active(generation):
                raise HomeAssistantError("Hood automation is disabled")
            entity = self.config.get("hood_light")
            if not entity or self.light_state() not in ("on", "off"):
                raise HomeAssistantError("Hood light is unavailable")
            desired = "on" if on else "off"
            if self.light_state() == desired:
                return
            async with asyncio.timeout(SERVICE_TIMEOUT):
                await self.hass.services.async_call(
                    entity.split(".")[0], f"turn_{desired}", {"entity_id": entity}, blocking=True
                )
            self.light_error = None
            self._changed()

    async def _follow_light(self, running, generation):
        if not self._active(generation):
            return
        previous, self._light_running = self._light_running, running
        if (
            previous is None
            or previous == running
            or not self.config.get("hood_light")
            or not self._active(generation)
        ):
            return
        try:
            await self.async_light(running)
        except Exception:
            self.light_error = "light_command_failed"
            _LOGGER.warning("Hood light command failed", exc_info=True)
            self._changed()

    async def _remember(self, target):
        if self.last_speed != target:
            self.last_speed = target
            try:
                await self._save()
            except Exception as exc:
                raise HoodFault("storage_failed") from exc

    async def _run(self, generation):
        try:
            while self._active(generation) and self._target is not None:
                target = self._target
                if target != 0:
                    values = await self._read(generation)
                    if values is None:
                        return
                    if self._light_running is None and sum(values) <= 1:
                        self._light_running = any(values)
                    # A newer request received during the read supersedes this one.
                    if self._target != target:
                        continue
                if target != 0 and values == tuple(speed == target for speed in SPEEDS):
                    await self._remember(target)
                    await self._follow_light(True, generation)
                    if self._target == target:
                        self._target = None
                    continue
                if not await self._off(generation):
                    return
                if not self._active(generation):
                    return
                if self._target == 0:
                    self._target = None
                    await self._follow_light(False, generation)
                    continue
                if not await self._break(generation):
                    return
                # Latest requested speed wins only after the all-off barrier.
                target = self._target
                if target == 0:
                    self._target = None
                    await self._follow_light(False, generation)
                    continue
                self.phase = "starting"
                self._changed()
                if not await self._command(self.outputs[SPEEDS.index(target)], True, generation):
                    return
                expected = tuple(speed == target for speed in SPEEDS)
                if await self._wait(expected, generation, target=target):
                    await self._remember(target)
                    await self._follow_light(True, generation)
                    if self._target == target:
                        self._target = None
        except Exception as exc:
            self.last_error = str(exc) if isinstance(exc, HoodFault) else "command_failed"
            self._failed = True
            self._target = None
            # Best-effort stop, never a retry of turn_on. Maintenance closes this gate too.
            for entity in self.outputs:
                try:
                    if not await self._command(entity, False, generation):
                        break
                except Exception:
                    _LOGGER.warning("Hood stop command failed", exc_info=True)
        finally:
            self.phase = "fault" if self._failed else "idle"
            self._changed()
