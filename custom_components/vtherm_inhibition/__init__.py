"""VTherm inhibition plugin: let another device take over a VTherm.

One config entry per VTherm. Each entry owns an ``InhibitionDetector`` (source
tracking, delays, grace period, bypass) and is exposed to the Versatile
Thermostat core through a single inhibition provider factory registered in
``vtherm_api`` while at least one entry is loaded.
"""

from __future__ import annotations

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from vtherm_api import VThermAPI
from vtherm_api.log_collector import get_vtherm_logger

from .const import DATA_DETECTORS, DATA_FACTORY, DOMAIN, PLATFORMS, PROVIDER_NAME, VT_DOMAIN
from .detector import InhibitionDetector
from .factory import async_register_factory, async_unregister_factory
from .models import InhibitionConfig

_LOGGER = get_vtherm_logger(__name__)

type InhibitionConfigEntry = ConfigEntry[InhibitionDetector]

_BASE_REGISTER = VThermAPI.register_inhibition_provider_factory


def _core_attaches_on_registration(api: VThermAPI) -> bool:
    """True when the API in use is a core that attaches providers on registration.

    The Versatile Thermostat core overrides ``register_inhibition_provider_factory``
    to notify the started VTherms (late plugin); the bare ``vtherm_api`` does not.
    """
    return getattr(type(api), "register_inhibition_provider_factory", None) is not _BASE_REGISTER


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
    _LOGGER.info("Reloading VTherm %s to apply the inhibition provider", config.vtherm_entity_id)
    await hass.config_entries.async_reload(entry_id)
    # The core may recreate the VThermAPI instance on reload: make sure the
    # factory is still registered for later discoveries.
    if hass.data.get(DOMAIN, {}).get(DATA_DETECTORS):
        async_register_factory(hass)


async def async_setup_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> bool:
    """Set up the inhibition of one VTherm."""
    config = InhibitionConfig.from_entry(entry.data, entry.options)
    detector = InhibitionDetector(hass, config, entry.title)
    entry.runtime_data = detector

    # Entities restore the last confirmed status and the bypass before start.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await detector.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Expose the detector to the core only once it is started: a core notified
    # of the factory registration attaches the provider at once, and a detector
    # without a confirmed status yet would withdraw the VTherm (UNKNOWN, with a
    # precautionary stop) until its first evaluation.
    detectors: dict[str, InhibitionDetector] = hass.data.setdefault(DOMAIN, {}).setdefault(DATA_DETECTORS, {})
    detectors[config.vtherm_unique_id] = detector
    api = VThermAPI.get_vtherm_api(hass)
    before = api.get_inhibition_provider_factory(PROVIDER_NAME) if api is not None else None
    registered = async_register_factory(hass)
    newly_registered = registered and before is not hass.data[DOMAIN].get(DATA_FACTORY)

    # A core notified of a new registration attaches the provider itself (its
    # notification task may not have run yet): a reload would attach it a
    # second time. Otherwise (factory already registered for another entry,
    # or no notifying core) the VTherm is reloaded to discover the provider.
    if detector.providers or (newly_registered and api is not None and _core_attaches_on_registration(api)):
        return True
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
    detector.async_stop()

    data = hass.data.get(DOMAIN, {})
    detectors: dict[str, InhibitionDetector] = data.get(DATA_DETECTORS, {})
    if detectors.get(detector.config.vtherm_unique_id) is detector:
        detectors.pop(detector.config.vtherm_unique_id)
    if not detectors:
        async_unregister_factory(hass)

    if entry.disabled_by is not None:
        # Not eager: the VTherm reload must start after this unload has returned.
        hass.async_create_task(
            async_reload_vtherm(hass, detector.config), f"{DOMAIN} release VTherm", eager_start=False
        )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: InhibitionConfigEntry) -> None:
    """Reload the VTherm without provider once its entry is deleted."""
    await async_reload_vtherm(hass, InhibitionConfig.from_entry(entry.data, entry.options))
