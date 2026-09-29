"""Public, leased voice producers; independent of any equipment integration."""

import hashlib
import json
from dataclasses import dataclass, field
from uuid import uuid4

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse, callback

from .const import DOMAIN
from .voice_events import Notice
from .voice_protocol import (
    DISCOVER,
    LEASE_SECONDS,
    PUBLISH,
    READY,
    REGISTER,
    SCHEMAS,
    SERVICES,
    SOURCE_FIELDS,
    UNREGISTER,
    VERSION,
)

# Refuse new identities/sessions on exhaustion instead of forgetting tombstones
# and accepting a previously superseded producer. These are protocol resources,
# not apartment configuration controls.
MAX_SOURCES = 128
MAX_RETIRED_SESSIONS = 1024


@dataclass
class ExternalSource:
    receiver: object
    identity: tuple[str, str, str]
    session: str
    name: str = ""
    area_id: str | None = None
    preferences: dict = field(default_factory=dict)
    maintenance: bool = True
    registered: bool = False
    closed: bool = False
    expires: float = 0
    revision: int = 0
    fingerprint: str = ""
    response: dict = field(default_factory=dict)

    @property
    def enabled(self):
        return (
            self.receiver.running
            and self.registered
            and not self.closed
            and not self.maintenance
            and self.expires > self.receiver.now()
            and self.receiver.owner_loaded(self.identity)
        )

    def area(self):
        return self.area_id


