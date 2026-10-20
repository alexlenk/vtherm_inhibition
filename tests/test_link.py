"""API calls: set for ACTIVE/UNKNOWN, unset only for a confirmed INACTIVE, lifecycle and retries."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import mock_restore_cache
from vtherm_api.const import DOMAIN as API_DOMAIN
from vtherm_api.const import VTHERM_API_NAME

from custom_components.vtherm_inhibition.const import CONF_STOP_UNDERLYINGS, CONF_VTHERM_ENTITY_ID, DOMAIN
from custom_components.vtherm_inhibition.link import ISSUE_CORE_WITHOUT_INHIBITION
from custom_components.vtherm_inhibition.models import InhibitionStatus

from .conftest import VT_ENTITY_ID, VT_UNIQUE_ID, FakeCoreAPI, set_vtherm_state

HEATER = "input_boolean.heater"
SENSOR = "binary_sensor.bathroom_inhibition"
BYPASS = "switch.bathroom_inhibition_bypass"
SET = ("set", VT_ENTITY_ID, True)
UNSET = ("unset", VT_ENTITY_ID, None)


def _attrs(hass: HomeAssistant) -> dict:
    return dict(hass.states.get(SENSOR).attributes)


# ---------------------------------------------------------------------------
# Status -> call
# ---------------------------------------------------------------------------
async def test_active_sets_with_stop_flag(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """ACTIVE confirmed after the activation delay -> one set(stop_underlyings)."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER], activation=30)
    assert core_api.calls == [UNSET]  # startup re-assertion of the confirmed INACTIVE (a no-op in the core)

    hass.states.async_set(HEATER, "on")
    await advance(29)
    assert core_api.calls == [UNSET]
    await advance(1)
    assert core_api.calls == [UNSET, SET]


