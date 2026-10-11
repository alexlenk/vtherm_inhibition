"""Shared harness of the end-to-end tests (imported only when the core is available).

``custom_components`` is a namespace package (no ``__init__.py`` in either
repository): with the core checkout on ``sys.path`` (done by the package
``__init__``), Home Assistant's loader finds ``versatile_thermostat`` next to
``vtherm_inhibition``. The core test helpers (``tests/commons.py``: mock
climate/switch entities, temperature events...) are imported under the
``vtherm_core_tests`` package name so they do not clash with this
repository's ``tests`` package.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from . import CORE_PATH


def _load_core_tests_package() -> None:
    """Import the core ``tests`` package as ``vtherm_core_tests``."""
    if "vtherm_core_tests" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "vtherm_core_tests",
        CORE_PATH / "tests" / "__init__.py",
        submodule_search_locations=[str(CORE_PATH / "tests")],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["vtherm_core_tests"] = module
    spec.loader.exec_module(module)


_load_core_tests_package()

# pylint: disable=wrong-import-position
from custom_components.versatile_thermostat import const as vt_const
from custom_components.versatile_thermostat.base_thermostat import BaseThermostat
from custom_components.versatile_thermostat.vtherm_central_api import VersatileThermostatAPI
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from vtherm_api import VThermAPI
from vtherm_api.const import get_tz

from custom_components.vtherm_inhibition.const import (
    CONF_ACTIVATION_DELAY,
    CONF_DEACTIVATION_DELAY,
    CONF_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_VTHERM_ENTITY_ID,
    CONF_VTHERM_UNIQUE_ID,
)
from custom_components.vtherm_inhibition.const import (
    DOMAIN as PLUGIN_DOMAIN,
)

commons = importlib.import_module("vtherm_core_tests.commons")

CLIMATE_VT_ID = "climate.bathroom"
CLIMATE_VT_ENTRY_ID = "vt-bathroom-entry"
SWITCH_VT_ID = "climate.office"
SWITCH_VT_ENTRY_ID = "vt-office-entry"
UNDERLYING_CLIMATE = "climate.mock_climate"
UNDERLYING_SWITCH = "switch.mock_switch"
SOURCE = "binary_sensor.bad_heating_blocked"
WINDOW = "binary_sensor.bathroom_window"


# ---------------------------------------------------------------------------
# Harness fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load both custom integrations."""
    yield


@pytest.fixture(autouse=True)
def skip_notifications():
    """No persistent_notification integration in the tests (as in the core tests)."""
    with (
        patch("homeassistant.components.persistent_notification.async_create"),
        patch("homeassistant.components.persistent_notification.async_dismiss"),
    ):
        yield


@pytest.fixture(autouse=True)
def isolate_vtherm_api():
    """Fresh VTherm API singleton for every test."""
    yield
    VersatileThermostatAPI.reset_vtherm_api()
    VThermAPI.reset_vtherm_api()


