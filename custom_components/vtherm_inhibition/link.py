"""Apply the confirmed status to the VTherm through the vtherm_api facade.

``VThermAPI.set_inhibition(vtherm_entity_id, stop_underlyings)`` is called for
a confirmed ``ACTIVE`` or ``UNKNOWN``, ``VThermAPI.unset_inhibition(...)`` only
for a confirmed ``INACTIVE``. The core keeps its own operational state (gate,
entry stop, exit reconciliation, persistence); everything else is decided
here:

* the VTherm is targeted by its stable unique ID, resolved to its current
  entity ID through the entity registry at every call (renames are followed);
* a call is only made while the VTherm is *available*: its entity exists (it
  has a state that is not ``unavailable``). The VTherm does not need to have
  started: the core merges an early ``set`` with the inhibition state it
  restores at startup (no second entry stop) and closes its command gate at
  once (no startup command). The pending call is made as soon as the VTherm
  becomes available;
* each time the VTherm becomes available (Home Assistant start, VTherm
  reload) the current call is made again: ``set`` is idempotent in the core and
  ``unset`` is a no-op outside an inhibition;
* a failed call (for example an exit reconciliation that failed in the core,
  which leaves the VTherm inhibited) is retried after 30 s, 60 s, 120 s, then
  every 5 minutes, as long as the confirmed status asks for the same call. A
  status change cancels the retry and makes the new call. An ``unset`` still
  failing after the 3rd retry raises a repair issue, removed on success or
  status change;
* repeated statuses asking for the same call (``ACTIVE`` <-> ``UNKNOWN``) make
  no new call: ``stop_underlyings`` is only read when an inhibition starts.

Stopping the link (entry unload or reload) makes no call: unloading never
implies ``INACTIVE``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HassJob, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util
from vtherm_api import VThermAPI
from vtherm_api.const import DOMAIN as VTHERM_API_DOMAIN
from vtherm_api.const import VTHERM_API_NAME
from vtherm_api.log_collector import get_vtherm_logger

from .const import DOMAIN, RETRY_DELAYS, RETRY_INTERVAL, VT_DOMAIN
from .detector import InhibitionDetector
from .models import ApiCall, call_for

_LOGGER = get_vtherm_logger(__name__)

ISSUE_CORE_WITHOUT_INHIBITION = "core_without_inhibition"
DOCUMENTATION_URL = "https://github.com/alexlenk/vtherm_inhibition#prerequisites"

ISSUE_UNSET_FAILING = "unset_failing"


def get_core_api(hass: HomeAssistant) -> VThermAPI | None:
    """Return the VTherm API instance created by the core, never creating one.

    ``VThermAPI.get_vtherm_api(hass)`` would create a bare instance when the
    core has none yet, which the core would then use instead of its own.
    """
    return hass.data.get(VTHERM_API_DOMAIN, {}).get(VTHERM_API_NAME)


def resolve_vtherm_entity_id(hass: HomeAssistant, unique_id: str, fallback_entity_id: str) -> str:
    """Return the current entity ID of the VTherm with this unique ID."""
    if unique_id:
        entity_id = er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, VT_DOMAIN, unique_id)
        if entity_id is not None:
            return entity_id
    return fallback_entity_id


def vtherm_available(state: State | None) -> bool:
    """True when the VTherm entity exists (its state is written as soon as it is added)."""
    return state is not None and state.state != STATE_UNAVAILABLE


async def async_call_api(hass: HomeAssistant, call: ApiCall, entity_id: str, stop_underlyings: bool) -> None:
    """Make one facade call. Raises like the facade (LookupError, TypeError, ...)."""
    api = get_core_api(hass)
    if api is None:
        raise LookupError("Versatile Thermostat is not loaded")
    if call is ApiCall.SET:
        await api.set_inhibition(entity_id, stop_underlyings)
    else:
        await api.unset_inhibition(entity_id)


def _format_error(err: BaseException) -> str:
    return f"{type(err).__name__}: {err}" if str(err) else type(err).__name__


class VThermLink:
    """Drive one VTherm from the confirmed status of its detector."""

    def __init__(self, hass: HomeAssistant, detector: InhibitionDetector, name: str) -> None:
        self._hass = hass
        self._detector = detector
        self._name = name
        self._started = False
        #: Call asked by the confirmed status.
        self._wanted: ApiCall | None = None
        #: Last call that succeeded (None: not known to be applied, e.g. after a
        #: failure, at startup or when the VTherm (re)appeared).
        self._applied: ApiCall | None = None
        #: Incremented each time the VTherm becomes available (a new VTherm instance).
        self._generation = 0
        self._entity_id = ""
        self._available = False
        self._task: asyncio.Task | None = None
        self._retry_cancel: CALLBACK_TYPE | None = None
        self._retry_count = 0
        self._next_retry_at: datetime | None = None
        self._retry_job = HassJob(self._async_retry_fired, f"{name} inhibition retry", cancel_on_shutdown=True)
        self._last_call: ApiCall | None = None
        self._last_call_at: datetime | None = None
        self._last_error: str | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsub_vtherm: list[CALLBACK_TYPE] = []
        self._update_listeners: list[Callable[[], None]] = []
        #: Called with the new entity ID when the VTherm is renamed.
        self.rename_handler: Callable[[str], None] | None = None

    def __str__(self) -> str:
        return f"VThermInhibition-{self._name}"

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------
    @property
    def vtherm_entity_id(self) -> str:
        """Return the current entity ID of the VTherm."""
        return self._entity_id

    @property
    def vtherm_available(self) -> bool:
        """Return True while the VTherm entity exists."""
        return self._available

    @property
    def call_pending(self) -> bool:
        """True while the wanted call has not succeeded yet (VTherm not available, retry)."""
        return self._started and self._wanted is not None and self._wanted != self._applied

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @callback
    def async_start(self) -> None:
        """Follow the detector and the VTherm, and make the current call."""
        if self._started:
            return
        self._started = True
        self._wanted = call_for(self._detector.status)
        self._unsubs.append(self._detector.add_update_listener(self._async_detector_updated))
        # Makes the call if the VTherm is already available.
        self._track_vtherm()
        self._publish()

    @callback
    def async_stop(self) -> None:
        """Stop following; never calls ``unset`` (a call in progress completes)."""
        self._started = False
        self._cancel_retry()
        self._clear_unset_issue()
        for unsub in (*self._unsubs, *self._unsub_vtherm):
            unsub()
        self._unsubs.clear()
        self._unsub_vtherm.clear()

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
    # VTherm tracking
    # ------------------------------------------------------------------
    @callback
    def _track_vtherm(self) -> None:
        for unsub in self._unsub_vtherm:
            unsub()
        config = self._detector.config
        self._entity_id = resolve_vtherm_entity_id(self._hass, config.vtherm_unique_id, config.vtherm_entity_id)
        self._unsub_vtherm = []
        if self._entity_id:
            self._unsub_vtherm = [
                async_track_state_change_event(self._hass, [self._entity_id], self._async_vtherm_state_changed),
                async_track_entity_registry_updated_event(
                    self._hass, [self._entity_id], self._async_vtherm_registry_updated
                ),
            ]
        self._update_availability(self._hass.states.get(self._entity_id) if self._entity_id else None)

    @callback
    def _async_vtherm_state_changed(self, event: Event[EventStateChangedData]) -> None:
        self._update_availability(event.data["new_state"])

    @callback
    def _async_vtherm_registry_updated(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Follow a renamed VTherm."""
        data = event.data
        if data["action"] != "update" or "old_entity_id" not in data or not self._started:
            return
        _LOGGER.info("%s - VTherm renamed from %s to %s", self, data["old_entity_id"], data["entity_id"])
        self._track_vtherm()
        if self.rename_handler is not None:
            self.rename_handler(self._entity_id)
        self._publish()

    @callback
    def _update_availability(self, state: State | None) -> None:
        available = vtherm_available(state)
        if available == self._available:
            return
        self._available = available
        if not self._started:
            return
        if available:
            # A (re)started VTherm: make the wanted call again, whatever was applied before.
            _LOGGER.info("%s - VTherm %s available, applying %s", self, self._entity_id, self._wanted)
            self._applied = None
            self._generation += 1
            self._retry_count = 0
            self._cancel_retry()
            self._kick()
        else:
            _LOGGER.info("%s - VTherm %s not available", self, self._entity_id)
            self._cancel_retry()
        self._publish()

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    @callback
    def _async_detector_updated(self) -> None:
        wanted = call_for(self._detector.status)
        if wanted == self._wanted:
            return
        _LOGGER.debug("%s - status %s asks for %s", self, self._detector.status, wanted)
        self._wanted = wanted
        self._retry_count = 0
        self._cancel_retry()
        self._clear_unset_issue()
        self._kick()
        self._publish()

    @callback
    def _kick(self) -> None:
        """Start the call task unless it is already running (it picks up the new wanted call)."""
        if self._task is not None and not self._task.done():
            return
        if not self._needs_call():
            return
        self._task = self._hass.async_create_task(self._async_run(), f"{self} API call", eager_start=True)

    def _needs_call(self) -> bool:
        return self._started and self._available and self._wanted is not None and self._wanted != self._applied

    async def _async_run(self) -> None:
        """Make the wanted call until it is applied, it fails or the link stops."""
        while self._needs_call():
            call = self._wanted
            assert call is not None
            entity_id = self._entity_id
            generation = self._generation
            stop_underlyings = self._detector.config.stop_underlyings
            self._last_call = call
            self._last_call_at = dt_util.utcnow()
            self._publish()
            _LOGGER.info("%s - %s_inhibition(%s%s)", self, call, entity_id, f", {stop_underlyings}" if call is ApiCall.SET else "")
            try:
                await async_call_api(self._hass, call, entity_id, stop_underlyings)
            except Exception as err:  # noqa: BLE001  # pylint: disable=broad-except
                # The real state of the VTherm is not known any more: the next
                # wanted call is always made, even if it equals the last applied one.
                self._applied = None
                self._last_error = _format_error(err)
                self._async_handle_error(call, err)
                if self._wanted == call:
                    self._schedule_retry(err)
                    break
            else:
                # Not applied to a VTherm that reappeared during the call.
                self._applied = call if generation == self._generation else None
                self._last_error = None
                self._retry_count = 0
                ir.async_delete_issue(self._hass, DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION)
                self._clear_unset_issue()
            self._publish()
        self._publish()

    @callback
    def _async_handle_error(self, call: ApiCall, err: Exception) -> None:
        if isinstance(err, TypeError):
            _LOGGER.error(
                "%s - %s_inhibition refused, the installed Versatile Thermostat has no inhibition support: %s",
                self,
                call,
                err,
            )
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                ISSUE_CORE_WITHOUT_INHIBITION,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_CORE_WITHOUT_INHIBITION,
                learn_more_url=DOCUMENTATION_URL,
            )
        else:
            _LOGGER.warning("%s - %s_inhibition failed for %s: %s", self, call, self._entity_id, _format_error(err))

    @callback
    def _schedule_retry(self, err: Exception) -> None:
        """Retry with a bounded backoff (not for a core without inhibition support:
        that needs an update and a restart, after which the VTherm reappears)."""
        if isinstance(err, TypeError) or not self._started or not self._available:
            return
        if self._wanted is ApiCall.UNSET and self._retry_count >= len(RETRY_DELAYS):
            self._raise_unset_issue()
        delay = RETRY_DELAYS[self._retry_count] if self._retry_count < len(RETRY_DELAYS) else RETRY_INTERVAL
        self._retry_count += 1
        self._next_retry_at = dt_util.utcnow() + timedelta(seconds=delay)
        self._retry_cancel = async_call_later(self._hass, delay, self._retry_job)
        _LOGGER.info("%s - %s retried in %s s (attempt %s)", self, self._wanted, delay, self._retry_count + 1)

    @property
    def _unset_issue_id(self) -> str:
        return f"{ISSUE_UNSET_FAILING}_{self._detector.config.vtherm_unique_id or self._name}"

    @callback
    def _raise_unset_issue(self) -> None:
        """The VTherm stays inhibited although the plugin confirmed INACTIVE."""
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._unset_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_UNSET_FAILING,
            translation_placeholders={
                "entry": self._name,
                "vtherm": self._entity_id,
                "error": self._last_error or "",
            },
        )

    @callback
    def _clear_unset_issue(self) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, self._unset_issue_id)

    @callback
    def _cancel_retry(self) -> None:
        if self._retry_cancel is not None:
            self._retry_cancel()
        self._retry_cancel = None
        self._next_retry_at = None

    @callback
    def _async_retry_fired(self, _now: datetime) -> None:
        self._retry_cancel = None
        self._next_retry_at = None
        self._kick()
        self._publish()

    @callback
    def _publish(self) -> None:
        for listener in list(self._update_listeners):
            listener()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        """Return the diagnostic attributes of the API calls."""
        return {
            "vtherm_entity_id": self._entity_id,
            "vtherm_available": self._available,
            "last_api_call": str(self._last_call) if self._last_call else None,
            "last_api_call_at": self._last_call_at.isoformat() if self._last_call_at else None,
            "last_error": self._last_error,
            "retry_pending": self.call_pending,
            "next_retry_at": self._next_retry_at.isoformat() if self._next_retry_at else None,
        }


async def async_release_vtherm(hass: HomeAssistant, unique_id: str, fallback_entity_id: str, name: str) -> None:
    """Call ``unset_inhibition`` once (entry removal); failures are only logged."""
    entity_id = resolve_vtherm_entity_id(hass, unique_id, fallback_entity_id)
    if not entity_id:
        return
    try:
        await async_call_api(hass, ApiCall.UNSET, entity_id, False)
    except Exception as err:  # noqa: BLE001  # pylint: disable=broad-except
        _LOGGER.warning(
            "VThermInhibition-%s - could not release %s on removal (%s). If it stays inhibited, add an entry "
            "for it again, then delete it once Versatile Thermostat is running",
            name,
            entity_id,
            _format_error(err),
        )
    else:
        _LOGGER.info("VThermInhibition-%s - %s released (entry removed)", name, entity_id)
