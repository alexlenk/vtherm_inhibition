"""Source detection, debounce, unavailable grace period and bypass.

One ``InhibitionDetector`` exists per plugin config entry (that is, per
VTherm). It lives as long as the config entry, independently of whether the
VTherm core has attached a provider yet, so that the diagnostic entities and
the debounce timers work from the first second.

Vocabulary used below:

* **raw status**: the instantaneous aggregate of the sources (and bypass),
  never published to the core;
* **confirmed status**: the status published to the providers (and therefore
  to the core) once the configured delays have elapsed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HassJob, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util
from vtherm_api import InhibitionStatus, InhibitionStopPolicy
from vtherm_api.log_collector import get_vtherm_logger

from .const import SOURCE_ACTIVE, SOURCE_DOMAIN_CLIMATE, SOURCE_INACTIVE, SOURCE_UNAVAILABLE
from .models import InhibitionConfig

if TYPE_CHECKING:
    from .provider import VThermInhibitionProvider

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
        self._restored_status: InhibitionStatus | None = None
        self._bypass = False
        self._started = False
        self._sources: dict[str, SourceState] = {}
        self._timer_cancel: CALLBACK_TYPE | None = None
        self._timer_target: InhibitionStatus | None = None
        self._timer_until: datetime | None = None
        self._timer_started: datetime | None = None
        self._timer_job = HassJob(self._async_timer_fired, f"{name} inhibition timer", cancel_on_shutdown=True)
        self._unsub_sources: CALLBACK_TYPE | None = None
        self._providers: list[VThermInhibitionProvider] = []
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
    def physical_stop_policy(self) -> InhibitionStopPolicy:
        """Return the effective per-VTherm physical stop policy."""
        return self._config.physical_stop_policy

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
    def providers(self) -> list[VThermInhibitionProvider]:
        """Return the providers currently attached to this detector."""
        return list(self._providers)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @callback
    def restore_status(self, status: str | None) -> None:
        """Restore the last confirmed status from before a restart or reload.

        Only used when the unavailable grace period is not zero: the restored
        status is then kept for at most the grace period while the sources
        are not available yet. Ignored once the detector has started.
        """
        if self._started or self._config.unavailable_grace_period <= 0:
            return
        if status in (InhibitionStatus.ACTIVE, InhibitionStatus.INACTIVE):
            self._restored_status = InhibitionStatus(status)
            self._status = self._restored_status
            _LOGGER.debug("%s - restored confirmed status %s", self, status)

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
            "%s - started with sources %s, policy %s",
            self,
            list(self._config.source_entities),
            self._config.physical_stop_policy,
        )
        self._evaluate()

    @callback
    def _subscribe(self) -> None:
        if self._unsub_sources is not None:
            self._unsub_sources()
            self._unsub_sources = None
        if self._config.source_entities:
            self._unsub_sources = async_track_state_change_event(
                self._hass, list(self._config.source_entities), self._async_source_changed
            )

    @callback
    def async_reconfigure(self, config: InhibitionConfig) -> None:
        """Apply new options in place, without stopping the attached providers.

        * The sources are re-subscribed and re-evaluated. A raw status change
          caused by the new sources or HVAC modes follows the normal rules
          (activation/deactivation delay, grace period); before the first
          confirmation the first known aggregate is still confirmed at once.
        * A timer still running for the same target keeps its start time; its
          deadline becomes start + new delay (confirmed at once if already
          elapsed).
        * The policy is read live by the providers: a policy-only change emits
          no transition; the core captures the policy at the next entry.
        """
        old_config = self._config
        if config == old_config:
            return
        self._config = config
        _LOGGER.info(
            "%s - reconfigured: sources %s, policy %s",
            self,
            list(config.source_entities),
            config.physical_stop_policy,
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
        """Stop listening; every attached provider reports ``UNKNOWN`` first."""
        for provider in list(self._providers):
            provider.stop_listening()
        self._providers.clear()
        self._cancel_timer()
        if self._unsub_sources is not None:
            self._unsub_sources()
            self._unsub_sources = None
        self._started = False
        self._status = InhibitionStatus.UNKNOWN
        self._raw = InhibitionStatus.UNKNOWN
        self._live = False
        _LOGGER.info("%s - stopped", self)
        self._publish()

    @callback
    def attach_provider(self, provider: VThermInhibitionProvider) -> None:
        """Attach a provider which receives the confirmed transitions."""
        if provider not in self._providers:
            self._providers.append(provider)
            self._publish()

    @callback
    def detach_provider(self, provider: VThermInhibitionProvider) -> None:
        """Detach a provider."""
        if provider in self._providers:
            self._providers.remove(provider)
            self._publish()

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
    def async_refresh(self) -> InhibitionStatus:
        """Re-read every source and return the confirmed status."""
        if self._started:
            self._evaluate()
        return self._status

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle a state change of a source."""
        self._evaluate()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def _read_sources(self) -> list[str]:
        contributions: list[str] = []
        for entity_id in self._config.source_entities:
            state = self._hass.states.get(entity_id)
            contribution = source_contribution(state, self._config.climate_hvac_modes)
            self._sources[entity_id] = SourceState(entity_id, state.state if state else None, contribution)
            contributions.append(contribution)
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
        """
        contributions = self._read_sources()

        if self._bypass:
            self._cancel_timer()
            self._raw = InhibitionStatus.INACTIVE
            self._live = True
            self._confirm(InhibitionStatus.INACTIVE)
            return

        new_raw = aggregate(contributions)

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
            self._publish()
            return

        self._schedule(new_raw, self._delay_for(new_raw))

    def _delay_for(self, status: InhibitionStatus) -> int:
        if status is InhibitionStatus.ACTIVE:
            return self._config.activation_delay
        if status is InhibitionStatus.INACTIVE:
            return self._config.deactivation_delay
        return self._config.unavailable_grace_period

    @callback
    def _schedule(self, target: InhibitionStatus, delay: int) -> None:
        if delay <= 0:
            self._confirm(target)
            return
        self._timer_target = target
        self._timer_started = dt_util.utcnow()
        self._timer_until = self._timer_started + timedelta(seconds=delay)
        self._timer_cancel = async_call_later(self._hass, delay, self._timer_job)
        _LOGGER.debug("%s - %s pending for %ss", self, target, delay)
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
        """Publish a confirmed status to the attached providers."""
        old_status = self._status
        self._status = new_status
        if new_status != old_status:
            self._last_change = dt_util.utcnow()
            _LOGGER.info("%s - confirmed status %s -> %s", self, old_status, new_status)
            for provider in list(self._providers):
                provider.on_confirmed_status(new_status)
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
            "physical_stop_policy": str(self._config.physical_stop_policy),
            "bypass": self._bypass,
            "pending_status": str(self._timer_target) if self._timer_target else None,
            "pending_until": self._timer_until.isoformat() if self._timer_until else None,
            "last_change": self._last_change.isoformat() if self._last_change else None,
            "sources": {
                source.entity_id: {"state": source.state, "contribution": source.contribution}
                for source in self.source_states
            },
            "climate_hvac_modes": sorted(self._config.climate_hvac_modes),
            "activation_delay": self._config.activation_delay,
            "deactivation_delay": self._config.deactivation_delay,
            "unavailable_grace_period": self._config.unavailable_grace_period,
            "vtherm_entity_id": self._config.vtherm_entity_id,
            "provider_attached": bool(self._providers),
        }
