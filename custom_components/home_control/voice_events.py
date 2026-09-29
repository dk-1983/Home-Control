"""Process events and routing identity; no equipment control or polling."""

import logging
from dataclasses import dataclass

from homeassistant.core import callback
from homeassistant.helpers import device_registry
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, selected_groups
from .process_config import owned_outputs

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notice:
    source: str
    level: str
    key: str
    message: str
    active: bool = False


class VoiceBus:
    def __init__(self):
        self.sources = {}
        self.active = {}
        self.center = None

    def publish(self, notice):
        identity = (notice.source, notice.key)
        source = self.sources.get(notice.source)
        if source is None or not source.runtime.controller.enabled:
            return
        if notice.active:
            previous = self.active.get(identity)
            if previous == notice:
                return
            self.active[identity] = notice
        if self.center:
            self.center.submit(notice)

    def resolve(self, source, key):
        self.active.pop((source, key), None)
        if self.center:
            self.center.invalidate(source, key)

    def remove(self, source):
        self.sources.pop(source, None)
        if self.center:
            self.center.invalidate(source)
        for identity in list(self.active):
            if identity[0] == source:
                self.resolve(*identity)

    def allowed(self, notice, repeat=False):
        source = self.sources.get(notice.source)
        if source is None or not source.runtime.controller.enabled:
            return False
        config = source.runtime.config
        return bool(config.get("voice_" + notice.level.lower(), False)) and (
            not repeat or config.get("voice_repeat", False)
        )


def voice_bus(hass):
    if "home_control_voice" not in hass.data:
        hass.data["home_control_voice"] = VoiceBus()
    return hass.data["home_control_voice"]


class ProcessVoice:
    def __init__(self, runtime):
        self.runtime = runtime
        self.hass = runtime.hass
        self.source = runtime.entry.entry_id
        self.bus = voice_bus(self.hass)
        self.outputs = sorted(owned_outputs(runtime.config))
        if not runtime.config.get("process_type"):
            self.outputs = selected_groups(runtime.config)
        self._issues = set()
        self._baseline = None
        self._timer = None
        self._unsubscribe = None
        self._enabled = runtime.controller.enabled
        self._completed = {}

    def area(self):
        if area := self.runtime.config.get("voice_area"):
            return area
        device = device_registry.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.source)}
        )
        return device.area_id if device else None

    def start(self):
        self.bus.sources[self.source] = self
        self.runtime.listeners.add(self.update)
        if self.outputs:
            self._unsubscribe = async_track_state_change_event(self.hass, self.outputs, self._event)
        self._baseline = self._snapshot()
        self._completed = {
            e: r.get("last_success") for e, r in self.runtime.attributes.get("valves", {}).items()
        }
        self.update()

    def stop(self):
        self.runtime.listeners.discard(self.update)
        if self._unsubscribe:
            self._unsubscribe()
        if self._timer:
            self._timer.cancel()
        self.bus.remove(self.source)

    @callback
    def _event(self, event):
        self.update()

    def _snapshot(self):
        states = [self.hass.states.get(e) for e in self.outputs]
        if not states or any(s is None or s.state not in ("on", "off") for s in states):
            return None
        return tuple(s.state for s in states)

    @callback
    def update(self):
        # An observer must never disrupt the equipment controller's callbacks.
        try:
            self._update()
        except Exception:
            _LOGGER.exception("Could not observe voice events for %s", self.source)

    def _update(self):
        enabled = self.runtime.controller.enabled
        if not enabled:
            for source, key in list(self.bus.active):
                if source == self.source:
                    self.bus.resolve(source, key)
            if self.bus.center:
                self.bus.center.invalidate(self.source)
            self._issues.clear()
            self._baseline = self._snapshot()
            self._enabled = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            return
        attrs = self.runtime.attributes
        title = self.runtime.entry.title
        issues = {}
        for key in ("last_error", "input_error", "light_error"):
            if attrs.get(key):
                # These are source-reported faults, not fresh physical diagnoses.
                issues[key] = (
                    "ERROR",
                    f"{title}. "
                    + {
                        "last_error": "Процесс сообщает об ошибке. Проверьте состояние автоматики.",
                        "input_error": "Ошибка сигналов ручного управления.",
                        "light_error": "Не удалось управлять подсветкой вытяжки.",
                    }[key],
                )
        if attrs.get("last_result") in ("speaker_failed", "storage_failed"):
            issues["delivery"] = (
                "ERROR",
                f"{title}. Последняя попытка подачи звонка завершилась ошибкой.",
            )
        if attrs.get("sensor_valid") is False:
            issues["sensor"] = (
                "WARNING",
                f"{title}. Недостаточно достоверных данных датчика влажности.",
            )
        records = attrs.get("valves", {})
        for index, (entity, record) in enumerate(records.items(), 1):
            key = f"valve_{index}"
            if record.get("status") in ("failed", "interrupted"):
                issues[key] = ("ERROR", f"{title}. Тренировка клапана номер {index} не завершена.")
            elif record.get("status") == "pending" and record.get("error"):
                issues[key] = ("WARNING", f"{title}. Тренировка клапана номер {index} отложена.")
            success = record.get("last_success")
            if self._enabled and success and success != self._completed.get(entity):
                self.bus.publish(
                    Notice(
                        self.source,
                        "INFO",
                        key + "_done",
                        f"{title}. Тренировка клапана номер {index} завершена.",
                    )
                )
            self._completed[entity] = success
        for key in self._issues - issues.keys():
            self.bus.resolve(self.source, key)
        for key, (level, message) in issues.items():
            self.bus.publish(Notice(self.source, level, key, message, True))
        self._issues = set(issues)
        if not self._enabled:
            self._baseline = self._snapshot()
        self._enabled = True
        current = self._snapshot()
        if self.runtime.config.get("process_type") == "valve_exercise":
            return
        if current != self._baseline and self._timer is None:
            self._timer = self.hass.loop.call_later(2, self._info)

    @callback
    def _info(self):
        self._timer = None
        current = self._snapshot()
        previous, self._baseline = self._baseline, current
        if (
            current is None
            or previous is None
            or current == previous
            or not self.runtime.controller.enabled
        ):
            return
        title = self.runtime.entry.title
        if self.runtime.config.get("process_type") == "hood":
            # Read the process's settled speed, not the intermediate all-off gap.
            if self.runtime.attributes.get("phase") not in ("idle", "fault"):
                self._baseline = previous
                self._timer = self.hass.loop.call_later(2, self._info)
                return
            if self.runtime.attributes.get("phase") == "fault":
                return
            percentage = self.runtime.percentage
            message = (
                f"{title}. Скорость {percentage} процентов."
                if percentage
                else f"{title}. Вытяжка выключена."
            )
        elif len(current) > 1:
            message = f"{title}. Включено групп: {current.count('on')} из {len(current)}."
        else:
            message = f"{title}. " + ("Включено." if current[0] == "on" else "Выключено.")
        self.bus.publish(Notice(self.source, "INFO", "state", message))
