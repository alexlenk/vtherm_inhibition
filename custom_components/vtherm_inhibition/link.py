"""Apply the confirmed status to the VTherm through the vtherm_api facade.

``VThermAPI.set_inhibition(vtherm_entity_id, stop_underlyings)`` is called for
a confirmed ``ACTIVE`` or ``UNKNOWN``, ``VThermAPI.unset_inhibition(...)`` only
for a confirmed ``INACTIVE``. The core keeps its own operational state (gate,
entry stop, exit reconciliation, persistence); everything else is decided
here:

* the VTherm is targeted by its stable unique ID, resolved to its current
  entity ID through the entity registry at every call (renames are followed;
  a unique ID missing from the registry means the VTherm is not available);
* a call is only made while the VTherm is *available* (its entity exists: it
  has a state that is not ``unavailable``) and has *started*: it restored its
  inhibition state at startup (``VThermAPI.is_inhibition_started``; the core
  refuses calls before with ``InhibitionNotStartedError``). It does not wait
  for the VTherm to be ready (every underlying reporting a state): the core
  defers the entry stop and the exit reconciliation until then. While waiting,
  the VTherm is checked at each of its state changes and every
  ``START_POLL_INTERVAL`` seconds;
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
  no new call: ``stop_underlyings`` is only read when an inhibition starts;
* the inhibition state the core publishes (``specific_states.inhibition``) is
  checked against the last successful call: an exit that the core deferred
  until the VTherm was ready and that failed afterwards (the VTherm is
  inhibited again), or an inhibition the VTherm lost, is retried like a
  failed call.

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
from vtherm_api import InhibitionNotStartedError, VThermAPI
from vtherm_api.const import DOMAIN as VTHERM_API_DOMAIN
from vtherm_api.const import VTHERM_API_NAME
from vtherm_api.log_collector import get_vtherm_logger

from .const import DOMAIN, RELEASE_START_TIMEOUT, RETRY_DELAYS, RETRY_INTERVAL, START_POLL_INTERVAL, VT_DOMAIN
from .detector import InhibitionDetector
from .models import ApiCall, call_for

_LOGGER = get_vtherm_logger(__name__)

ISSUE_CORE_WITHOUT_INHIBITION = "core_without_inhibition"
DOCUMENTATION_URL = "https://github.com/alexlenk/vtherm_inhibition#prerequisites"
RECOVERY_URL = "https://github.com/alexlenk/vtherm_inhibition#recovery-if-the-plugin-is-removed-while-a-vtherm-is-inhibited"

ISSUE_UNSET_FAILING = "unset_failing"
ISSUE_RELEASE_FAILED = "release_failed"

#: Message of the facade's TypeError for an entity without inhibition support.
_FACADE_UNSUPPORTED = "does not support inhibition"
_INHIBITION_METHODS = ("async_set_inhibition", "async_unset_inhibition")


class CoreWithoutInhibitionError(Exception):
    """The VTherm entity has no inhibition support (Versatile Thermostat too old)."""


def get_core_api(hass: HomeAssistant) -> VThermAPI | None:
    """Return the VTherm API instance created by the core, never creating one.

    ``VThermAPI.get_vtherm_api(hass)`` would create a bare instance when the
    core has none yet, which the core would then use instead of its own.
    """
    return hass.data.get(VTHERM_API_DOMAIN, {}).get(VTHERM_API_NAME)


def resolve_vtherm_entity_id(hass: HomeAssistant, unique_id: str, fallback_entity_id: str) -> str:
    """Return the current entity ID of the VTherm with this unique ID ("" if it is not registered).

    The stored entity ID is only used for an entry without unique ID: it could
    designate another (recreated) VTherm or a climate entity of another
    integration.
    """
    if unique_id:
        return er.async_get(hass).async_get_entity_id(CLIMATE_DOMAIN, VT_DOMAIN, unique_id) or ""
    return fallback_entity_id


def vtherm_available(state: State | None) -> bool:
    """True when the VTherm entity exists (its state is written as soon as it is added)."""
    return state is not None and state.state != STATE_UNAVAILABLE


def _find_vtherm_entity(hass: HomeAssistant, entity_id: str) -> Any:
    component = hass.data.get(CLIMATE_DOMAIN)
    for entity in getattr(component, "entities", ()):
        if getattr(entity, "entity_id", None) == entity_id:
            return entity
    return None


def vtherm_started(hass: HomeAssistant, entity_id: str) -> bool:
    """True once the VTherm accepts the API calls (``VThermAPI.is_inhibition_started``:
    it restored its inhibition state at startup). Without the core's API, or for a target
    the facade cannot resolve, it is reported started: the call then fails like the
    facade does and is retried."""
    api = get_core_api(hass)
    if api is None:
        return True
    try:
        return api.is_inhibition_started(entity_id)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        return True


def _is_not_started_error(err: BaseException) -> bool:
    return isinstance(err, InhibitionNotStartedError)


def _check_inhibition_support(hass: HomeAssistant, entity_id: str) -> None:
    """Raise CoreWithoutInhibitionError when the climate entity lacks the inhibition methods."""
    entity = _find_vtherm_entity(hass, entity_id)
    if entity is not None and not all(callable(getattr(entity, method, None)) for method in _INHIBITION_METHODS):
        raise CoreWithoutInhibitionError(f"VTherm entity '{entity_id}' does not support inhibition")


async def async_call_api(hass: HomeAssistant, call: ApiCall, entity_id: str, stop_underlyings: bool) -> None:
    """Make one facade call. Raises like the facade (LookupError, ...), and
    CoreWithoutInhibitionError only for a VTherm without inhibition support: a
    TypeError raised inside the core is an ordinary failure."""
    api = get_core_api(hass)
    if api is None:
        raise LookupError("Versatile Thermostat is not loaded")
    _check_inhibition_support(hass, entity_id)
    try:
        if call is ApiCall.SET:
            await api.set_inhibition(entity_id, stop_underlyings)
        else:
            await api.unset_inhibition(entity_id)
    except TypeError as err:
        if _FACADE_UNSUPPORTED in str(err):
            raise CoreWithoutInhibitionError(str(err)) from err
        raise


def core_inhibited(state: State | None) -> bool | None:
    """The inhibition state published by the core: True (inhibited, gate closed),
    False (not inhibited, gate open), None (not published or in transition)."""
    if state is None:
        return None
    snapshot = (state.attributes.get("specific_states") or {}).get("inhibition")
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("inhibited") is True and snapshot.get("command_gate") == "CLOSED":
        return True
    if snapshot.get("inhibited") is False and snapshot.get("command_gate") == "OPEN":
        return False
    return None


@callback
def _is_registry_create(event_data: er.EventEntityRegistryUpdatedData) -> bool:
    return event_data["action"] == "create"


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
        #: True while the call waits for the VTherm to restore its state at startup.
        self._waiting_start = False
        self._start_poll_cancel: CALLBACK_TYPE | None = None
        self._start_poll_job = HassJob(self._async_start_poll_fired, f"{name} VTherm start check", cancel_on_shutdown=True)
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
        self._stop_waiting_start()
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
        if not self._entity_id:
            # Not registered (yet): resolve again when an entity is registered.
            self._unsub_vtherm = [
                self._hass.bus.async_listen(
                    er.EVENT_ENTITY_REGISTRY_UPDATED,
                    self._async_registry_created,
                    event_filter=_is_registry_create,
                )
            ]
        else:
            self._unsub_vtherm = [
                async_track_state_change_event(self._hass, [self._entity_id], self._async_vtherm_state_changed),
                async_track_entity_registry_updated_event(
                    self._hass, [self._entity_id], self._async_vtherm_registry_updated
                ),
            ]
        self._update_availability(self._hass.states.get(self._entity_id) if self._entity_id else None)

    @callback
    def _async_registry_created(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        config = self._detector.config
        if self._started and resolve_vtherm_entity_id(self._hass, config.vtherm_unique_id, config.vtherm_entity_id):
            self._track_vtherm()
            self._publish()

    @callback
    def _async_vtherm_state_changed(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if vtherm_available(state) and self._available:
            self._check_core_state(state)
        self._update_availability(state)
        if self._waiting_start and self._available:
            self._kick()

    @callback
    def _check_core_state(self, state: State) -> None:
        """Retry when the core's inhibition no longer matches the last successful call."""
        if (
            not self._started
            or self._applied is None
            or self._wanted != self._applied
            or (self._task is not None and not self._task.done())
            or self._retry_cancel is not None
        ):
            return
        inhibited = core_inhibited(state)
        if inhibited is None or inhibited == (self._wanted is ApiCall.SET):
            return
        snapshot = state.attributes["specific_states"]["inhibition"]
        reason = snapshot.get("last_error_code") or ("inhibited" if inhibited else "not inhibited")
        self._last_error = f"Versatile Thermostat is {'inhibited' if inhibited else 'not inhibited'} ({reason})"
        _LOGGER.warning("%s - %s after %s_inhibition, retrying", self, self._last_error, self._wanted)
        self._applied = None
        self._schedule_retry()
        self._publish()

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
            self._stop_waiting_start()
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
            if not vtherm_started(self._hass, entity_id):
                self._wait_for_start()
                break
            self._stop_waiting_start()
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
                if _is_not_started_error(err):
                    # Defensive: the check above and the core's own check see the same entity
                    # with nothing awaited in between, so this only happens with a core that
                    # raises it without exposing is_inhibition_started. Wait for the startup.
                    self._wait_for_start()
                    break
                self._last_error = _format_error(err)
                self._async_handle_error(call, err)
                if self._wanted == call and generation == self._generation:
                    if not isinstance(err, CoreWithoutInhibitionError):
                        self._schedule_retry()
                    break
                # The wanted call changed, or the VTherm reappeared: call again at once.
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
    def _wait_for_start(self) -> None:
        """Check again at the next VTherm state change, or after START_POLL_INTERVAL."""
        if not self._started or not self._available:
            return
        if not self._waiting_start:
            _LOGGER.info("%s - waiting for %s to start before %s_inhibition", self, self._entity_id, self._wanted)
        self._waiting_start = True
        if self._start_poll_cancel is None:
            self._start_poll_cancel = async_call_later(self._hass, START_POLL_INTERVAL, self._start_poll_job)

    @callback
    def _stop_waiting_start(self) -> None:
        self._waiting_start = False
        if self._start_poll_cancel is not None:
            self._start_poll_cancel()
        self._start_poll_cancel = None

    @callback
    def _async_start_poll_fired(self, _now: datetime) -> None:
        self._start_poll_cancel = None
        if self._waiting_start:
            self._kick()

    @callback
    def _async_handle_error(self, call: ApiCall, err: Exception) -> None:
        if isinstance(err, CoreWithoutInhibitionError):
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
    def _schedule_retry(self) -> None:
        """Retry with a bounded backoff while the VTherm is available (a core without
        inhibition support is not retried: it needs an update and a restart, after
        which the VTherm reappears)."""
        if not self._started or not self._available:
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
            "waiting_for_vtherm_start": self._waiting_start,
            "next_retry_at": self._next_retry_at.isoformat() if self._next_retry_at else None,
        }


