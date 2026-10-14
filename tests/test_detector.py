"""Detection: aggregation, delays, grace period, startup and bypass."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.vtherm_inhibition.models import InhibitionStatus

ACTIVE = InhibitionStatus.ACTIVE
INACTIVE = InhibitionStatus.INACTIVE
UNKNOWN = InhibitionStatus.UNKNOWN

HEATER = "input_boolean.heater"
AC = "climate.bathroom_ac"
BLOCK = "binary_sensor.bathroom_heating_blocked"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
async def test_or_aggregation(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Any active source inhibits; all inactive releases."""
    hass.states.async_set(HEATER, "off")
    hass.states.async_set(BLOCK, "off")
    await setup_plugin([HEATER, BLOCK])
    view, rec = await attach()
    assert view.status is INACTIVE

    hass.states.async_set(BLOCK, "on")
    await hass.async_block_till_done()
    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    assert view.status is ACTIVE

    hass.states.async_set(BLOCK, "off")
    await hass.async_block_till_done()
    assert view.status is ACTIVE

    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert view.status is INACTIVE
    assert rec.statuses == [ACTIVE, INACTIVE]


async def test_climate_source_default_modes(hass: HomeAssistant, setup_plugin, attach) -> None:
    """A climate source inhibits in heat/cool/heat_cool/auto/dry, not in off/fan_only."""
    hass.states.async_set(AC, "off")
    await setup_plugin([AC])
    view, _ = await attach()
    for mode, expected in [
        ("heat", ACTIVE),
        ("fan_only", INACTIVE),
        ("cool", ACTIVE),
        ("off", INACTIVE),
        ("dry", ACTIVE),
        ("heat_cool", ACTIVE),
        ("auto", ACTIVE),
    ]:
        hass.states.async_set(AC, mode)
        await hass.async_block_till_done()
        assert view.status is expected, mode


