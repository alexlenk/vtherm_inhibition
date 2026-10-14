"""VTherm inhibition plugin: let another device take over a VTherm.

One config entry per VTherm. Each entry owns an ``InhibitionDetector``
(sources, delays, grace period, bypass: the business status) and a
``VThermLink`` that applies the confirmed status to the VTherm through the
``vtherm_api`` facade (``set_inhibition`` / ``unset_inhibition``, issue #2087).
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from vtherm_api.log_collector import get_vtherm_logger

from .const import CONF_SOURCE_ENTITIES, CONF_VTHERM_ENTITY_ID, DOMAIN, PLATFORMS
from .detector import InhibitionDetector
from .entity import vtherm_device
from .link import ISSUE_CORE_WITHOUT_INHIBITION, VThermLink, async_release_vtherm, release_issue_id
from .models import InhibitionConfig, migrate_v1_settings

_LOGGER = get_vtherm_logger(__name__)


@dataclass(slots=True)
class InhibitionRuntime:
    """Runtime objects of one entry."""

    detector: InhibitionDetector
    link: VThermLink


type InhibitionConfigEntry = ConfigEntry[InhibitionRuntime]


def _missing_sources_issue_id(entry_id: str) -> str:
    return f"missing_sources_{entry_id}"


@callback
def _async_sync_missing_sources_issue(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Show a repair issue while configured sources do not exist."""
    missing = entry.runtime_data.detector.missing_sources
    issue_id = _missing_sources_issue_id(entry.entry_id)
    if missing:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="missing_sources",
            translation_placeholders={"entry": entry.title, "sources": ", ".join(missing)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _async_rename_source(hass: HomeAssistant, entry: InhibitionConfigEntry, old: str, new: str) -> None:
    """Follow a renamed source entity in the entry options (applied in place)."""
    sources = list(entry.options.get(CONF_SOURCE_ENTITIES, entry.data.get(CONF_SOURCE_ENTITIES, [])))
    if old not in sources:
        return
    sources = list(dict.fromkeys(new if source == old else source for source in sources))
    _LOGGER.info("%s - source %s renamed to %s", entry.title, old, new)
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_SOURCE_ENTITIES: sources})


@callback
def _async_rename_vtherm(hass: HomeAssistant, entry: InhibitionConfigEntry, new: str) -> None:
    """Keep the stored entity ID of a renamed VTherm up to date (the unique ID is the reference)."""
    if new and entry.data.get(CONF_VTHERM_ENTITY_ID) != new:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_VTHERM_ENTITY_ID: new})


@callback
def _async_remove_fallback_device(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Remove the plugin's own device once the entities live on the VTherm device."""
    if vtherm_device(hass, entry.runtime_data.detector.config.vtherm_unique_id) is None:
        return
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    if device is not None:
        registry.async_remove_device(device.id)


async def async_setup_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> bool:
    """Set up the inhibition of one VTherm."""
    config = InhibitionConfig.from_entry(entry.data, entry.options)
    detector = InhibitionDetector(hass, config, entry.title)
    detector.rename_handler = lambda old, new: _async_rename_source(hass, entry, old, new)
    link = VThermLink(hass, detector, entry.title)
    link.rename_handler = lambda new: _async_rename_vtherm(hass, entry, new)
    entry.runtime_data = InhibitionRuntime(detector, link)
    # The VTherm is driven again: a failed release of a previous entry no longer matters.
    ir.async_delete_issue(hass, DOMAIN, release_issue_id(config.vtherm_unique_id, config.vtherm_entity_id))

    # Entities restore the last confirmed status and the bypass before start.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_fallback_device(hass, entry)
    await detector.async_start()
    # Re-assert the restored (or first evaluated) status once the VTherm is available.
    link.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    reported_missing: list[tuple[str, ...] | None] = [None]

    @callback
    def _missing_sources_changed() -> None:
        if detector.missing_sources != reported_missing[0]:
            reported_missing[0] = detector.missing_sources
            _async_sync_missing_sources_issue(hass, entry)

    entry.async_on_unload(detector.add_update_listener(_missing_sources_changed))
    _missing_sources_changed()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Apply changed options in place (no reload, no API call unless the status changes)."""
    detector = entry.runtime_data.detector
    config = InhibitionConfig.from_entry(entry.data, entry.options)
    if config.vtherm_unique_id != detector.config.vtherm_unique_id:
        # The target VTherm cannot change through the options; reload to be safe.
        await hass.config_entries.async_reload(entry.entry_id)
        return
    detector.async_reconfigure(config)


async def async_unload_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> bool:
    """Unload without any API call: unloading never implies ``INACTIVE``.

    An inhibited VTherm stays inhibited until the entry is set up again and
    confirms ``INACTIVE`` (reload, restart), or until the entry is removed.
    """
    # Entities first, so they save (for restore) the last confirmed status.
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = entry.runtime_data
    runtime.link.async_stop()
    runtime.detector.async_stop()
    ir.async_delete_issue(hass, DOMAIN, _missing_sources_issue_id(entry.entry_id))
    return True


async def async_remove_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Release the VTherm (``unset_inhibition``) once its entry is deleted."""
    config = InhibitionConfig.from_entry(entry.data, entry.options)
    await async_release_vtherm(hass, config.vtherm_unique_id, config.vtherm_entity_id, entry.title)
    if not any(other.entry_id != entry.entry_id for other in hass.config_entries.async_entries(DOMAIN)):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Version 1 -> 2: ``physical_stop_policy`` becomes the ``stop_underlyings`` boolean."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            data=migrate_v1_settings(entry.data),
            options=migrate_v1_settings(entry.options),
            version=2,
            minor_version=1,
        )
        _LOGGER.info("%s - configuration migrated to version 2", entry.title)
    return True