def release_issue_id(unique_id: str, entity_id: str) -> str:
    """ID of the repair issue raised when a VTherm could not be released on removal."""
    return f"{ISSUE_RELEASE_FAILED}_{unique_id or entity_id}"


async def async_release_vtherm(
    hass: HomeAssistant, unique_id: str, fallback_entity_id: str, name: str, wait_for_start: bool = True
) -> None:
    """Call ``unset_inhibition`` once (entry removal).

    If the VTherm has not restored its state yet (Home Assistant or VTherm
    starting), the call is made once it has, checked every START_POLL_INTERVAL
    for at most RELEASE_START_TIMEOUT; the removal does not wait for it.
    A failure raises a persistent repair issue (the entry is gone, nothing will
    retry): the VTherm may stay inhibited. It is removed when an entry for this
    VTherm is set up again.
    """
    entity_id = resolve_vtherm_entity_id(hass, unique_id, fallback_entity_id)
    if not entity_id:
        _LOGGER.info("VThermInhibition-%s - the VTherm does not exist any more, nothing to release", name)
        return
    if wait_for_start and not vtherm_started(hass, entity_id):
        _LOGGER.info("VThermInhibition-%s - %s has not started yet, released once it has", name, entity_id)
        _schedule_release_when_started(hass, unique_id, fallback_entity_id, name)
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
        ir.async_create_issue(
            hass,
            DOMAIN,
            release_issue_id(unique_id, entity_id),
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RELEASE_FAILED,
            translation_placeholders={"vtherm": entity_id, "error": _format_error(err)},
            learn_more_url=RECOVERY_URL,
        )
    else:
        _LOGGER.info("VThermInhibition-%s - %s released (entry removed)", name, entity_id)


@callback
def _schedule_release_when_started(hass: HomeAssistant, unique_id: str, fallback_entity_id: str, name: str) -> None:
    """Release the VTherm once it has started, or after RELEASE_START_TIMEOUT anyway."""
    checks_left = RELEASE_START_TIMEOUT // START_POLL_INTERVAL

    @callback
    def _check(_now: datetime) -> None:
        nonlocal checks_left
        checks_left -= 1
        entity_id = resolve_vtherm_entity_id(hass, unique_id, fallback_entity_id)
        if entity_id and checks_left > 0 and not vtherm_started(hass, entity_id):
            async_call_later(hass, START_POLL_INTERVAL, _check)
            return
        hass.async_create_task(
            async_release_vtherm(hass, unique_id, fallback_entity_id, name, wait_for_start=False),
            f"VThermInhibition-{name} release",
        )

    async_call_later(hass, START_POLL_INTERVAL, _check)