class ExternalVoiceRegistry:
    def __init__(self, center):
        self.center = center
        self.hass = center.hass
        self.bus = center.bus
        self.center_session = uuid4().hex
        self.sources = {}
        self.retired = {}
        self.running = False
        self._unsub = None
        self._timer = None

    def now(self):
        return self.hass.loop.time()

    def owner_loaded(self, identity):
        manager = self.hass.config_entries
        entry = manager.async_get_entry(identity[1]) if manager is not None else None
        return (
            entry is not None
            and entry.domain == identity[0]
            and entry.state is ConfigEntryState.LOADED
        )

    @callback
    def start(self):
        self.running = True
        for service in SERVICES:
            self.hass.services.async_register(
                DOMAIN,
                service,
                self._handle,
                schema=SCHEMAS[service],
                supports_response=SupportsResponse.ONLY,
            )
        self._unsub = self.hass.bus.async_listen(DISCOVER, self._discover)
        self._ready()

    @callback
    def _ready(self):
        if self.running:
            self.hass.bus.async_fire(
                READY, {"protocol_version": VERSION, "center_session": self.center_session}
            )

    @callback
    def _discover(self, event):
        version = event.data.get("protocol_version")
        if type(version) is int and version == VERSION:
            self._ready()

    def _schedule_expiry(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        deadlines = [s.expires for s in self.sources.values() if s.registered]
        if self.running and deadlines:
            self._timer = self.hass.loop.call_at(min(deadlines), self.expire)

    @callback
    def expire(self):
        changed = False
        for source in self.sources.values():
            if source.registered and (
                source.expires <= self.now() or not self.owner_loaded(source.identity)
            ):
                self._deactivate(source)
                changed = True
        self._schedule_expiry()
        if changed:
            self.center._changed()

    def _deactivate(self, source):
        source.registered = False
        self.bus.remove(source.identity)

    @staticmethod
    def _reject(reason):
        return {"accepted": False, "reason": reason}

    async def _handle(self, call):
        # No awaits inside the transaction: validate, compare revision, replace
        # snapshot and acknowledge atomically within HA's event loop.
        if not self.running:
            return self._reject("registration_required")
        data = call.data
        identity = tuple(data["source"][key] for key in SOURCE_FIELDS)
        session, revision = data["producer_session"], data["revision"]
        operation = call.service
        self.expire()
        old = self.sources.get(identity)
        if operation != REGISTER and data["center_session"] != self.center_session:
            return self._reject("registration_required")
        if session in self.retired.get(identity, set()):
            return self._reject("retired_session")
        if not self.owner_loaded(identity):
            return self._reject("unknown_source")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": operation, "data": dict(data)},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        same_session = old is not None and old.session == session
        if operation == PUBLISH and (not same_session or not old.registered):
            return self._reject("registration_required")
        if same_session and revision <= old.revision:
            if revision < old.revision:
                return self._reject("stale_revision")
            if fingerprint != old.fingerprint:
                return self._reject("revision_conflict")
            # Return the previous acknowledgement without renewing the lease or
            # reviving a removed registration. A new revision is required.
            return dict(old.response)
        if same_session and old.closed:
            return self._reject("retired_session")
        if operation != REGISTER and (not same_session or not old.registered):
            return self._reject("registration_required")
        if operation == REGISTER:
            if old is None and len(self.sources) >= MAX_SOURCES:
                return self._reject("source_capacity")
            if old is not None and not same_session:
                retired = self.retired.setdefault(identity, set())
                if len(retired) >= MAX_RETIRED_SESSIONS:
                    return self._reject("session_capacity")
                retired.add(old.session)
                self._deactivate(old)
            if not same_session:
                old = ExternalSource(self, identity, session)
                self.sources[identity] = old
            self._register(old, data)
            response = {
                "accepted": True,
                "protocol_version": VERSION,
                "center_session": self.center_session,
                "lease_seconds": LEASE_SECONDS,
            }
        elif operation == PUBLISH:
            event = data["event"]
            if event["resolved"]:
                self.bus.resolve(identity, event["key"])
            elif not old.maintenance:
                if (
                    event["active"]
                    and (identity, event["key"]) not in self.bus.active
                    and sum(key[0] == identity for key in self.bus.active) >= 64
                ):
                    return self._reject("issue_capacity")
                self.bus.publish(
                    Notice(
                        identity, event["level"], event["key"], event["message"], event["active"]
                    )
                )
            response = {"accepted": True}
        elif operation == UNREGISTER:
            self._deactivate(old)
            old.closed = True
            response = {"accepted": True}
        else:
            return self._reject("unknown_operation")
        old.revision, old.fingerprint, old.response = revision, fingerprint, response
        self._schedule_expiry()
        self.center._changed()
        return dict(response)

    def _register(self, source, data):
        was_enabled = source.enabled
        preferences = dict(source.preferences)
        source.name = data["name"]
        source.area_id = data["area_id"] or None
        source.preferences = dict(data["preferences"])
        source.maintenance = data["maintenance"]
        source.registered = True
        source.expires = self.now() + LEASE_SECONDS
        self.bus.sources[source.identity] = source
        if source.maintenance:
            self.bus.remove(source.identity)
            self.bus.sources[source.identity] = source
            return
        disabled = {
            level
            for level in ("INFO", "WARNING", "ERROR")
            if not source.preferences["voice_" + level.lower()]
        }
        self.center.invalidate_levels(source.identity, disabled)
        if not source.preferences["voice_repeat"]:
            self.center.cancel_repeats(source.identity)
        issues = {issue["key"]: issue for issue in data["active_issues"]}
        for identity in list(self.bus.active):
            if identity[0] == source.identity and identity[1] not in issues:
                self.bus.resolve(*identity)
        for key, issue in issues.items():
            notice = Notice(source.identity, issue["level"], key, issue["message"], True)
            previous = self.bus.active.get((source.identity, key))
            self.bus.publish(notice)
            # Enabling a previously suppressed level admits the current problem
            # once. An ordinary heartbeat never resets reminder time.
            preference = "voice_" + issue["level"].lower()
            if (
                previous == notice
                and source.preferences[preference]
                and (not was_enabled or not preferences.get(preference, False))
            ):
                self.center.submit(previous)

    def summaries(self):
        return [
            {
                "domain": source.identity[0],
                "process_id": source.identity[2],
                "name": source.name,
                "status": (
                    "unregistered"
                    if source.closed
                    else "expired"
                    if not source.registered or source.expires <= self.now()
                    else "unloaded"
                    if not self.owner_loaded(source.identity)
                    else "maintenance"
                    if source.maintenance
                    else "registered"
                ),
                "active_issues": sum(
                    identity[0] == source.identity for identity in self.bus.active
                ),
            }
            for source in self.sources.values()
        ]

    @callback
    def stop(self):
        if not self.running:
            return
        self.running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._unsub:
            self._unsub()
            self._unsub = None
        for service in SERVICES:
            self.hass.services.async_remove(DOMAIN, service)
        for source in self.sources.values():
            self._deactivate(source)
        self.sources.clear()
        self.retired.clear()
