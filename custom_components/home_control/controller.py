"""Independent, serialized lighting controllers; no Home Assistant imports."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Press:
    """Timestamp and lifecycle generation captured at event arrival."""

    at: float
    generation: int


def button_action(payload: str | bytes, button: str, *, retain: bool = False) -> str | None:
    """Decode supported actions; never replay retained button messages."""
    if retain:
        return None
    try:
        data = json.loads(payload)
    except ValueError, TypeError, UnicodeDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get(button), dict):
        return None
    action = data[button].get("Action")
    return action if action in ("SINGLE", "HOLD") else None


def is_single_press(payload: str | bytes, button: str, *, retain: bool = False) -> bool:
    return button_action(payload, button, retain=retain) == "SINGLE"


class ChandelierController:
    """Own one ordered set of switches and a short optimistic selection cycle.

    Methods run in one event loop. Commands are serialized, and maintenance
    invalidates queued presses immediately. Already dispatched commands cannot
    be recalled. Feedback is checked explicitly by the integration's timer.
    """

    def __init__(
        self,
        groups: Sequence[str],
        read_states: Callable[[], Sequence[str | None]],
        send: Callable[[bool, tuple[str, ...]], Awaitable[None]],
        *,
        window: float = 3.0,
        feedback_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        changed: Callable[[], None] = lambda: None,
    ) -> None:
        if not 1 <= len(groups) <= 4 or len(set(groups)) != len(groups):
            raise ValueError("One to four distinct switches are required")
        if not 0.2 <= window <= 30 or not 1 <= feedback_timeout <= 60:
            raise ValueError("Invalid timing configuration")
        self.groups = tuple(groups)
        self.read_states = read_states
        self.send = send
        self.window = window
        self.feedback_timeout = feedback_timeout
        self.clock = clock
        self.changed = changed
        self.enabled = False
        self.step: int | None = None
        self.deadline: float | None = None
        self.expected: tuple[str, ...] | None = None
        self.feedback_due: float | None = None
        self.last_error: str | None = None
        self.last_source: str | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    def _reset(self) -> None:
        self.step = self.deadline = self.expected = self.feedback_due = None

    def set_enabled(self, enabled: bool) -> None:
        """Open/close admission; never send equipment commands."""
        self.enabled = enabled
        self._generation += 1
        self._reset()
        self.last_error = None
        self.changed()

    def capture_press(self) -> Press | None:
        return Press(self.clock(), self._generation) if self.enabled else None

    async def async_wait_idle(self) -> None:
        """Wait for an already dispatched service call to finish."""
        async with self._lock:
            pass

    def _fail(self, reason: str) -> None:
        self._generation += 1
        self._reset()
        self.last_error = reason
        self.changed()

    def check_feedback(self) -> None:
        """Confirm HA-reported states, or abandon an unconfirmed expectation.

        This is state feedback, not proof of electrical output. No retries and
        no automatic corrective switching are performed.
        """
        if self.expected is None:
            return
        if tuple(self.read_states()) == self.expected:
            self.expected = self.feedback_due = None
            self.changed()
        elif self.feedback_due is not None and self.clock() >= self.feedback_due:
            _LOGGER.warning("Lighting state feedback timeout")
            self._fail("feedback_timeout")

    def _plan(self, press: Press, states: tuple[str, ...]):
        """Return the next step, expected state and ordered service commands."""
        active = self.deadline is not None and press.at <= self.deadline
        if active and self.step is not None:
            next_step = len(self.groups) if self.step == 0 else self.step - 1
            on = next_step == len(self.groups)
            targets = self.groups if on else (self.groups[len(self.groups) - 1 - next_step],)
        else:
            # Briefly trust an unconfirmed command even after selection
            # expires, so delayed feedback cannot start the wrong cycle.
            baseline = self.expected if self.expected is not None else states
            on = all(s == "off" for s in baseline)
            next_step = len(self.groups) if on else 0
            targets = self.groups
        expected = ("off",) * (len(self.groups) - next_step) + ("on",) * next_step
        return next_step, expected, ((on, targets),)

    async def async_press(self, press: Press | None, source: str) -> None:
        if press is None:
            return
        async with self._lock:
            if not self.enabled or press.generation != self._generation:
                return
            # Never replay old input after a stalled service call.
            if self.clock() - press.at > 10:
                self._fail("stale_press")
                return
            self.last_source = source
            states = tuple(self.read_states())
            if len(states) != len(self.groups) or any(s not in ("on", "off") for s in states):
                self._fail("unavailable_group")
                return
            generation = self._generation
            self.check_feedback()
            if generation != self._generation:
                return

            next_step, expected, commands = self._plan(press, states)

            # Suspend the previous feedback timer while a new service is in
            # flight. The new target will replace it after service success.
            self.expected = self.feedback_due = None
            self.changed()
            try:
                for on, targets in commands:
                    # Maintenance can close the gate during an earlier service
                    # in a multi-command transition. Do not dispatch the rest.
                    if not self.enabled or generation != self._generation:
                        return
                    await self.send(on, targets)
            except asyncio.CancelledError:
                self._fail("command_cancelled")
                raise
            except Exception:
                _LOGGER.exception("Lighting command failed; selection reset")
                self._fail("command_failed")
                return
            if not self.enabled or generation != self._generation:
                return
            self.step = next_step
            self.deadline = press.at + self.window
            self.expected = expected
            self.feedback_due = self.clock() + self.feedback_timeout
            self.last_error = None
            self.changed()

    async def async_hold(self, press: Press | None, action: Callable[[], Awaitable[None]]) -> None:
        """Run the night light action under the same gate and command lock.

        A successful HOLD does not advance or extend chandelier selection.
        """
        if press is None:
            return
        async with self._lock:
            if not self.enabled or press.generation != self._generation:
                return
            if self.clock() - press.at > 10:
                self._fail("stale_press")
                return
            generation = self._generation
            self.last_source = "mqtt_hold"
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Night light command failed")
                if generation == self._generation:
                    self.last_error = "night_light_command_failed"
                    self.changed()
                return
            if self.enabled and generation == self._generation:
                if self.last_error == "night_light_command_failed":
                    self.last_error = None
                self.changed()


class KitchenController(ChandelierController):
    """Kitchen combinations with the same sliding selection window and gate."""

    PATTERNS = (
        ("off", "off", "off", "off"),
        ("on", "off", "off", "off"),
        ("on", "on", "off", "off"),
        ("on", "on", "on", "off"),
        ("off", "off", "on", "off"),
        ("off", "off", "off", "on"),
    )

    def __init__(self, groups, read_states, send, *, window=3.0, **kwargs):
        if len(groups) != 4:
            raise ValueError("Kitchen mode requires four distinct switches")
        super().__init__(groups, read_states, send, window=window, **kwargs)

    def _plan(self, press: Press, states: tuple[str, ...]):
        active = self.deadline is not None and press.at <= self.deadline
        if active and self.step is not None:
            baseline = self.PATTERNS[self.step]
            next_step = (self.step + 1) % len(self.PATTERNS)
        else:
            baseline = self.expected if self.expected is not None else states
            next_step = 1 if all(s == "off" for s in baseline) else 0
        expected = self.PATTERNS[next_step]
        if next_step == 0:
            return next_step, expected, ((False, self.groups),)
        commands = []
        # Turn off the previous group before turning on its replacement.
        for on in (False, True):
            target_state = "on" if on else "off"
            targets = tuple(
                group
                for group, before, after in zip(self.groups, baseline, expected, strict=True)
                if before != after and after == target_state
            )
            if targets:
                commands.append((on, targets))
        return next_step, expected, tuple(commands)
