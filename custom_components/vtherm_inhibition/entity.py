"""Base entity shared by the plugin entities of one VTherm."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, VT_DOMAIN

if TYPE_CHECKING:
    from . import InhibitionRuntime


def vtherm_device(hass: HomeAssistant, vtherm_unique_id: str) -> dr.DeviceEntry | None:
    """Return the device of the targeted VTherm, found from its stable unique_id."""
    registry = er.async_get(hass)
    climate_entity_id = registry.async_get_entity_id(CLIMATE_DOMAIN, VT_DOMAIN, vtherm_unique_id)
    climate_entry = registry.async_get(climate_entity_id) if climate_entity_id else None
    if climate_entry is None or not climate_entry.device_id:
        return None
    return dr.async_get(hass).async_get(climate_entry.device_id)


class InhibitionEntity(Entity):
    """Entity refreshed on every detector or link change.

    It is shown on the VTherm device when it exists (``device_entry``, the
    Home Assistant way to link a helper entity to another integration's
    device), otherwise on a device of its own named after the entry.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, runtime: InhibitionRuntime, entry_id: str, title: str, key: str) -> None:
        self._detector = runtime.detector
        self._link = runtime.link
        unique_id = self._detector.config.vtherm_unique_id
        self._attr_unique_id = f"{unique_id}_{key}"
        self._attr_translation_key = key
        device = vtherm_device(hass, unique_id)
        if device is not None:
            self.device_entry = device
        else:
            self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)}, name=title)

    async def async_added_to_hass(self) -> None:
        """Follow the detector and the link."""
        await super().async_added_to_hass()
        self.async_on_remove(self._detector.add_update_listener(self._async_updated))
        self.async_on_remove(self._link.add_update_listener(self._async_updated))

    @callback
    def _async_updated(self) -> None:
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()
