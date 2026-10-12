"""VTherm inhibition plugin: let another device take over a VTherm.

One config entry per VTherm. Each entry owns an ``InhibitionDetector`` (source
tracking, delays, grace period, bypass) and is exposed to the Versatile
Thermostat core through a single inhibition provider factory registered in
``vtherm_api`` while at least one entry is loaded.
"""

from __future__ import annotations

import asyncio

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from vtherm_api.log_collector import get_vtherm_logger

from .const import CONF_SOURCE_ENTITIES, DATA_DETECTORS, DOMAIN, PLATFORMS, VT_DOMAIN
from .detector import InhibitionDetector
from .entity import vtherm_device
from .factory import async_register_factory, async_unregister_factory, core_supports_inhibition, get_core_api
from .models import InhibitionConfig

_LOGGER = get_vtherm_logger(__name__)

type InhibitionConfigEntry = ConfigEntry[InhibitionDetector]

ISSUE_CORE_WITHOUT_INHIBITION = "core_without_inhibition"
DOCUMENTATION_URL = "https://github.com/alexlenk/vtherm_inhibition#prerequisites"

#: Loop iterations given to the core's registration task before falling back
#: to a VTherm reload (today's core attaches synchronously, in the first one).
ATTACH_WAIT_ITERATIONS = 5


def _missing_sources_issue_id(entry_id: str) -> str:
    return f"missing_sources_{entry_id}"


def _check_core(hass: HomeAssistant) -> None:
    """Raise when Versatile Thermostat is not ready or lacks inhibition support."""
    api = get_core_api(hass)
    if api is None:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="core_not_ready")
    if not core_supports_inhibition(api):
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_CORE_WITHOUT_INHIBITION,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CORE_WITHOUT_INHIBITION,
            learn_more_url=DOCUMENTATION_URL,
        )
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key=ISSUE_CORE_WITHOUT_INHIBITION)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION)