# ---------------------------------------------------------------------------
# Underlying devices: service calls are recorded and applied to the mocks
# ---------------------------------------------------------------------------
@dataclass
class Underlyings:
    """Records every service call reaching an underlying mock entity."""

    hass: HomeAssistant
    climates: dict[str, Any] = field(default_factory=dict)
    switches: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def install(self) -> None:
        """(Re)register the climate/switch services used by the underlyings.

        Called after the integrations are set up because setting up the
        climate/switch components registers their own services.
        """
        for service in ("set_hvac_mode", "set_temperature", "set_fan_mode", "set_preset_mode"):
            self.hass.services.async_register(CLIMATE_DOMAIN, service, self._handle)
        for service in ("turn_on", "turn_off"):
            self.hass.services.async_register(SWITCH_DOMAIN, service, self._handle)

    async def _handle(self, call: ServiceCall) -> None:
        entity_ids = call.data.get("entity_id")
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        for entity_id in entity_ids or []:
            data = {k: v for k, v in call.data.items() if k != "entity_id"}
            self.calls.append((call.service, entity_id, data))
            if entity_id in self.climates:
                mock = self.climates[entity_id]
                if call.service == "set_hvac_mode":
                    mock.set_hvac_mode(data["hvac_mode"])
                elif call.service == "set_temperature" and "temperature" in data:
                    mock.set_temperature(temperature=data["temperature"])
            elif entity_id in self.switches:
                mock = self.switches[entity_id]
                if call.service == "turn_on":
                    await mock.async_turn_on()
                elif call.service == "turn_off":
                    await mock.async_turn_off()

    def reset(self) -> None:
        """Forget the recorded calls."""
        self.calls.clear()

    def of(self, service: str, entity_id: str | None = None) -> list[dict[str, Any]]:
        """Return the data of the calls to a service (optionally for one entity)."""
        return [data for srv, eid, data in self.calls if srv == service and (entity_id is None or eid == entity_id)]

    def hvac_modes(self, entity_id: str = UNDERLYING_CLIMATE) -> list[str]:
        """HVAC modes sent to an underlying climate, in order."""
        return [str(data["hvac_mode"]) for data in self.of("set_hvac_mode", entity_id)]

    def temperatures(self, entity_id: str = UNDERLYING_CLIMATE) -> list[float]:
        """Target temperatures sent to an underlying climate, in order."""
        return [data["temperature"] for data in self.of("set_temperature", entity_id) if "temperature" in data]


@pytest.fixture
async def underlyings(hass: HomeAssistant) -> Underlyings:
    """The underlying mock climate and switch, plus the window and temperature sensors."""
    result = Underlyings(hass)
    climate = await commons.create_and_register_mock_climate(hass, "mock_climate", "MockClimateName", {})
    result.climates[UNDERLYING_CLIMATE] = climate
    switch = commons.MockSwitch(hass, "mock_switch", "theMockedSwitch")
    await commons.register_mock_entity(hass, switch, SWITCH_DOMAIN)
    result.switches[UNDERLYING_SWITCH] = switch
    hass.states.async_set("sensor.mock_temp_sensor", "15")
    hass.states.async_set("sensor.mock_ext_temp_sensor", "5")
    hass.states.async_set(WINDOW, "off")
    hass.states.async_set(SOURCE, "off")
    return result


# ---------------------------------------------------------------------------
# Config entries
# ---------------------------------------------------------------------------
_COMMON_VT = {
    vt_const.CONF_TEMP_SENSOR: "sensor.mock_temp_sensor",
    vt_const.CONF_EXTERNAL_TEMP_SENSOR: "sensor.mock_ext_temp_sensor",
    vt_const.CONF_CYCLE_MIN: 5,
    vt_const.CONF_TEMP_MIN: 7,
    vt_const.CONF_TEMP_MAX: 30,
    "eco_temp": 17,
    "comfort_temp": 21,
    "boost_temp": 23,
    "frost_temp": 7,
    vt_const.CONF_USE_MOTION_FEATURE: False,
    vt_const.CONF_USE_POWER_FEATURE: False,
    vt_const.CONF_USE_PRESENCE_FEATURE: False,
    vt_const.CONF_MINIMAL_ACTIVATION_DELAY: 30,
    vt_const.CONF_MINIMAL_DEACTIVATION_DELAY: 0,
    vt_const.CONF_SAFETY_DELAY_MIN: 5,
    vt_const.CONF_SAFETY_MIN_ON_PERCENT: 0.3,
    vt_const.CONF_REPAIR_INCORRECT_STATE: True,
}


def _window_conf(window: bool) -> dict[str, Any]:
    if not window:
        return {vt_const.CONF_USE_WINDOW_FEATURE: False}
    return {
        vt_const.CONF_USE_WINDOW_FEATURE: True,
        vt_const.CONF_WINDOW_SENSOR: WINDOW,
        vt_const.CONF_WINDOW_DELAY: 0,
        vt_const.CONF_WINDOW_OFF_DELAY: 0,
        vt_const.CONF_WINDOW_ACTION: vt_const.CONF_WINDOW_TURN_OFF,
    }


