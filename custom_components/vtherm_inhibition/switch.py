"""Bypass switch: while on, the provider reports a confirmed INACTIVE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import InhibitionConfigEntry
from .entity import InhibitionEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InhibitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the bypass switch of the VTherm."""
    async_add_entities([InhibitionBypassSwitch(hass, entry.runtime_data, entry.entry_id, entry.title)])


class InhibitionBypassSwitch(InhibitionEntity, SwitchEntity, RestoreEntity):
    """Ignore every source while on (state restored after a restart)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:debug-step-over"

    def __init__(self, hass, detector, entry_id, title) -> None:
        super().__init__(hass, detector, entry_id, title, "inhibition_bypass")

    @property
    def is_on(self) -> bool:
        """Return True when the bypass is on."""
        return self._detector.bypass

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the bypass on."""
        self._detector.set_bypass(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the bypass off."""
        self._detector.set_bypass(False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore the bypass before the detector starts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._detector.restore_bypass(last_state.state == STATE_ON)
