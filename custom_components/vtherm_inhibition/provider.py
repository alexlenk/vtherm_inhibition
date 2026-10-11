"""``InterfaceInhibitionProvider`` implementation bound to one VTherm."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util
from vtherm_api import (
    InhibitionStatus,
    InhibitionStopPolicy,
    InhibitionTransition,
    InhibitionTransitionCallback,
)
from vtherm_api.log_collector import get_vtherm_logger

from .const import PROVIDER_NAME
from .detector import InhibitionDetector

_LOGGER = get_vtherm_logger(__name__)


class VThermInhibitionProvider:
    """Thin adapter publishing the detector's confirmed status to the core.

    * ``status`` is the detector's confirmed status, or ``UNKNOWN`` once
      ``stop_listening`` has been called.
    * Each confirmed change produces one ``InhibitionTransition`` with a
      ``sequence`` that is strictly increasing per provider instance.
      Repeated statuses produce nothing.
    * Callbacks are awaited one after the other, in registration order,
      from a task started eagerly: ``async_refresh_state`` never awaits a
      callback itself, so a core holding its own lock while refreshing
      cannot deadlock. A failing callback is logged and does not prevent
      the others nor later transitions.
    """

    def __init__(self, detector: InhibitionDetector, thermostat: Any) -> None:
        self._detector = detector
        self._hass = detector.hass
        self._thermostat_name = str(getattr(thermostat, "name", None) or detector.config.vtherm_entity_id)
        self._callbacks: list[InhibitionTransitionCallback] = []
        self._sequence = 0
        self._listening = False
        self._stopped = False
        self._last_reported = detector.status
        self._dispatch_lock = asyncio.Lock()

    def __str__(self) -> str:
        return f"{PROVIDER_NAME}({self._thermostat_name})"

    # -- contract -----------------------------------------------------------
    @property
    def name(self) -> str:
        """Return the stable provider name (the factory name)."""
        return PROVIDER_NAME

    @property
    def status(self) -> InhibitionStatus:
        """Return the last confirmed status."""
        if self._stopped:
            return InhibitionStatus.UNKNOWN
        return self._detector.status

    @property
    def physical_stop_policy(self) -> InhibitionStopPolicy:
        """Return the effective policy; ``TURN_OFF`` when unset or invalid."""
        return self._detector.physical_stop_policy

    @property
    def sequence(self) -> int:
        """Return the sequence of the last emitted transition."""
        return self._sequence

    @property
    def is_listening(self) -> bool:
        """Return True while attached to the detector."""
        return self._listening

    def post_init(self, entry_infos: Any) -> None:
        """Nothing to read from the VTherm configuration: the plugin entry owns it."""
        del entry_infos

    async def async_start_listening(self) -> None:
        """Attach to the detector (which already listens to the sources)."""
        if self._listening:
            return
        self._stopped = False
        self._listening = True
        self._detector.attach_provider(self)
        _LOGGER.debug("%s - listening, status %s", self, self._detector.status)
        # Report a change that happened while detached (e.g. after a stop).
        self.on_confirmed_status(self._detector.status)

    @callback
    def stop_listening(self) -> None:
        """Report ``UNKNOWN`` to the registered callbacks, then detach them."""
        self._detector.detach_provider(self)
        self._listening = False
        self._stopped = True
        callbacks = list(self._callbacks)
        self._callbacks.clear()
        if callbacks:
            transition = self._next_transition(InhibitionStatus.UNKNOWN)
            self._hass.async_create_task(
                self._async_dispatch(transition, callbacks),
                f"{PROVIDER_NAME} stop",
                eager_start=True,
            )
        self._last_reported = InhibitionStatus.UNKNOWN
        _LOGGER.debug("%s - stopped listening", self)

    def restore_state(self, old_state: Any) -> None:
        """No-op: the core persists its withdrawal state; the plugin restores
        its last confirmed status through its own diagnostic entity."""
        del old_state

    async def async_refresh_state(self) -> InhibitionStatus:
        """Re-evaluate the sources immediately and return the confirmed status."""
        if self._stopped:
            return InhibitionStatus.UNKNOWN
        self._detector.async_refresh()
        return self.status

    def register_transition_callback(self, callback_: InhibitionTransitionCallback) -> CALLBACK_TYPE:
        """Register a coroutine called on each confirmed transition."""
        self._callbacks.append(callback_)

        @callback
        def _remove() -> None:
            if callback_ in self._callbacks:
                self._callbacks.remove(callback_)

        return _remove

    # -- detector side ----------------------------------------------------------
    @callback
    def on_confirmed_status(self, new_status: InhibitionStatus) -> None:
        """Called by the detector for each confirmed status change."""
        if not self._listening or new_status == self._last_reported:
            return
        transition = self._next_transition(new_status)
        self._hass.async_create_task(
            self._async_dispatch(transition, None),
            f"{PROVIDER_NAME} transition",
            eager_start=True,
        )

    def _next_transition(self, new_status: InhibitionStatus) -> InhibitionTransition:
        self._sequence += 1
        transition = InhibitionTransition(
            provider_name=PROVIDER_NAME,
            new_status=new_status,
            old_status=self._last_reported,
            stop_policy=self.physical_stop_policy,
            timestamp=dt_util.utcnow(),
            sequence=self._sequence,
        )
        self._last_reported = new_status
        return transition

    async def _async_dispatch(
        self,
        transition: InhibitionTransition,
        callbacks: list[InhibitionTransitionCallback] | None,
    ) -> None:
        """Await every callback in order; a failure never breaks the provider."""
        async with self._dispatch_lock:
            targets = list(self._callbacks) if callbacks is None else callbacks
            _LOGGER.info(
                "%s - transition %s -> %s (sequence %s, policy %s)",
                self,
                transition.old_status,
                transition.new_status,
                transition.sequence,
                transition.stop_policy,
            )
            for target in targets:
                try:
                    await target(transition)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("%s - transition callback failed", self)