def _vtherm_config_entry_id(hass: HomeAssistant, config: InhibitionConfig) -> str | None:
    """Return the config entry ID of the targeted VTherm, if it exists."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(CLIMATE_DOMAIN, VT_DOMAIN, config.vtherm_unique_id)
    if entity_id is not None:
        registry_entry = registry.async_get(entity_id)
        if registry_entry is not None and registry_entry.config_entry_id:
            return registry_entry.config_entry_id
    for vt_entry in hass.config_entries.async_entries(VT_DOMAIN):
        if vt_entry.unique_id and vt_entry.unique_id == config.vtherm_unique_id:
            return vt_entry.entry_id
    return None


async def async_reload_vtherm(hass: HomeAssistant, config: InhibitionConfig) -> None:
    """Reload the targeted VTherm so the core (re)discovers the provider.

    Only done while Home Assistant is running: at startup the core discovers
    the factory itself in ``async_startup`` (after ``homeassistant_started``).
    """
    if hass.state is not CoreState.running:
        return
    entry_id = _vtherm_config_entry_id(hass, config)
    if entry_id is None:
        _LOGGER.debug("No loaded VTherm found for unique_id %s", config.vtherm_unique_id)
        return
    vt_entry = hass.config_entries.async_get_entry(entry_id)
    if vt_entry is None or vt_entry.state is not ConfigEntryState.LOADED:
        return
    api = get_core_api(hass)
    if api is None or not core_supports_inhibition(api):
        return
    _LOGGER.info("Reloading VTherm %s to apply the inhibition provider", config.vtherm_entity_id)
    await hass.config_entries.async_reload(entry_id)
    # The core may recreate the VThermAPI instance on reload: make sure the
    # factory is still registered for later discoveries.
    if hass.data.get(DOMAIN, {}).get(DATA_DETECTORS):
        async_register_factory(hass)


@callback
def _async_sync_missing_sources_issue(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Show a repair issue while configured sources do not exist."""
    missing = entry.runtime_data.missing_sources
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
def _async_remove_fallback_device(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Remove the plugin's own device once the entities live on the VTherm device."""
    if vtherm_device(hass, entry.runtime_data) is None:
        return
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    if device is not None:
        registry.async_remove_device(device.id)


async def async_setup_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> bool:
    """Set up the inhibition of one VTherm."""
    _check_core(hass)
    config = InhibitionConfig.from_entry(entry.data, entry.options)
    detector = InhibitionDetector(hass, config, entry.title)
    detector.rename_handler = lambda old, new: _async_rename_source(hass, entry, old, new)
    entry.runtime_data = detector

    # Entities restore the last confirmed status and the bypass before start.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_fallback_device(hass, entry)
    await detector.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    reported_missing: list[tuple[str, ...] | None] = [None]

    @callback
    def _missing_sources_changed() -> None:
        if detector.missing_sources != reported_missing[0]:
            reported_missing[0] = detector.missing_sources
            _async_sync_missing_sources_issue(hass, entry)

    entry.async_on_unload(detector.add_update_listener(_missing_sources_changed))
    _missing_sources_changed()

    # Expose the detector to the core only once it is started: a core notified
    # of the factory registration attaches the provider at once, and a detector
    # without a confirmed status yet would withdraw the VTherm (UNKNOWN, with a
    # precautionary stop) until its first evaluation.
    detectors: dict[str, InhibitionDetector] = hass.data.setdefault(DOMAIN, {}).setdefault(DATA_DETECTORS, {})
    detectors[config.vtherm_unique_id] = detector
    async_register_factory(hass)

    # A core notified of a new registration creates the provider from its
    # notification task (synchronously with today's core). Give that task a
    # few loop iterations; reload the VTherm only if no provider was created
    # for it (factory already registered for another entry, or a core that
    # does not attach on registration). Never both: no double attachment.
    for _ in range(ATTACH_WAIT_ITERATIONS):
        if detector.created_providers:
            return True
        await asyncio.sleep(0)
    if not detector.created_providers:
        await async_reload_vtherm(hass, config)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Apply changed options in place.

    The entry is deliberately not reloaded: an unload makes the provider report
    UNKNOWN (contract), and the core would then withdraw a VTherm that runs
    normally and send it a precautionary stop. The running detector is
    reconfigured instead; the attached provider stays attached and only a real
    change of the confirmed status produces a transition.
    """
    detector = entry.runtime_data
    config = InhibitionConfig.from_entry(entry.data, entry.options)
    if config.vtherm_unique_id != detector.config.vtherm_unique_id:
        # The target VTherm cannot change through the options; reload to be safe.
        await hass.config_entries.async_reload(entry.entry_id)
        return
    detector.async_reconfigure(config)


async def async_unload_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> bool:
    """Unload: attached providers report UNKNOWN, then detach.

    The VTherm keeps its safe withdrawal until a provider confirms INACTIVE
    again (on reload, the factory registration or ``async_setup_entry``
    re-attaches a provider). Option changes do not unload. When the
    entry is disabled or removed, the VTherm is reloaded without provider.
    """
    detector = entry.runtime_data
    # Entities first, so they keep (and save for restore) the last confirmed
    # status instead of the UNKNOWN caused by the stop.
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # Await UNKNOWN delivery to the core before the factory is unregistered.
    await detector.async_stop()

    data = hass.data.get(DOMAIN, {})
    detectors: dict[str, InhibitionDetector] = data.get(DATA_DETECTORS, {})
    if detectors.get(detector.config.vtherm_unique_id) is detector:
        detectors.pop(detector.config.vtherm_unique_id)
    if not detectors:
        async_unregister_factory(hass)
    ir.async_delete_issue(hass, DOMAIN, _missing_sources_issue_id(entry.entry_id))

    if entry.disabled_by is not None:
        # Not eager: the VTherm reload must start after this unload has returned.
        hass.async_create_task(
            async_reload_vtherm(hass, detector.config), f"{DOMAIN} release VTherm", eager_start=False
        )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Reload the VTherm without provider once its entry is deleted."""
    await async_reload_vtherm(hass, InhibitionConfig.from_entry(entry.data, entry.options))
