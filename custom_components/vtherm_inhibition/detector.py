"""Source detection, debounce, unavailable grace period and bypass.

One ``InhibitionDetector`` exists per plugin config entry (that is, per
VTherm). It only computes the plugin's business status; ``VThermLink``
(``link.py``) turns the confirmed status into calls to the VTherm API.

Vocabulary used below:

* **raw status**: the instantaneous aggregate of the sources (and bypass),
  never acted upon directly;
* **confirmed status**: the status acted upon (``set_inhibition`` for
  ``ACTIVE``/``UNKNOWN``, ``unset_inhibition`` for ``INACTIVE``) once the
  configured delays have elapsed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HassJob, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util
from vtherm_api.log_collector import get_vtherm_logger

from .const import SOURCE_ACTIVE, SOURCE_DOMAIN_CLIMATE, SOURCE_INACTIVE, SOURCE_UNAVAILABLE
from .models import InhibitionConfig, InhibitionStatus

_LOGGER = get_vtherm_logger(__name__)


@dataclass(frozen=True, slots=True)
class SourceState:
    """Diagnostic view of one source."""

    entity_id: str
    state: str | None
    contribution: str


def source_contribution(state: State | None, climate_hvac_modes: frozenset[str]) -> str:
    """Return ``active``, ``inactive`` or ``unavailable`` for one source state.

    * missing entity, ``unavailable`` or ``unknown`` -> ``unavailable``;
    * ``climate``: ``active`` when its state (the HVAC mode) is in
      ``climate_hvac_modes``, ``inactive`` otherwise;
    * ``binary_sensor`` / ``input_boolean`` / ``switch``: ``on`` -> ``active``,
      ``off`` -> ``inactive``, any other value -> ``unavailable``.
    """
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return SOURCE_UNAVAILABLE
    if state.domain == SOURCE_DOMAIN_CLIMATE:
        return SOURCE_ACTIVE if state.state in climate_hvac_modes else SOURCE_INACTIVE
    if state.state == STATE_ON:
        return SOURCE_ACTIVE
    if state.state == STATE_OFF:
        return SOURCE_INACTIVE
    return SOURCE_UNAVAILABLE


def aggregate(contributions: list[str]) -> InhibitionStatus:
    """Three-valued OR of the sources.

    * at least one source ``active`` -> ``ACTIVE`` (even if others are
      unavailable);
    * otherwise at least one source ``unavailable`` -> ``UNKNOWN``;
    * otherwise (all sources ``inactive``, or no source) -> ``INACTIVE``.
    """
    if SOURCE_ACTIVE in contributions:
        return InhibitionStatus.ACTIVE
    if SOURCE_UNAVAILABLE in contributions:
        return InhibitionStatus.UNKNOWN
    return InhibitionStatus.INACTIVE


class InhibitionDetector:
    """Turn raw source states into a confirmed inhibition status."""

    def __init__(self, hass: HomeAssistant, config: InhibitionConfig, name: str) -> None:
        self._hass = hass
        self._config = config
        self._name = name
        self._status = InhibitionStatus.UNKNOWN
        self._raw = InhibitionStatus.UNKNOWN
        self._live = False
        self._bypass = False
        self._started = False
        self._sources: dict[str, SourceState] = {}
        self._timer_cancel: CALLBACK_TYPE | None = None
        self._timer_target: InhibitionStatus | None = None
        self._timer_until: datetime | None = None
        self._timer_started: datetime | None = None
        self._timer_job = HassJob(self._async_timer_fired, f"{name} inhibition timer", cancel_on_shutdown=True)
        self._unsub_sources: CALLBACK_TYPE | None = None
        self._unsub_registry: CALLBACK_TYPE | None = None
        # Start of the current unavailability episode (see _update_episode).
        self._unavailable_since: datetime | None = None
        self._missing_sources: tuple[str, ...] = ()
        #: Called with (old_entity_id, new_entity_id) when a source is renamed.
        self.rename_handler: Callable[[str, str], None] | None = None
        self._update_listeners: list[Callable[[], None]] = []
        self._last_change: datetime | None = None

    def __str__(self) -> str:
        return f"VThermInhibition-{self._name}"

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------
    @property
    def hass(self) -> HomeAssistant:
        """Return Home Assistant."""
        return self._hass

    @property
    def config(self) -> InhibitionConfig:
        """Return the effective configuration."""
        return self._config

    @property
    def status(self) -> InhibitionStatus:
        """Return the confirmed status."""
        return self._status

    @property
    def raw_status(self) -> InhibitionStatus:
        """Return the instantaneous (unconfirmed) aggregate."""
        return self._raw

    @property
    def bypass(self) -> bool:
        """Return True when the bypass switch is on."""
        return self._bypass

    @property
    def is_started(self) -> bool:
        """Return True while the detector listens to its sources."""
        return self._started

    @property
    def pending_status(self) -> InhibitionStatus | None:
        """Return the status a running timer will confirm, if any."""
        return self._timer_target

    @property
    def pending_until(self) -> datetime | None:
        """Return when the running timer fires, if any."""
        return self._timer_until

    @property
    def source_states(self) -> list[SourceState]:
        """Return the diagnostic state of every source."""
        return [self._sources[entity_id] for entity_id in self._config.source_entities if entity_id in self._sources]

    @property
    def missing_sources(self) -> tuple[str, ...]:
        """Configured sources that exist neither in the entity registry nor as a state."""
        return self._missing_sources

    @property
    def unavailable_since(self) -> datetime | None:
        """Start of the current unavailability episode, if any."""
        return self._unavailable_since

    @property
    def active_sources(self) -> list[str]:
        """Return the sources currently contributing ``active``."""
        return [source.entity_id for source in self.source_states if source.contribution == SOURCE_ACTIVE]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @callback
    def restore_status(self, status: str | None, unavailable_since: datetime | None = None) -> None:
        """Restore the last confirmed status from before a restart or reload.

        Deliberate plugin-side choice for sources with short dropouts: instead
        of reporting UNKNOWN at once, the restored status is kept for at most
        the grace period while the sources are not available yet, then
        UNKNOWN is reported. Only used when the grace period is not zero.
        ``unavailable_since`` continues an unavailability episode that was
        running before the reload/restart, so the grace is not granted twice.
        Ignored once the detector has started.
        """
        if self._started or self._config.unavailable_grace_period <= 0:
            return
        if status in (InhibitionStatus.ACTIVE, InhibitionStatus.INACTIVE):
            self._status = InhibitionStatus(status)
            self._unavailable_since = unavailable_since
            _LOGGER.debug("%s - restored confirmed status %s (unavailable since %s)", self, status, unavailable_since)

    @callback
    def restore_bypass(self, bypass: bool) -> None:
        """Restore the bypass switch before the detector starts."""
        if not self._started:
            self._bypass = bool(bypass)

    async def async_start(self) -> None:
        """Subscribe to the sources and evaluate them immediately."""
        if self._started:
            return
        self._started = True
        self._subscribe()
        _LOGGER.info(
            "%s - started with sources %s, stop_underlyings %s",
            self,
            list(self._config.source_entities),
            self._config.stop_underlyings,
        )
        self._evaluate()

    @callback
    def _unsubscribe(self) -> None:
        for unsub in (self._unsub_sources, self._unsub_registry):
            if unsub is not None:
                unsub()
        self._unsub_sources = None
        self._unsub_registry = None

    @callback
    def _subscribe(self) -> None:
        self._unsubscribe()
        if self._config.source_entities:
            sources = list(self._config.source_entities)
            self._unsub_sources = async_track_state_change_event(self._hass, sources, self._async_source_changed)
            self._unsub_registry = async_track_entity_registry_updated_event(
                self._hass, sources, self._async_source_registry_updated
            )

    @callback
    def _async_source_registry_updated(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Follow renamed sources; re-evaluate on removal (missing source)."""
        data = event.data
        if data["action"] == "update" and "old_entity_id" in data and self.rename_handler is not None:
            self.rename_handler(data["old_entity_id"], data["entity_id"])
            return
        self._evaluate()

    @callback
    def async_reconfigure(self, config: InhibitionConfig) -> None:
        """Apply new options in place, without reloading the entry.

        * The sources are re-subscribed and re-evaluated. A raw status change
          caused by the new sources or HVAC modes follows the normal rules
          (activation/deactivation delay, grace period); before the first
          confirmation the first known aggregate is still confirmed at once.
        * A timer still running for the same target keeps its start time; its
          deadline becomes start + new delay (confirmed at once if already
          elapsed).
        * ``stop_underlyings`` is read when ``set_inhibition`` is called: a
          change while the VTherm is inhibited applies from the next entry.
        """
        old_config = self._config
        if config == old_config:
            return
        self._config = config
        _LOGGER.info(
            "%s - reconfigured: sources %s, stop_underlyings %s",
            self,
            list(config.source_entities),
            config.stop_underlyings,
        )
        if not self._started:
            self._publish()
            return
        if config.source_entities != old_config.source_entities:
            self._sources = {
                entity_id: source for entity_id, source in self._sources.items() if entity_id in config.source_entities
            }
            self._subscribe()
        self._evaluate()
        self._reschedule_running_timer()
        self._publish()

    @callback
    def _reschedule_running_timer(self) -> None:
        """Move a running timer's deadline to its start + the current delay."""
        target = self._timer_target
        if self._timer_cancel is None or target is None or self._timer_started is None:
            return
        deadline = self._timer_started + timedelta(seconds=self._delay_for(target))
        if deadline == self._timer_until:
            return
        self._timer_cancel()
        now = dt_util.utcnow()
        if deadline <= now:
            self._timer_cancel = None
            self._timer_target = None
            self._timer_until = None
            self._timer_started = None
            self._confirm(target)
            return
        self._timer_until = deadline
        self._timer_cancel = async_call_later(self._hass, (deadline - now).total_seconds(), self._timer_job)

    @callback
    def async_stop(self) -> None:
        """Stop watching the sources and cancel the timers.

        The confirmed status is kept as it is: stopping (unload, reload)
        never implies ``INACTIVE``.
        """
        self._cancel_timer()
        self._unsubscribe()
        self._started = False
        _LOGGER.info("%s - stopped", self)

    @callback
    def add_update_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a callback invoked on any diagnostic change."""
        self._update_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)

        return _remove

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    @callback
    def set_bypass(self, bypass: bool) -> None:
        """Turn the bypass on or off (from the bypass switch)."""
        bypass = bool(bypass)
        if bypass == self._bypass:
            return
        self._bypass = bypass
        _LOGGER.info("%s - bypass %s", self, "on" if bypass else "off")
        if self._started:
            self._evaluate()
        else:
            self._publish()

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle a state change of a source."""
        self._evaluate()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def _read_sources(self) -> list[str]:
        contributions: list[str] = []
        missing: list[str] = []
        registry = er.async_get(self._hass)
        for entity_id in self._config.source_entities:
            state = self._hass.states.get(entity_id)
            contribution = source_contribution(state, self._config.climate_hvac_modes)
            self._sources[entity_id] = SourceState(entity_id, state.state if state else None, contribution)
            contributions.append(contribution)
            if state is None and registry.async_get(entity_id) is None:
                missing.append(entity_id)
        self._missing_sources = tuple(missing)
        return contributions

    @callback
    def _evaluate(self) -> None:
        """Recompute the raw status and drive the confirmation timers.

        Rules:

        1. Bypass on: confirmed ``INACTIVE`` immediately, timers cancelled.
        2. First evaluation with a known raw status (startup, reload, or
           after a restored status): confirmed immediately, without delay.
        3. Afterwards only a *change* of the raw status matters (attribute
           updates and repeated states are ignored, so a running timer is not
           restarted). Any raw change cancels the running timer; if the new
           raw status differs from the confirmed one, a timer is started:
           activation delay for ``ACTIVE``, deactivation delay for
           ``INACTIVE``, unavailable grace period for ``UNKNOWN``. A zero
           delay confirms immediately.
        4. The grace period is measured from the first unavailability of an
           unavailability episode, which only ends once the raw status is
           known again and confirmed (so a source flapping between
           unavailable and another value cannot hold a stale status forever).
        """
        contributions = self._read_sources()

        if self._bypass:
            self._cancel_timer()
            self._raw = InhibitionStatus.INACTIVE
            self._live = True
            self._confirm(InhibitionStatus.INACTIVE)
            return

        new_raw = aggregate(contributions)
        if new_raw is InhibitionStatus.UNKNOWN and self._unavailable_since is None:
            self._unavailable_since = dt_util.utcnow()

        if not self._live:
            if new_raw is not InhibitionStatus.UNKNOWN:
                self._cancel_timer()
                self._raw = new_raw
                self._live = True
                self._confirm(new_raw)
                return
            self._raw = new_raw
            if self._status is not InhibitionStatus.UNKNOWN and self._timer_cancel is None:
                # Restored status: hold it for at most the grace period.
                self._schedule(InhibitionStatus.UNKNOWN, self._config.unavailable_grace_period)
            self._publish()
            return

        if new_raw == self._raw:
            self._publish()
            return

        self._raw = new_raw
        self._cancel_timer()
        if new_raw == self._status:
            _LOGGER.debug("%s - raw status back to confirmed %s, timer cancelled", self, new_raw)
            self._end_episode_if_confirmed()
            self._publish()
            return

        self._schedule(new_raw, self._delay_for(new_raw))

    @callback
    def _end_episode_if_confirmed(self) -> None:
        if self._raw is not InhibitionStatus.UNKNOWN and self._raw == self._status:
            self._unavailable_since = None

    def _delay_for(self, status: InhibitionStatus) -> int:
        if status is InhibitionStatus.ACTIVE:
            return self._config.activation_delay
        if status is InhibitionStatus.INACTIVE:
            return self._config.deactivation_delay
        return self._config.unavailable_grace_period

    @callback
    def _schedule(self, target: InhibitionStatus, delay: int) -> None:
        now = dt_util.utcnow()
        # The grace period runs from the start of the unavailability episode.
        start = (self._unavailable_since or now) if target is InhibitionStatus.UNKNOWN else now
        deadline = start + timedelta(seconds=delay)
        if deadline <= now:
            self._confirm(target)
            return
        self._timer_target = target
        self._timer_started = start
        self._timer_until = deadline
        self._timer_cancel = async_call_later(self._hass, (deadline - now).total_seconds(), self._timer_job)
        _LOGGER.debug("%s - %s pending until %s", self, target, deadline)
        self._publish()

    @callback
    def _cancel_timer(self) -> None:
        if self._timer_cancel is not None:
            self._timer_cancel()
        self._timer_cancel = None
        self._timer_target = None
        self._timer_until = None
        self._timer_started = None

    @callback
    def _async_timer_fired(self, _now: datetime) -> None:
        target = self._timer_target
        self._timer_cancel = None
        self._timer_target = None
        self._timer_until = None
        self._timer_started = None
        if target is None or not self._started:
            return
        self._confirm(target)

    @callback
    def _confirm(self, new_status: InhibitionStatus) -> None:
        """Confirm a status and notify the listeners (link, entities)."""
        old_status = self._status
        self._status = new_status
        self._end_episode_if_confirmed()
        if new_status != old_status:
            self._last_change = dt_util.utcnow()
            _LOGGER.info("%s - confirmed status %s -> %s", self, old_status, new_status)
        self._publish()

    @callback
    def _publish(self) -> None:
        for listener in list(self._update_listeners):
            listener()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        """Return the diagnostic attributes (no secret, entity IDs only)."""
        return {
            "status": str(self._status),
            "raw_status": str(self._raw),
            "stop_underlyings": self._config.stop_underlyings,
            "bypass": self._bypass,
            "active_sources": self.active_sources,
            "pending_status": str(self._timer_target) if self._timer_target else None,
            "pending_until": self._timer_until.isoformat() if self._timer_until else None,
            "last_change": self._last_change.isoformat() if self._last_change else None,
            "unavailable_since": self._unavailable_since.isoformat() if self._unavailable_since else None,
            "missing_sources": list(self._missing_sources),
            "sources": {
                source.entity_id: {"state": source.state, "contribution": source.contribution}
                for source in self.source_states
            },
        }