def climate_vt_entry(window: bool = False) -> MockConfigEntry:
    """over_climate VTherm "Bathroom" without auto-regulation."""
    data = {
        vt_const.CONF_NAME: "Bathroom",
        vt_const.CONF_THERMOSTAT_TYPE: vt_const.CONF_THERMOSTAT_CLIMATE,
        vt_const.CONF_UNDERLYING_LIST: [UNDERLYING_CLIMATE],
        vt_const.CONF_AUTO_REGULATION_MODE: vt_const.CONF_AUTO_REGULATION_NONE,
        **_COMMON_VT,
        **_window_conf(window),
    }
    return MockConfigEntry(
        domain=vt_const.DOMAIN, title="Bathroom", unique_id=CLIMATE_VT_ENTRY_ID, entry_id=CLIMATE_VT_ENTRY_ID, data=data
    )


def switch_vt_entry() -> MockConfigEntry:
    """over_switch TPI VTherm "Office"."""
    data = {
        vt_const.CONF_NAME: "Office",
        vt_const.CONF_THERMOSTAT_TYPE: vt_const.CONF_THERMOSTAT_SWITCH,
        vt_const.CONF_UNDERLYING_LIST: [UNDERLYING_SWITCH],
        vt_const.CONF_HEATER_KEEP_ALIVE: 0,
        vt_const.CONF_PROP_FUNCTION: vt_const.PROPORTIONAL_FUNCTION_TPI,
        vt_const.CONF_TPI_COEF_INT: 0.3,
        vt_const.CONF_TPI_COEF_EXT: 0.01,
        **_COMMON_VT,
        **_window_conf(False),
    }
    return MockConfigEntry(
        domain=vt_const.DOMAIN, title="Office", unique_id=SWITCH_VT_ENTRY_ID, entry_id=SWITCH_VT_ENTRY_ID, data=data
    )