async def test_climate_source_custom_modes(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Only the configured HVAC modes inhibit."""
    hass.states.async_set(AC, "cool")
    await setup_plugin([AC], climate_hvac_modes=["heat"])
    view, _ = await attach()
    assert view.status is INACTIVE
    hass.states.async_set(AC, "heat")
    await hass.async_block_till_done()
    assert view.status is ACTIVE


async def test_switch_source(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Switches are on/off sources."""
    hass.states.async_set("switch.heater", "on")
    await setup_plugin(["switch.heater"])
    view, _ = await attach()
    assert view.status is ACTIVE


async def test_active_source_wins_over_unavailable(hass: HomeAssistant, setup_plugin, attach) -> None:
    """An available active source keeps the aggregate ACTIVE whatever the others."""
    hass.states.async_set(HEATER, "on")
    hass.states.async_set(AC, "unavailable")
    await setup_plugin([HEATER, AC])
    view, rec = await attach()
    assert view.status is ACTIVE
    hass.states.async_set(AC, "unknown")
    await hass.async_block_till_done()
    assert view.status is ACTIVE
    assert rec.transitions == []


async def test_unavailable_without_active_is_unknown(hass: HomeAssistant, setup_plugin, attach) -> None:
    """No active source and one unavailable source: UNKNOWN (grace 0)."""
    hass.states.async_set(HEATER, "off")
    hass.states.async_set(AC, "off")
    await setup_plugin([HEATER, AC])
    view, rec = await attach()
    hass.states.async_set(AC, "unavailable")
    await hass.async_block_till_done()
    assert view.status is UNKNOWN
    assert rec.statuses == [UNKNOWN]


async def test_missing_source_entity(hass: HomeAssistant, setup_plugin, attach) -> None:
    """A source that does not exist yet keeps the status UNKNOWN."""
    await setup_plugin(["binary_sensor.not_there_yet"], grace=60)
    view, _ = await attach()
    assert view.status is UNKNOWN
    hass.states.async_set("binary_sensor.not_there_yet", "off")
    await hass.async_block_till_done()
    assert view.status is INACTIVE


# ---------------------------------------------------------------------------
# Delays
# ---------------------------------------------------------------------------
async def test_activation_delay(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """ACTIVE is confirmed only after the activation delay."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER], activation=30)
    detector = entry.runtime_data.detector
    view, rec = await attach()

    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    assert view.status is INACTIVE
    assert detector.raw_status is ACTIVE
    assert detector.pending_status is ACTIVE
    assert detector.status is INACTIVE

    await advance(29)
    assert view.status is INACTIVE
    assert rec.transitions == []

    await advance(1)
    assert view.status is ACTIVE
    assert detector.pending_status is None
    assert rec.statuses == [ACTIVE]


async def test_deactivation_delay(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """INACTIVE is confirmed only after the deactivation delay."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER], deactivation=60)
    view, rec = await attach()
    assert view.status is ACTIVE

    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    await advance(59)
    assert view.status is ACTIVE
    await advance(1)
    assert view.status is INACTIVE
    assert rec.statuses == [INACTIVE]


async def test_flapping_cancels_timer(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """A raw change back to the confirmed status cancels the pending timer."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER], activation=30)
    view, rec = await attach()

    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    await advance(10)
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert entry.runtime_data.detector.pending_status is None
    await advance(120)
    assert view.status is INACTIVE
    assert rec.transitions == []


async def test_delay_restarts_on_each_raw_change(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """on, off, on: the activation delay counts from the last raw change."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER], activation=30)
    view, rec = await attach()

    hass.states.async_set(HEATER, "on")
    await advance(10)
    hass.states.async_set(HEATER, "off")
    await advance(10)
    hass.states.async_set(HEATER, "on")
    await advance(25)
    assert view.status is INACTIVE
    await advance(5)
    assert view.status is ACTIVE
    assert rec.statuses == [ACTIVE]


async def test_attribute_update_does_not_restart_timer(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Repeated raw states (attribute updates) keep the running timer."""
    hass.states.async_set(AC, "off")
    await setup_plugin([AC], activation=30)
    view, _ = await attach()

    hass.states.async_set(AC, "heat", {"temperature": 21})
    await advance(20)
    hass.states.async_set(AC, "heat", {"temperature": 22})
    hass.states.async_set(AC, "cool", {"temperature": 22})
    await advance(10)
    assert view.status is ACTIVE


# ---------------------------------------------------------------------------
# Unavailable grace period
# ---------------------------------------------------------------------------
async def test_grace_keeps_last_status_then_unknown(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Unavailable: last confirmed status kept for the grace period, then UNKNOWN."""
    hass.states.async_set(AC, "off")
    entry = await setup_plugin([AC], grace=120)
    view, rec = await attach()

    hass.states.async_set(AC, "unavailable")
    await hass.async_block_till_done()
    assert view.status is INACTIVE
    assert entry.runtime_data.detector.pending_status is UNKNOWN
    await advance(119)
    assert view.status is INACTIVE
    await advance(1)
    assert view.status is UNKNOWN
    assert rec.statuses == [UNKNOWN]


async def test_grace_recovery_to_same_status(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """A short drop to unavailable produces no transition at all."""
    hass.states.async_set(AC, "heat")
    await setup_plugin([AC], grace=120, activation=30, deactivation=60)
    view, rec = await attach()
    assert view.status is ACTIVE

    hass.states.async_set(AC, "unavailable")
    await advance(60)
    hass.states.async_set(AC, "heat")
    await advance(600)
    assert view.status is ACTIVE
    assert rec.transitions == []


async def test_grace_recovery_to_other_status_uses_delay(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Back from unavailable with another status: the normal delay applies from then."""
    hass.states.async_set(AC, "heat")
    await setup_plugin([AC], grace=120, deactivation=60)
    view, rec = await attach()

    hass.states.async_set(AC, "unavailable")
    await advance(100)
    hass.states.async_set(AC, "off")
    await advance(59)
    assert view.status is ACTIVE
    await advance(1)
    assert view.status is INACTIVE
    assert rec.statuses == [INACTIVE]


async def test_recovery_after_unknown(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """UNKNOWN (grace expired) -> INACTIVE after the deactivation delay."""
    hass.states.async_set(AC, "off")
    await setup_plugin([AC], grace=10, deactivation=60)
    view, rec = await attach()

    hass.states.async_set(AC, "unavailable")
    await advance(10)
    assert view.status is UNKNOWN
    hass.states.async_set(AC, "off")
    await advance(59)
    assert view.status is UNKNOWN
    await advance(1)
    assert view.status is INACTIVE
    assert rec.statuses == [UNKNOWN, INACTIVE]


async def test_zero_grace_is_immediate(hass: HomeAssistant, setup_plugin, attach) -> None:
    """With grace 0, unavailable means UNKNOWN at once."""
    hass.states.async_set(AC, "heat")
    await setup_plugin([AC], grace=0)
    view, rec = await attach()
    hass.states.async_set(AC, "unavailable")
    await hass.async_block_till_done()
    assert view.status is UNKNOWN
    assert rec.statuses == [UNKNOWN]


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
async def test_initial_status_ignores_delays(hass: HomeAssistant, setup_plugin, attach) -> None:
    """The first known aggregate is confirmed immediately."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER], activation=300, deactivation=300)
    assert entry.runtime_data.detector.status is ACTIVE
    _view, rec = await attach()
    assert entry.runtime_data.detector.status is ACTIVE
    assert rec.transitions == []


async def test_initial_unknown_then_first_live_status(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Unavailable at startup without restored status: UNKNOWN, then the first
    known aggregate is confirmed immediately."""
    hass.states.async_set(AC, "unavailable")
    await setup_plugin([AC], grace=300, activation=300)
    view, rec = await attach()
    assert view.status is UNKNOWN
    hass.states.async_set(AC, "cool")
    await hass.async_block_till_done()
    assert view.status is ACTIVE
    assert rec.statuses == [ACTIVE]


async def test_restored_status_held_for_grace(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """After a restart, the restored status covers unavailable sources for the grace period."""
    mock_restore_cache(hass, [State("binary_sensor.bathroom_inhibition", "on")])
    hass.states.async_set(AC, "unavailable")
    await setup_plugin([AC], grace=120)
    view, rec = await attach()
    assert view.status is ACTIVE
    await advance(119)
    assert view.status is ACTIVE
    await advance(1)
    assert view.status is UNKNOWN
    assert rec.statuses == [UNKNOWN]


async def test_restored_status_then_live(hass: HomeAssistant, setup_plugin, attach) -> None:
    """The first live aggregate replaces the restored status immediately."""
    mock_restore_cache(hass, [State("binary_sensor.bathroom_inhibition", "on")])
    hass.states.async_set(AC, "unavailable")
    entry = await setup_plugin([AC], grace=120, deactivation=600)
    view, rec = await attach()
    hass.states.async_set(AC, "off")
    await hass.async_block_till_done()
    assert view.status is INACTIVE
    assert entry.runtime_data.detector.pending_status is None
    assert rec.statuses == [INACTIVE]


async def test_restore_ignored_without_grace(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Grace 0: nothing is restored, unavailable sources mean UNKNOWN."""
    mock_restore_cache(hass, [State("binary_sensor.bathroom_inhibition", "off")])
    hass.states.async_set(AC, "unavailable")
    await setup_plugin([AC], grace=0)
    view, _ = await attach()
    assert view.status is UNKNOWN


async def test_restore_ignored_when_sources_known(hass: HomeAssistant, setup_plugin, attach) -> None:
    """A restored status never wins over live sources."""
    mock_restore_cache(hass, [State("binary_sensor.bathroom_inhibition", "on")])
    hass.states.async_set(AC, "off")
    await setup_plugin([AC], grace=120, deactivation=600)
    view, _ = await attach()
    assert view.status is INACTIVE


# ---------------------------------------------------------------------------
# Bypass
# ---------------------------------------------------------------------------
async def test_bypass(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Bypass on: INACTIVE immediately, sources ignored; off: delays apply again."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER], activation=30, deactivation=60)
    detector = entry.runtime_data.detector
    view, rec = await attach()
    assert view.status is ACTIVE

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.bathroom_inhibition_bypass"}, blocking=True
    )
    await hass.async_block_till_done()
    assert detector.bypass is True
    assert view.status is INACTIVE
    assert hass.states.get("switch.bathroom_inhibition_bypass").state == "on"

    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    hass.states.async_set(HEATER, "on")
    await advance(600)
    assert view.status is INACTIVE

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.bathroom_inhibition_bypass"}, blocking=True
    )
    await hass.async_block_till_done()
    assert view.status is INACTIVE
    assert detector.pending_status is ACTIVE
    await advance(30)
    assert view.status is ACTIVE
    assert rec.statuses == [INACTIVE, ACTIVE]


async def test_bypass_cancels_pending_timer(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Turning the bypass on cancels a pending activation."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER], activation=30)
    view, rec = await attach()
    hass.states.async_set(HEATER, "on")
    await advance(10)
    entry.runtime_data.detector.set_bypass(True)
    await advance(60)
    assert view.status is INACTIVE
    assert rec.transitions == []


async def test_bypass_covers_unavailable_sources(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Bypass reports INACTIVE even when every source is unavailable."""
    hass.states.async_set(AC, "unavailable")
    entry = await setup_plugin([AC])
    view, _ = await attach()
    assert view.status is UNKNOWN
    entry.runtime_data.detector.set_bypass(True)
    await hass.async_block_till_done()
    assert view.status is INACTIVE


async def test_bypass_restored(hass: HomeAssistant, setup_plugin, attach) -> None:
    """The bypass switch is restored after a restart, before the first evaluation."""
    mock_restore_cache(hass, [State("switch.bathroom_inhibition_bypass", "on")])
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    view, _ = await attach()
    assert entry.runtime_data.detector.bypass is True
    assert view.status is INACTIVE
    assert hass.states.get("switch.bathroom_inhibition_bypass").state == "on"


async def test_set_bypass_idempotent(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Setting the same bypass value twice changes nothing."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    view, rec = await attach()
    entry.runtime_data.detector.set_bypass(False)
    await hass.async_block_till_done()
    assert view.status is ACTIVE
    assert rec.transitions == []


# ---------------------------------------------------------------------------
# In-place reconfiguration (options flow)
# ---------------------------------------------------------------------------
async def _update(hass: HomeAssistant, entry, **options) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    await hass.async_block_till_done()


async def test_reconfigure_new_sources_use_delays(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """A raw change caused by new sources follows the normal delay."""
    hass.states.async_set(HEATER, "on")
    hass.states.async_set(BLOCK, "off")
    entry = await setup_plugin([HEATER], activation=30, deactivation=60)
    view, rec = await attach()
    assert view.status is ACTIVE

    await _update(hass, entry, source_entities=[BLOCK])
    assert view.status is ACTIVE
    assert entry.runtime_data.detector.pending_status is INACTIVE
    await advance(59)
    assert view.status is ACTIVE
    await advance(1)
    assert view.status is INACTIVE
    assert rec.statuses == [INACTIVE]


async def test_reconfigure_without_status_change(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Adding an inactive source while active changes nothing."""
    hass.states.async_set(HEATER, "on")
    hass.states.async_set(BLOCK, "off")
    entry = await setup_plugin([HEATER], activation=30, deactivation=60)
    view, rec = await attach()
    await _update(hass, entry, source_entities=[HEATER, BLOCK], activation_delay=5, unavailable_grace_period=10)
    await advance(600)
    assert view.status is ACTIVE
    assert rec.transitions == []
    assert entry.runtime_data.detector.config.activation_delay == 5


async def test_reconfigure_climate_modes(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Changing the inhibiting HVAC modes re-evaluates the climate sources."""
    hass.states.async_set(AC, "cool")
    entry = await setup_plugin([AC], deactivation=20)
    view, rec = await attach()
    assert view.status is ACTIVE
    await _update(hass, entry, climate_hvac_modes=["heat"])
    await advance(20)
    assert view.status is INACTIVE
    assert rec.statuses == [INACTIVE]


async def test_reconfigure_moves_running_timer_deadline(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """A running timer keeps its start; its deadline follows the new delay."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER], activation=30, deactivation=60)
    view, rec = await attach()
    hass.states.async_set(HEATER, "on")
    await advance(10)

    await _update(hass, entry, activation_delay=15)
    await advance(4)
    assert view.status is INACTIVE
    await advance(1)
    assert view.status is ACTIVE

    hass.states.async_set(HEATER, "off")
    await advance(10)
    await _update(hass, entry, deactivation_delay=120)
    await advance(100)
    assert view.status is ACTIVE
    await advance(10)
    assert view.status is INACTIVE
    assert rec.statuses == [ACTIVE, INACTIVE]


async def test_reconfigure_elapsed_deadline_confirms_now(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Shortening a delay below the elapsed time confirms at once."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER], activation=30)
    view, rec = await attach()
    hass.states.async_set(HEATER, "on")
    await advance(10)
    await _update(hass, entry, activation_delay=5)
    assert view.status is ACTIVE
    assert entry.runtime_data.detector.pending_status is None
    assert rec.statuses == [ACTIVE]


async def test_reconfigure_grace_period(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """The grace deadline follows a changed grace period; removing the
    unavailable source ends the hold without a transition."""
    hass.states.async_set(AC, "off")
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([AC, HEATER], grace=300)
    view, rec = await attach()
    hass.states.async_set(AC, "unavailable")
    await advance(60)
    await _update(hass, entry, unavailable_grace_period=100)
    await advance(39)
    assert view.status is INACTIVE
    await advance(1)
    assert view.status is UNKNOWN

    await _update(hass, entry, source_entities=[HEATER])
    assert view.status is INACTIVE
    assert rec.statuses == [UNKNOWN, INACTIVE]


async def test_reconfigure_with_bypass(hass: HomeAssistant, setup_plugin, attach) -> None:
    """While bypassed, a reconfiguration keeps INACTIVE."""
    hass.states.async_set(HEATER, "off")
    hass.states.async_set(BLOCK, "on")
    entry = await setup_plugin([HEATER])
    view, rec = await attach()
    entry.runtime_data.detector.set_bypass(True)
    await _update(hass, entry, source_entities=[BLOCK])
    assert view.status is INACTIVE
    assert rec.transitions == []


async def test_reconfigure_before_start(hass: HomeAssistant) -> None:
    """A detector not started yet only stores the new config."""
    from custom_components.vtherm_inhibition.detector import InhibitionDetector
    from custom_components.vtherm_inhibition.models import InhibitionConfig

    detector = InhibitionDetector(hass, InhibitionConfig.from_entry({"source_entities": [HEATER]}), "x")
    new = InhibitionConfig.from_entry({"source_entities": [BLOCK]})
    detector.async_reconfigure(new)
    assert detector.config is new
    assert detector.status is UNKNOWN


# ---------------------------------------------------------------------------
# Grace period measured per unavailability episode (review item 6)
# ---------------------------------------------------------------------------
async def test_grace_not_restarted_by_flapping(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """unavailable -> other value -> unavailable: the grace runs from the first dropout."""
    hass.states.async_set(AC, "off")
    await setup_plugin([AC], grace=120, activation=60)
    view, rec = await attach()

    hass.states.async_set(AC, "unavailable")
    await advance(50)
    hass.states.async_set(AC, "heat")  # raw ACTIVE, not confirmed: episode continues
    await advance(10)
    hass.states.async_set(AC, "unavailable")
    await advance(59)
    assert view.status is INACTIVE
    await advance(1)
    assert view.status is UNKNOWN
    assert rec.statuses == [UNKNOWN]


async def test_grace_episode_ends_when_confirmed_again(hass: HomeAssistant, setup_plugin, attach, advance) -> None:
    """Back to the confirmed status: the next dropout gets a full grace period."""
    hass.states.async_set(AC, "off")
    entry = await setup_plugin([AC], grace=120)
    view, _rec = await attach()
    hass.states.async_set(AC, "unavailable")
    await advance(30)
    hass.states.async_set(AC, "off")
    await advance(70)
    assert entry.runtime_data.detector.unavailable_since is None
    hass.states.async_set(AC, "unavailable")
    await advance(119)
    assert view.status is INACTIVE
    await advance(1)
    assert view.status is UNKNOWN


async def test_grace_episode_survives_entry_reload(hass: HomeAssistant, setup_plugin, advance) -> None:
    """An entry reload during a dropout does not grant a second grace period."""
    hass.states.async_set(AC, "off")
    entry = await setup_plugin([AC], grace=120)
    hass.states.async_set(AC, "unavailable")
    await advance(60)
    assert entry.runtime_data.detector.status is INACTIVE

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.detector.status is INACTIVE
    await advance(59)
    assert entry.runtime_data.detector.status is INACTIVE
    await advance(1)
    assert entry.runtime_data.detector.status is UNKNOWN
