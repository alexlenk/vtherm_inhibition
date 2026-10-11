"""Diagnostic binary sensor: on while the inhibition is confirmed active."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from vtherm_api import InhibitionStatus

from . import InhibitionConfigEntry
from .entity import InhibitionEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InhibitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the inhibition binary sensor of the VTherm."""
    async_add_entities([InhibitionBinarySensor(hass, entry.runtime_data, entry.entry_id, entry.title)])


class InhibitionBinarySensor(InhibitionEntity, BinarySensorEntity, RestoreEntity):
    """``on`` = confirmed ACTIVE, ``off`` = confirmed INACTIVE, ``unknown`` = UNKNOWN."""

    def __init__(self, hass, detector, entry_id, title) -> None:
        super().__init__(hass, detector, entry_id, title, "inhibition")

    @property
    def icon(self) -> str:
        """Return a status-dependent icon."""
        if self._detector.status is InhibitionStatus.ACTIVE:
            return "mdi:radiator-off"
        if self._detector.status is InhibitionStatus.INACTIVE:
            return "mdi:radiator"
        return "mdi:help-circle-outline"

    @property
    def is_on(self) -> bool | None:
        """Return the confirmed status as a boolean, None when UNKNOWN."""
        if self._detector.status is InhibitionStatus.ACTIVE:
            return True
        if self._detector.status is InhibitionStatus.INACTIVE:
            return False
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the detector diagnostics."""
        return self._detector.diagnostics()

    async def async_added_to_hass(self) -> None:
        """Restore the last confirmed status (used during the grace period)."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        restored = {
            STATE_ON: InhibitionStatus.ACTIVE,
            STATE_OFF: InhibitionStatus.INACTIVE,
        }.get(last_state.state)
        self._detector.restore_status(restored)