async def test_stop_underlyings_false(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """The per-VTherm option is passed as stop_underlyings."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER], stop_underlyings=False)
    assert core_api.calls == [("set", VT_ENTITY_ID, False)]


async def test_unknown_sets(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """UNKNOWN (grace 0) also sets; ACTIVE <-> UNKNOWN makes no new call."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    hass.states.async_set(HEATER, "unavailable")
    await hass.async_block_till_done()
    assert core_api.calls == [UNSET, SET]
    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    hass.states.async_set(HEATER, "unknown")
    await hass.async_block_till_done()
    assert core_api.calls == [UNSET, SET]


async def test_unset_only_once_inactive_is_confirmed(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """No unset during the deactivation delay, nor while UNKNOWN; unset once INACTIVE is confirmed."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER], deactivation=60)
    assert core_api.calls == [SET]

    hass.states.async_set(HEATER, "off")
    await advance(59)
    hass.states.async_set(HEATER, "unavailable")  # grace 0: UNKNOWN at once
    await advance(120)
    assert core_api.calls == [SET]
    hass.states.async_set(HEATER, "off")
    await advance(59)
    assert core_api.calls == [SET]
    await advance(1)
    assert core_api.calls == [SET, UNSET]


async def test_grace_period_makes_no_call(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """A dropout shorter than the grace period makes no call; a longer one sets."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER], grace=120)
    hass.states.async_set(HEATER, "unavailable")
    await advance(100)
    hass.states.async_set(HEATER, "off")
    await advance(600)
    assert core_api.calls == [UNSET]

    hass.states.async_set(HEATER, "unavailable")
    await advance(119)
    assert core_api.calls == [UNSET]
    await advance(1)
    assert core_api.calls == [UNSET, SET]


async def test_bypass(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """Bypass on releases the VTherm at once; off inhibits again after the activation delay."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER], activation=30)
    await hass.services.async_call("switch", "turn_on", {"entity_id": BYPASS}, blocking=True)
    await hass.async_block_till_done()
    assert core_api.calls == [SET, UNSET]
    assert _attrs(hass)["bypass"] is True

    await hass.services.async_call("switch", "turn_off", {"entity_id": BYPASS}, blocking=True)
    await advance(29)
    assert core_api.calls == [SET, UNSET]
    await advance(1)
    assert core_api.calls == [SET, UNSET, SET]


async def test_policy_change_applies_from_next_entry(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """Changing stop_underlyings while inhibited makes no call; the next set uses it."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    hass.config_entries.async_update_entry(entry, options={CONF_STOP_UNDERLYINGS: False})
    await hass.async_block_till_done()
    assert core_api.calls == [SET]

    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    assert core_api.calls == [SET, UNSET, ("set", VT_ENTITY_ID, False)]


async def test_status_change_during_a_call(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """Calls never overlap: a status change during a slow call is applied after it."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])
    core_api.gate = asyncio.Event()
    hass.states.async_set(HEATER, "on")
    await asyncio.sleep(0)
    hass.states.async_set(HEATER, "off")
    await asyncio.sleep(0)
    assert core_api.calls == [UNSET, SET]
    core_api.gate.set()
    await hass.async_block_till_done()
    assert core_api.calls == [UNSET, SET, UNSET]
    assert entry.runtime_data.link.call_pending is False


def _events(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "NEW EVENT" in r.getMessage()]


async def test_event_log_on_each_call_change(hass: HomeAssistant, vtherm, core_api, setup_plugin, caplog) -> None:
    """Each change of the call is written to the VTherm event log with its reason; the startup is not."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    assert _events(caplog) == []

    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    await hass.services.async_call("switch", "turn_on", {"entity_id": BYPASS}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": BYPASS}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    hass.states.async_set(HEATER, "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set(HEATER, "on")  # UNKNOWN -> ACTIVE: same call, nothing logged
    await hass.async_block_till_done()

    events = _events(caplog)
    assert len(events) == 5
    assert events[0].startswith(f"{VT_ENTITY_ID} - ")
    assert "Inhibition requested by VThermInhibition-Bathroom (active source: input_boolean.heater)" in events[0]
    assert "Inhibition released by VThermInhibition-Bathroom (the bypass switch is on)" in events[1]
    assert "Inhibition requested by VThermInhibition-Bathroom (active source: input_boolean.heater)" in events[2]
    assert "Inhibition released by VThermInhibition-Bathroom (no active source)" in events[3]
    assert "Inhibition requested by VThermInhibition-Bathroom (unavailable source: input_boolean.heater)" in events[4]


async def test_event_log_uses_the_vtherm_name(hass: HomeAssistant, vtherm, core_api, setup_plugin, caplog, monkeypatch) -> None:
    """The event is written under the VTherm entity (its name), so it shows in that VTherm's logs."""

    class _VTherm:
        def __str__(self) -> str:
            return "Bathroom VTherm"

    monkeypatch.setattr("custom_components.vtherm_inhibition.link._find_vtherm_entity", lambda _h, _e: _VTherm())
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    assert _events(caplog)[0].startswith("Bathroom VTherm - ---------------------> NEW EVENT: Inhibition requested")


# ---------------------------------------------------------------------------
# Restart, reload, unload, removal
# ---------------------------------------------------------------------------
async def test_restart_restored_active_is_reasserted(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """Restored ACTIVE with sources not available yet: set again, never unset before confirmation."""
    mock_restore_cache(hass, [State(SENSOR, "on")])
    hass.states.async_set(HEATER, "unavailable")
    entry = await setup_plugin([HEATER], grace=300, deactivation=60)
    assert entry.runtime_data.detector.status is InhibitionStatus.ACTIVE
    assert core_api.calls == [SET]
    await advance(300)  # grace expired: UNKNOWN, still set (no new call)
    assert core_api.calls == [SET]

    hass.states.async_set(HEATER, "off")  # first live value: confirmed at once
    await hass.async_block_till_done()
    assert core_api.calls == [SET, UNSET]


async def test_waits_for_the_vtherm_entity(hass: HomeAssistant, core_api, setup_plugin, advance) -> None:
    """Plugin set up before the VTherm entity exists (not even registered): no call until it
    exists; then called at once (this fake has no core entity, so the VTherm counts as
    started; waiting for the startup restore is tested below)."""
    hass.states.async_set(HEATER, "on")
    hass.states.async_set(VT_ENTITY_ID, "heat")  # another entity with the stored entity ID: not used
    entry = await setup_plugin([HEATER])
    assert core_api.calls == []
    assert _attrs(hass)["retry_pending"] is True
    assert _attrs(hass)["vtherm_available"] is False
    assert _attrs(hass)["vtherm_entity_id"] == ""
    await advance(600)
    assert core_api.calls == []
    hass.states.async_remove(VT_ENTITY_ID)

    er.async_get(hass).async_get_or_create("climate", "versatile_thermostat", VT_UNIQUE_ID, suggested_object_id="bathroom")
    await hass.async_block_till_done()
    assert _attrs(hass)["vtherm_entity_id"] == VT_ENTITY_ID
    assert core_api.calls == []

    hass.states.async_set(VT_ENTITY_ID, "heat")  # first state of the entity, before its startup
    await hass.async_block_till_done()
    assert core_api.calls == [SET]
    assert _attrs(hass)["retry_pending"] is False
    assert entry.runtime_data.link.vtherm_available is True
    # Its startup (is_ready) is not a reappearance: no new call.
    set_vtherm_state(hass, ready=True)
    await hass.async_block_till_done()
    assert core_api.calls == [SET]


async def test_entry_reload_reasserts_without_unset(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """Reloading the entry while inhibited: no unset, set re-asserted."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert core_api.calls == [SET, SET]


async def test_unload_and_disable_make_no_call(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """Unloading (or disabling) never implies INACTIVE: no unset, timers stopped."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER], deactivation=10)
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await advance(60)
    assert core_api.calls == [SET]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert core_api.calls == [SET, UNSET]  # first live value after the setup
    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert core_api.calls == [SET, UNSET, SET]


async def test_removal_unsets(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """Deleting the entry releases the VTherm."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert core_api.calls == [SET, UNSET]


async def test_removal_with_vtherm_deleted(hass: HomeAssistant, core_api, setup_plugin, caplog) -> None:
    """Removal while the VTherm does not exist any more: nothing to release, no issue."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert core_api.calls == []
    assert "nothing to release" in caplog.text
    assert not [i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN]


async def test_removal_release_failure_raises_issue(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """Removal while Versatile Thermostat cannot be reached: a persistent repair issue explains
    the recovery; setting up an entry for this VTherm again removes it."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    hass.data[API_DOMAIN].pop(VTHERM_API_NAME)  # core not loaded
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "release_failed_vt-bathroom-uid")
    assert issue is not None
    assert issue.is_persistent is True
    assert issue.translation_placeholders == {
        "vtherm": VT_ENTITY_ID,
        "error": "LookupError: Versatile Thermostat is not loaded",
    }

    hass.data[API_DOMAIN][VTHERM_API_NAME] = core_api
    await setup_plugin([HEATER])
    assert ir.async_get(hass).async_get_issue(DOMAIN, "release_failed_vt-bathroom-uid") is None


async def test_vtherm_reload_reasserts(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """The VTherm disappears and comes back (VTherm reload): the call is made again."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    hass.states.async_remove(VT_ENTITY_ID)
    await hass.async_block_till_done()
    assert core_api.calls == [SET]
    set_vtherm_state(hass, ready=False)
    await hass.async_block_till_done()
    assert core_api.calls == [SET, SET]
    set_vtherm_state(hass, ready=True)
    await hass.async_block_till_done()
    assert core_api.calls == [SET, SET]
    # Attribute updates of a ready VTherm make no call.
    hass.states.async_set(VT_ENTITY_ID, "off", {"is_ready": True})
    await hass.async_block_till_done()
    assert core_api.calls == [SET, SET]


# ---------------------------------------------------------------------------
# Errors and retries
# ---------------------------------------------------------------------------
async def test_unset_failure_is_retried(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """A failed exit reconciliation is retried after 30 s, 60 s, 120 s, then every 5 minutes."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    core_api.errors = [RuntimeError("reconciliation failed")] * 5
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert core_api.names == ["set", "unset"]
    attrs = _attrs(hass)
    assert attrs["last_error"] == "RuntimeError: reconciliation failed"
    assert attrs["retry_pending"] is True
    assert attrs["next_retry_at"] is not None
    assert attrs["status"] == "inactive"

    issue_id = "unset_failing_vt-bathroom-uid"
    for delay, count in ((30, 2), (60, 3), (120, 4), (300, 5)):
        assert (ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None) is (count <= 4)
        await advance(delay - 1)
        assert core_api.names.count("unset") == count - 1
        await advance(1)
        assert core_api.names.count("unset") == count
        if count == 4:  # the 3rd retry failed
            issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
            assert issue is not None
            assert issue.translation_placeholders == {
                "entry": "Bathroom",
                "vtherm": VT_ENTITY_ID,
                "error": "RuntimeError: reconciliation failed",
            }
    # The 5th failure is followed by a success 5 minutes later.
    await advance(300)
    assert core_api.names.count("unset") == 6
    attrs = _attrs(hass)
    assert attrs["last_error"] is None
    assert attrs["retry_pending"] is False
    assert attrs["next_retry_at"] is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await advance(3600)
    assert core_api.names.count("unset") == 6


async def test_retry_cancelled_by_status_change(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """A status change cancels the retry of the failed unset and makes the new call."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER], activation=10)
    core_api.errors = [RuntimeError("reconciliation failed")] * 4
    hass.states.async_set(HEATER, "off")
    for delay in (30, 60, 120):
        await advance(delay)
    assert core_api.names == ["set", "unset", "unset", "unset", "unset"]
    issue_id = "unset_failing_vt-bathroom-uid"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    hass.states.async_set(HEATER, "on")
    await advance(10)
    # After a failure the core state is not known: set is made even though it was the last success.
    assert core_api.names == ["set", "unset", "unset", "unset", "unset", "set"]
    assert _attrs(hass)["retry_pending"] is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await advance(3600)
    assert core_api.names == ["set", "unset", "unset", "unset", "unset", "set"]
    assert entry.runtime_data.link.call_pending is False


async def test_core_not_loaded_is_retried(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """Without the core's API instance the call fails and is retried until it is there."""
    hass.data[API_DOMAIN].pop(VTHERM_API_NAME)
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    assert _attrs(hass)["last_error"] == "LookupError: Versatile Thermostat is not loaded"
    assert VTHERM_API_NAME not in hass.data[API_DOMAIN]  # never created by the plugin
    hass.data[API_DOMAIN][VTHERM_API_NAME] = core_api
    await advance(30)
    assert core_api.calls == [SET]
    assert _attrs(hass)["last_error"] is None


async def test_core_without_inhibition_support(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """TypeError (VTherm without async_set_inhibition): repair issue, no timer retry, retried on reappearance."""
    core_api.errors = [TypeError("VTherm entity 'climate.bathroom' does not support inhibition")]
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION) is not None
    assert _attrs(hass)["retry_pending"] is True
    await advance(3600)
    assert core_api.names == ["set"]

    hass.states.async_remove(VT_ENTITY_ID)
    set_vtherm_state(hass)
    await hass.async_block_till_done()
    assert core_api.names == ["set", "set"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION) is None


async def test_real_facade_errors(hass: HomeAssistant, vtherm, setup_plugin, advance, monkeypatch) -> None:
    """With the real vtherm_api facade: a climate entity without inhibition support raises TypeError."""
    from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
    from vtherm_api import VThermAPI

    class _Component:
        def __init__(self) -> None:
            self.entities: list = []

    class _OldVTherm:
        entity_id = VT_ENTITY_ID

    monkeypatch.setattr(VThermAPI, "_hass", hass)
    hass.data[API_DOMAIN][VTHERM_API_NAME] = VThermAPI()
    component = hass.data[CLIMATE_DOMAIN] = _Component()
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    assert _attrs(hass)["last_error"].startswith("LookupError: No VTherm entity found")

    component.entities = [_OldVTherm()]
    await advance(30)
    assert _attrs(hass)["last_error"] == (
        "CoreWithoutInhibitionError: VTherm entity 'climate.bathroom' does not support inhibition"
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION) is not None
    assert _attrs(hass)["next_retry_at"] is None  # not retried on a timer

    # A VTherm with inhibition support whose core raises a TypeError: an ordinary failure.
    calls: list[str] = []

    class _BuggyVTherm:
        entity_id = VT_ENTITY_ID

        async def async_set_inhibition(self, stop_underlyings: bool) -> None:
            calls.append("set")
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'float'")

        async def async_unset_inhibition(self) -> None:
            calls.append("unset")

    component.entities = [_BuggyVTherm()]
    hass.states.async_remove(VT_ENTITY_ID)
    set_vtherm_state(hass)  # restarted VTherm
    await hass.async_block_till_done()
    assert calls == ["set"]
    assert _attrs(hass)["last_error"].startswith("TypeError: unsupported operand")
    assert _attrs(hass)["next_retry_at"] is not None
    await advance(30)
    assert calls == ["set", "set"]
    hass.data.pop(CLIMATE_DOMAIN)


class _Manager:
    def __init__(self, restored: bool) -> None:
        self.is_restored = restored


class _StartingVTherm:
    """A VTherm entity as the core has it, before or after its startup restore."""

    entity_id = VT_ENTITY_ID

    def __init__(self, restored: bool) -> None:
        self.inhibition_manager = _Manager(restored)

    @property
    def is_inhibition_started(self) -> bool:
        """As the core: True once the inhibition state is restored at startup."""
        return self.inhibition_manager.is_restored

    async def async_set_inhibition(self, stop_underlyings: bool) -> None:
        raise AssertionError("called through the facade")

    async def async_unset_inhibition(self) -> None:
        raise AssertionError("called through the facade")


def _install_vtherm_entity(hass: HomeAssistant, restored: bool) -> _StartingVTherm:
    from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN

    class _Component:
        entities: list = []

    entity = _StartingVTherm(restored)
    component = hass.data[CLIMATE_DOMAIN] = _Component()
    component.entities = [entity]
    return entity


async def test_waits_for_the_vtherm_startup(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """No call before the VTherm restored its state; the call is made at its next state change."""
    entity = _install_vtherm_entity(hass, restored=False)
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    assert core_api.calls == []
    attrs = _attrs(hass)
    assert attrs["waiting_for_vtherm_start"] is True
    assert attrs["last_error"] is None
    assert attrs["next_retry_at"] is None

    await advance(60)  # still not started: polled, no call, no retry
    assert core_api.calls == []

    entity.inhibition_manager.is_restored = True
    set_vtherm_state(hass, ready=False)  # the startup writes the state, not ready yet
    await hass.async_block_till_done()
    assert core_api.calls == [SET]
    assert _attrs(hass)["waiting_for_vtherm_start"] is False


async def test_vtherm_startup_found_by_poll(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """Without a state change, the startup is found by the periodic check."""
    entity = _install_vtherm_entity(hass, restored=False)
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    entity.inhibition_manager.is_restored = True
    await advance(5)
    assert core_api.calls == [SET]
    assert _attrs(hass)["waiting_for_vtherm_start"] is False


async def test_not_started_error_waits_without_retry(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """The core's InhibitionNotStartedError (VTherm restarted between check and call) waits
    for the startup instead of counting as a failure."""

    from vtherm_api import InhibitionNotStartedError

    core_api.errors = [InhibitionNotStartedError("not started")]
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    assert core_api.names == ["unset"]
    attrs = _attrs(hass)
    assert attrs["waiting_for_vtherm_start"] is True
    assert attrs["last_error"] is None
    assert attrs["next_retry_at"] is None
    await advance(5)
    assert core_api.names == ["unset", "unset"]
    assert _attrs(hass)["waiting_for_vtherm_start"] is False


# ---------------------------------------------------------------------------
# VTherm targeting
# ---------------------------------------------------------------------------
async def test_vtherm_entity_id_rename(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """The VTherm is resolved from its unique ID: a renamed entity ID is followed."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    er.async_get(hass).async_update_entity(VT_ENTITY_ID, new_entity_id="climate.bath")
    hass.states.async_remove(VT_ENTITY_ID)
    await hass.async_block_till_done()
    set_vtherm_state(hass, entity_id="climate.bath")
    await hass.async_block_till_done()
    assert entry.data[CONF_VTHERM_ENTITY_ID] == "climate.bath"
    assert _attrs(hass)["vtherm_entity_id"] == "climate.bath"

    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert core_api.calls[0] == SET
    assert core_api.calls[-1] == ("unset", "climate.bath", None)

    # Removal resolves the current entity ID too.
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert core_api.calls[-1] == ("unset", "climate.bath", None)


async def test_stale_stored_entity_id(hass: HomeAssistant, vtherm, core_api) -> None:
    """An entry whose stored entity ID is outdated still targets the VTherm (by unique ID)."""
    from .conftest import plugin_entry

    hass.states.async_set(HEATER, "on")
    entry = plugin_entry([HEATER], entity_id="climate.old_name")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert core_api.calls == [SET]


def test_fake_api_is_a_vtherm_api() -> None:
    """The fake keeps the facade signature of vtherm_api."""
    import inspect

    from vtherm_api import VThermAPI

    for name in ("set_inhibition", "unset_inhibition"):
        fake = inspect.signature(getattr(FakeCoreAPI, name)).parameters
        real = inspect.signature(getattr(VThermAPI, name)).parameters
        assert list(fake) == list(real)


async def test_core_internal_type_error_is_retried(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """A TypeError raised inside the core's exit reconciliation is an ordinary failure:
    retried, unset_failing issue after the 3rd retry, no 'core without inhibition' issue."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    core_api.errors = [TypeError("unsupported operand type(s) for +: 'NoneType' and 'float'")] * 4
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    for delay in (30, 60, 120):
        await advance(delay)
    assert core_api.names == ["set", "unset", "unset", "unset", "unset"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "unset_failing_vt-bathroom-uid") is not None
    await advance(300)
    assert core_api.names[-1] == "unset"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "unset_failing_vt-bathroom-uid") is None


async def test_core_without_inhibition_issue_cleared_with_last_entry(
    hass: HomeAssistant, vtherm, core_api, setup_plugin
) -> None:
    """The 'core without inhibition' issue goes away when the last entry is deleted."""
    core_api.errors = [TypeError("VTherm entity 'climate.bathroom' does not support inhibition")]
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION) is not None
    core_api.errors = [RuntimeError("still failing")]
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CORE_WITHOUT_INHIBITION) is None


async def test_reappearance_during_a_failing_call(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """The VTherm reappears while a call to the old instance is in flight and fails: the call is
    made again at once on the new instance (no 30 s retry)."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    core_api.gate = asyncio.Event()
    core_api.errors = [RuntimeError("old instance gone")]
    hass.states.async_set(HEATER, "on")
    for _ in range(5):
        await asyncio.sleep(0)
    assert core_api.names == ["unset", "set"]
    hass.states.async_remove(VT_ENTITY_ID)
    set_vtherm_state(hass)
    for _ in range(5):
        await asyncio.sleep(0)
    core_api.gate.set()
    await hass.async_block_till_done()
    assert core_api.names == ["unset", "set", "set"]
    assert _attrs(hass)["retry_pending"] is False


def _core_state(hass: HomeAssistant, inhibited: bool, gate: str, error: str | None = None) -> None:
    hass.states.async_set(
        VT_ENTITY_ID,
        "off" if inhibited else "heat",
        {
            "is_ready": True,
            "specific_states": {
                "inhibition": {
                    "schema_version": 1,
                    "inhibited": inhibited,
                    "command_gate": gate,
                    "last_error_code": error,
                }
            },
        },
    )


async def test_deferred_exit_failure_is_retried(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """unset succeeded (exit deferred by the core), then the core reports itself inhibited again
    (failed deferred exit): the plugin retries the unset with the normal backoff."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert core_api.names == ["set", "unset"]
    _core_state(hass, inhibited=False, gate="EXIT_RECONCILING")  # deferred exit in progress
    await hass.async_block_till_done()
    assert _attrs(hass)["retry_pending"] is False

    _core_state(hass, inhibited=True, gate="CLOSED", error="exit_reconcile_failed")
    await hass.async_block_till_done()
    attrs = _attrs(hass)
    assert attrs["retry_pending"] is True
    assert attrs["next_retry_at"] is not None
    assert attrs["last_error"] == "Versatile Thermostat is inhibited (exit_reconcile_failed)"
    _core_state(hass, inhibited=True, gate="CLOSED", error="exit_reconcile_failed")  # no second schedule
    await advance(29)
    assert core_api.names == ["set", "unset"]
    await advance(1)
    assert core_api.names == ["set", "unset", "unset"]
    _core_state(hass, inhibited=False, gate="OPEN")
    await hass.async_block_till_done()
    assert _attrs(hass)["retry_pending"] is False
    assert _attrs(hass)["last_error"] is None


async def test_lost_inhibition_is_set_again(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """set succeeded but the core later reports itself not inhibited: set again (backoff)."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    _core_state(hass, inhibited=True, gate="CLOSED")
    await hass.async_block_till_done()
    assert core_api.names == ["set"]
    _core_state(hass, inhibited=False, gate="OPEN")
    await hass.async_block_till_done()
    assert _attrs(hass)["retry_pending"] is True
    await advance(30)
    assert core_api.names == ["set", "set"]


async def test_unavailable_while_waiting_for_startup(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """The VTherm disappears while the plugin waits for its startup: the wait stops; the
    reappeared VTherm (a new instance) is waited for again, then called."""
    entity = _install_vtherm_entity(hass, restored=False)
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    assert _attrs(hass)["waiting_for_vtherm_start"] is True
    hass.states.async_set(VT_ENTITY_ID, "unavailable")
    await hass.async_block_till_done()
    assert _attrs(hass)["waiting_for_vtherm_start"] is False
    await advance(30)
    assert core_api.calls == []

    set_vtherm_state(hass, ready=False)  # back, still not restored
    await hass.async_block_till_done()
    assert _attrs(hass)["waiting_for_vtherm_start"] is True
    assert core_api.calls == []
    entity.inhibition_manager.is_restored = True
    await advance(5)
    assert core_api.calls == [SET]


async def test_status_change_while_waiting_for_startup(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """A status change while waiting makes only the last wanted call once the VTherm started."""
    entity = _install_vtherm_entity(hass, restored=False)
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    hass.states.async_set(HEATER, "off")
    await advance(5)
    assert core_api.calls == []
    entity.inhibition_manager.is_restored = True
    await advance(5)
    assert core_api.calls == [UNSET]
    await advance(60)
    assert core_api.calls == [UNSET]


async def test_unload_while_waiting_for_startup(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """Unloading the entry while waiting stops the checks: no call afterwards."""
    entity = _install_vtherm_entity(hass, restored=False)
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    assert await hass.config_entries.async_unload(entry.entry_id)
    entity.inhibition_manager.is_restored = True
    set_vtherm_state(hass, ready=True)
    await advance(30)
    assert core_api.calls == []


async def test_removal_before_startup_releases_once_started(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """An entry deleted before the VTherm restored its state: the unset is made once it has
    started (no refused call, no repair issue)."""
    entity = _install_vtherm_entity(hass, restored=False)
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert core_api.calls == []
    await advance(5)
    assert core_api.calls == []
    entity.inhibition_manager.is_restored = True
    await advance(5)
    assert core_api.calls == [UNSET]
    assert not [issue for (domain, _), issue in ir.async_get(hass).issues.items() if domain == DOMAIN]


async def test_removal_before_startup_released_after_timeout(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """A VTherm that never starts is released anyway after RELEASE_START_TIMEOUT."""
    from custom_components.vtherm_inhibition.const import RELEASE_START_TIMEOUT

    _install_vtherm_entity(hass, restored=False)
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(RELEASE_START_TIMEOUT // 5 - 1):
        await advance(5)
    assert core_api.calls == []
    await advance(5)
    assert core_api.calls == [UNSET]