def plugin_entry(
    vtherm_entry_id: str = CLIMATE_VT_ENTRY_ID,
    vtherm_entity_id: str = CLIMATE_VT_ID,
    sources: list[str] | None = None,
    activation: int = 10,
    deactivation: int = 30,
    grace: int = 0,
    policy: str = "turn_off",
) -> MockConfigEntry:
    """Plugin entry targeting a VTherm by its unique_id (= its config entry id)."""
    return MockConfigEntry(
        domain=PLUGIN_DOMAIN,
        title=vtherm_entity_id,
        unique_id=f"{PLUGIN_DOMAIN}-{vtherm_entry_id}",
        data={
            CONF_VTHERM_ENTITY_ID: vtherm_entity_id,
            CONF_VTHERM_UNIQUE_ID: vtherm_entry_id,
            CONF_SOURCE_ENTITIES: sources or [SOURCE],
            CONF_ACTIVATION_DELAY: activation,
            CONF_DEACTIVATION_DELAY: deactivation,
            CONF_UNAVAILABLE_GRACE_PERIOD: grace,
            CONF_PHYSICAL_STOP_POLICY: policy,
        },
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def vtherm(hass: HomeAssistant, entity_id: str) -> BaseThermostat:
    """Return the live VTherm entity (a reload creates a new instance)."""
    entity = commons.search_entity(hass, entity_id, CLIMATE_DOMAIN)
    assert entity is not None, f"{entity_id} not found"
    return entity


def inhibition_attrs(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """The published ``specific_states.inhibition`` block (None when ABSENT)."""
    state = hass.states.get(entity_id)
    assert state is not None
    return (state.attributes.get("specific_states") or {}).get("inhibition")


def plugin_entity_id(hass: HomeAssistant, domain: str, vtherm_unique_id: str, key: str) -> str:
    """Entity id of a plugin entity, from its stable unique id."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, PLUGIN_DOMAIN, f"{vtherm_unique_id}_{key}")
    assert entity_id is not None
    return entity_id


def plugin_entity(hass: HomeAssistant, entity_id: str):
    """Return the plugin entity object (to drive the bypass switch)."""
    for platform in entity_platform.async_get_platforms(hass, PLUGIN_DOMAIN):
        if entity_id in platform.entities:
            return platform.entities[entity_id]
    raise AssertionError(f"{entity_id} not loaded")


@pytest.fixture
def advance(hass: HomeAssistant, freezer) -> Callable[[float], Awaitable[None]]:
    """Move the clock forward by N seconds and run the due timers."""

    async def _advance(seconds: float) -> None:
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    return _advance


async def heat_comfort(hass: HomeAssistant, entity: BaseThermostat) -> None:
    """Put the VTherm in heat / comfort with a cold room."""
    from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT
    from custom_components.versatile_thermostat.vtherm_preset import VThermPreset

    now = datetime.now(tz=get_tz(hass))
    await commons.send_temperature_change_event(entity, 15, now, sleep=False)
    await commons.send_ext_temperature_change_event(entity, 5, now, sleep=False)
    await entity.async_set_hvac_mode(VThermHvacMode_HEAT)
    await entity.async_set_preset_mode(VThermPreset.COMFORT)
    await hass.async_block_till_done()



BATHROOM_SENSOR = "binary_sensor.bathroom_inhibition"
BATHROOM_BYPASS = "switch.bathroom_inhibition_bypass"
OFFICE_SENSOR = "binary_sensor.office_inhibition"


@dataclass
class Scenario:
    """A VTherm and its plugin entry."""

    hass: HomeAssistant
    vt_entry: MockConfigEntry
    plugin_entry: MockConfigEntry
    entity_id: str

    @property
    def vt(self) -> BaseThermostat:
        """The live VTherm (a VTherm reload creates a new entity object)."""
        return vtherm(self.hass, self.entity_id)

    @property
    def attrs(self) -> dict[str, Any] | None:
        """The published ``specific_states.inhibition`` block."""
        return inhibition_attrs(self.hass, self.entity_id)

    @property
    def detector(self):
        """The plugin detector of the entry."""
        return self.plugin_entry.runtime_data


@pytest.fixture
def setup_bathroom(hass: HomeAssistant, underlyings: Underlyings, advance):
    """Return a coroutine creating the heating bathroom VTherm, then its plugin entry
    (the "add the plugin while Home Assistant runs" path)."""

    async def _setup(window: bool = False, **plugin_kwargs: Any) -> Scenario:
        vt_entry = climate_vt_entry(window=window)
        await setup_entry(hass, vt_entry)
        underlyings.install()
        await heat_comfort(hass, vtherm(hass, CLIMATE_VT_ID))
        p_entry = plugin_entry(**plugin_kwargs)
        await setup_entry(hass, p_entry)
        underlyings.install()
        # let the delayed temperature resend of the underlying climate run
        await advance(5)
        return Scenario(hass, vt_entry, p_entry, CLIMATE_VT_ID)

    return _setup


async def send_temperature(hass: HomeAssistant, entity: BaseThermostat, temperature: float = 15) -> None:
    """A fresh room temperature (keeps the safety feature quiet across long waits)."""
    now = datetime.now(tz=get_tz(hass))
    await commons.send_temperature_change_event(entity, temperature, now, sleep=False)
    await commons.send_ext_temperature_change_event(entity, 5, now, sleep=False)
    await hass.async_block_till_done()

__all__ = [
    "BATHROOM_BYPASS",
    "BATHROOM_SENSOR",
    "BINARY_SENSOR_DOMAIN",
    "CLIMATE_VT_ENTRY_ID",
    "CLIMATE_VT_ID",
    "OFFICE_SENSOR",
    "SOURCE",
    "SWITCH_VT_ENTRY_ID",
    "SWITCH_VT_ID",
    "UNDERLYING_CLIMATE",
    "UNDERLYING_SWITCH",
    "WINDOW",
    "Scenario",
    "Underlyings",
    "climate_vt_entry",
    "commons",
    "heat_comfort",
    "inhibition_attrs",
    "plugin_entity",
    "plugin_entity_id",
    "plugin_entry",
    "send_temperature",
    "setup_entry",
    "switch_vt_entry",
    "vtherm",
]
