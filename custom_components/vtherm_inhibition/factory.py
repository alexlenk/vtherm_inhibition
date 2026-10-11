"""Inhibition provider factory registered with ``VThermAPI``."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from vtherm_api import InterfaceThermostatRuntime, VThermAPI
from vtherm_api.const import DOMAIN as VTHERM_API_DOMAIN
from vtherm_api.const import VTHERM_API_NAME
from vtherm_api.log_collector import get_vtherm_logger

from .const import DATA_DETECTORS, DATA_FACTORY, DOMAIN, PROVIDER_NAME
from .detector import InhibitionDetector
from .provider import VThermInhibitionProvider

_LOGGER = get_vtherm_logger(__name__)


class InhibitionProviderFactory:
    """Create a provider for each VTherm that has a plugin config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    @property
    def name(self) -> str:
        """Return the stable factory name."""
        return PROVIDER_NAME

    def _detector_for(self, thermostat: InterfaceThermostatRuntime) -> InhibitionDetector | None:
        """Match the VTherm by unique_id (stable), by entity_id only without one."""
        detectors: dict[str, InhibitionDetector] = self._hass.data.get(DOMAIN, {}).get(DATA_DETECTORS, {})
        unique_id = getattr(thermostat, "unique_id", None)
        if unique_id:
            return detectors.get(unique_id)
        entity_id = getattr(thermostat, "entity_id", None)
        if entity_id:
            for detector in detectors.values():
                if detector.config.vtherm_entity_id == entity_id:
                    return detector
        return None

    def supports(self, thermostat: InterfaceThermostatRuntime) -> bool:
        """Return True when a plugin entry targets this VTherm. Pure, no side effect."""
        return self._detector_for(thermostat) is not None

    def create(self, thermostat: InterfaceThermostatRuntime) -> VThermInhibitionProvider:
        """Create the provider bound to this VTherm's detector."""
        detector = self._detector_for(thermostat)
        if detector is None:
            raise ValueError(f"No {DOMAIN} configuration for VTherm {getattr(thermostat, 'unique_id', None)}")
        detector.created_providers += 1
        return VThermInhibitionProvider(detector, thermostat)


def get_core_api(hass: HomeAssistant) -> VThermAPI | None:
    """Return the VTherm API created by the core, without ever creating one.

    ``VThermAPI.get_vtherm_api(hass)`` creates a bare API when the core has
    none (e.g. after its last entry was unloaded), which breaks the core later.
    """
    return hass.data.get(VTHERM_API_DOMAIN, {}).get(VTHERM_API_NAME)


def core_supports_inhibition(api: object) -> bool:
    """Return True when the loaded core implements inhibition providers (#2087).

    ``vtherm_api`` >= 0.6.0 is not enough: an older core subclasses it and
    inherits the registry, but never queries it. A core with inhibition support
    overrides ``register_inhibition_provider_factory`` and
    ``unregister_inhibition_provider_factory`` to attach/detach the providers
    of its running VTherms (documented "late plugin" behaviour); that override
    is the check.
    """
    for method in ("register_inhibition_provider_factory", "unregister_inhibition_provider_factory"):
        implementation = getattr(type(api), method, None)
        if implementation is None or implementation is getattr(VThermAPI, method, None):
            return False
    return hasattr(api, "get_inhibition_provider_factories")


def async_register_factory(hass: HomeAssistant) -> bool:
    """Register the factory once; replace a stale factory of this plugin."""
    data = hass.data.setdefault(DOMAIN, {})
    factory: InhibitionProviderFactory | None = data.get(DATA_FACTORY)
    if factory is None:
        factory = InhibitionProviderFactory(hass)
        data[DATA_FACTORY] = factory
    api = get_core_api(hass)
    if api is None or not core_supports_inhibition(api):
        _LOGGER.warning("Unable to register the inhibition provider factory: no Versatile Thermostat core with inhibition support")
        return False
    existing = api.get_inhibition_provider_factory(PROVIDER_NAME)
    if existing is factory:
        return True
    if isinstance(existing, InhibitionProviderFactory):
        # Left over by a previous load of this plugin: replace it.
        api.unregister_inhibition_provider_factory(PROVIDER_NAME)
    try:
        api.register_inhibition_provider_factory(factory)
    except ValueError as err:
        _LOGGER.error("Unable to register the inhibition provider factory: %s", err)
        return False
    return True


def async_unregister_factory(hass: HomeAssistant) -> None:
    """Remove the factory from the VTherm API registry."""
    data = hass.data.get(DOMAIN, {})
    factory = data.pop(DATA_FACTORY, None)
    api = get_core_api(hass)
    if api is not None and factory is not None and api.get_inhibition_provider_factory(PROVIDER_NAME) is factory:
        api.unregister_inhibition_provider_factory(PROVIDER_NAME)
