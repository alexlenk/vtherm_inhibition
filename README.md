# VTherm Inhibition

[Lire cette documentation en français](README-fr.md)

Home Assistant plugin that lets a [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm) **step back while another device takes over** the room: an electric heater running on PV surplus, an air conditioner heating or cooling, a person heating manually at the valve...

It is the inhibition plugin of [issue #2087](https://github.com/jmcollin78/versatile_thermostat/issues/2087) ("controlled VTherm inhibition", design v2). The plugin owns the whole business logic: sources, delays, grace period, bypass, status, lifecycle and the entry-stop option. It tells Versatile Thermostat what to do through the two operations of the VTherm API, `set_inhibition(stop_underlyings)` and `unset_inhibition()`. The Versatile Thermostat core then resolves the effective HVAC mode to `OFF` (reason `hvac_off_inhibition`), stops regulating and repairing, blocks every command to its underlying devices (except the optional entry stop), and when the inhibition is released it reconciles the devices with the **current** requested state. The plugin never commands the underlying devices itself, and never changes the requested mode, preset or setpoint.

## How It Works

For each configured VTherm, the plugin watches one or more **source** entities and computes a status:

| Status | Meaning | Call to Versatile Thermostat |
| --- | --- | --- |
| `active` | Another device has taken over (confirmed). | `set_inhibition(stop_underlyings)` |
| `inactive` | No source is active (confirmed). | `unset_inhibition()` |
| `unknown` | The plugin cannot tell (sources unavailable for longer than the grace period). | `set_inhibition(stop_underlyings)`: the VTherm stays inhibited until `inactive` is confirmed. |

Only **confirmed** statuses lead to a call; `unset_inhibition` is only called once `inactive` is confirmed.

### Sources

| Source domain | Inhibits when |
| --- | --- |
| `binary_sensor`, `input_boolean`, `switch` | its state is `on` |
| `climate` | its HVAC mode is one of the *inhibiting HVAC modes* (default: `heat`, `cool`, `heat_cool`, `auto`, `dry`; i.e. everything except `off` and `fan_only`) |

A source that is missing, `unavailable` or `unknown` is *unavailable*; any other state of an on/off source (neither `on` nor `off`) is also treated as unavailable.

Sources that would feed back into the plugin are refused by the configuration: the VTherm itself, every entity it controls (underlyings, valve opening/closing, calibration and synchronisation entities) and the plugin's own entities (inhibition sensors, bypass switches). A renamed source is followed automatically; a source that no longer exists raises a repair issue (**Settings > System > Repairs**) until it is back or replaced.

### Aggregation rule (several sources)

Sources are combined with a three-valued **OR**:

1. at least one available source is active → `active`, even if other sources are unavailable;
2. otherwise, at least one source is unavailable → `unknown` (subject to the grace period below);
3. otherwise (every source available and inactive) → `inactive`.

### Delays (debounce)

- **Activation delay**: the aggregate must stay `active` for this long before `active` is confirmed.
- **Deactivation delay**: the aggregate must stay `inactive` for this long before `inactive` is confirmed.
- Any change of the aggregate cancels the running timer; if the new aggregate differs from the confirmed status a new timer starts from zero. Going back to the confirmed status before the timer ends (flapping) cancels it without any call.
- Attribute-only updates of a source (same state) do not restart a timer.
- A zero delay confirms immediately.

### Unavailable grace period

HomeKit bridges, Zigbee or Wi-Fi devices often drop to `unavailable` for a few seconds. When the aggregate becomes `unknown`, the plugin **keeps the last confirmed status** for the *unavailable grace period*; only after it expires is `unknown` confirmed (and the VTherm inhibited). Without it, every short dropout would inhibit the VTherm and, with the entry stop, turn its devices off. Set the grace period to `0` to confirm `unknown` at once; then **any** blip of a source inhibits the VTherm immediately (with the entry stop, its devices are turned off), including a source that is renamed (its old entity disappears before the new one exists) or that comes up slowly after a Home Assistant restart. Keep a grace period of a few seconds at least unless you really want that.

The grace period is measured from the **first** unavailability of an *unavailability episode*. The episode only ends once the aggregate is known again **and** confirmed (for example the source comes back with the confirmed status, or a new status has passed its delay). A source flapping between `unavailable` and another value therefore cannot hold a stale status beyond the grace period, and an entry reload or a restart during the episode does not grant a second grace period (the episode start is saved with the inhibition binary sensor). If the sources come back before, nothing happens (same status) or the normal activation/deactivation delay starts from the moment they came back (different status). Recovery from a confirmed `unknown` also uses the activation/deactivation delay.

### Startup, restart and reload

- The **first** known aggregate after the plugin starts (Home Assistant start, entry reload) is confirmed **immediately**, without the activation/deactivation delay. Delays apply to every later change.
- If the sources are not available yet at that moment, the status is `unknown`, unless the grace period is not `0` and a status was confirmed before the restart: that **restored** status (saved by the inhibition binary sensor) is then kept for at most the grace period, counted from the plugin start, or from the start of the unavailability episode that was already running before the reload/restart. The first live aggregate replaces it immediately.
- The bypass switch keeps its state across restarts.
- A restart or a reload never releases an inhibited VTherm by itself: the restored (or first evaluated) status is applied again, and `unset_inhibition` is only called once `inactive` is confirmed. See [Calls to Versatile Thermostat](#calls-to-versatile-thermostat).

### Bypass

Each VTherm gets a **bypass switch**. While it is on, every source is ignored and the status is a confirmed `inactive` **immediately** (pending timers are cancelled, unavailable sources do not matter): the VTherm is released and regulates normally. When it is turned off, the sources are evaluated again and the normal delays apply (for example, an active source is confirmed after the activation delay).

### Entry stop (`stop_underlyings`)

| Value | Behavior when the inhibition starts |
| --- | --- |
| on (default) | The VTherm steps back and turns its underlying devices off once. |
| off | The VTherm steps back (effective mode `OFF`, no regulation, no repair, no command) but sends nothing: the devices are left as they are, e.g. a valve a person is controlling by hand. |

The option is passed to `set_inhibition` when an inhibition starts. Changing it while the VTherm is inhibited makes no call: it applies from the next inhibition.

## Calls to Versatile Thermostat

- The VTherm is identified by its stable unique ID and resolved to its **current** entity ID at every call: renaming the VTherm entity is followed (the entry is updated). If no VTherm with that unique ID is registered (deleted, or not yet created), the VTherm is considered unavailable: the stored entity ID is never used instead, as it could now designate another entity.
- Calls are made once the VTherm entity exists and the VTherm has restored its state at startup (the core refuses calls before). The plugin does not wait for the VTherm to be ready (every underlying reporting a state): the core holds the entry stop and the exit reconciliation until then. A VTherm that was inhibited before a restart stays inhibited through its restored state (no second entry stop); one that was not starts normally until the plugin's `set` arrives (it checks at each VTherm state change and every 5 seconds). While the plugin waits, `waiting_for_vtherm_start` is `true`; while the VTherm entity does not exist, the pending call is shown by the `retry_pending` attribute and made as soon as it appears.
- Each time the VTherm becomes available (Home Assistant start, VTherm reload) and each time the plugin entry is set up (reload, restart), the call matching the current status is made again: `set_inhibition` is idempotent in the core (no second stop) and `unset_inhibition` does nothing outside an inhibition.
- Statuses asking for the same call (`active` ↔ `unknown`) make no new call.
- The plugin also checks the inhibition state the VTherm publishes (`specific_states.inhibition`) against its last successful call. When `unset_inhibition` reaches a VTherm that has not finished starting, the core accepts it and reconciles later; if that later reconciliation fails, the VTherm is inhibited again. The plugin then sees an inhibited VTherm although it confirmed `inactive`, and retries `unset_inhibition` like a failed call. Likewise a VTherm that reports itself not inhibited after a successful `set_inhibition` is set again.
- Calls never overlap: a status change during a call is applied right after it.
- **Failures and retries**: when a call fails (for example the core could not reconcile the devices when leaving the inhibition: it raises and keeps the VTherm inhibited), it is retried after 30 s, 60 s, 120 s, then every 5 minutes, as long as the status still asks for the same call. A status change cancels the retry and makes the new call. If `unset_inhibition` still fails after the 3rd retry, a repair issue tells that the VTherm stays inhibited; it disappears on success or when the status changes. A Versatile Thermostat without inhibition support (the VTherm entity has no inhibition operations) is reported by a repair issue and the call is made again when the VTherm restarts; any other error, including an error inside the core, is retried.

## Prerequisites

- Home Assistant 2026.7.1 or later.
- [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat): **the first Versatile Thermostat release that includes inhibition (#2087)** ([#2101](https://github.com/jmcollin78/versatile_thermostat/pull/2101)), not released yet. Older releases do not support inhibition.
- `vtherm_api` **≥ 0.6.1** (released on PyPI, installed automatically).

With an older Versatile Thermostat the entries load, but the core refuses the calls: a repair issue explains the minimum versions and the VTherms are left untouched.

## Installation

### HACS

1. In HACS, add `https://github.com/alexlenk/vtherm_inhibition` as a custom repository of type *Integration*.
2. Install `VTherm Inhibition`.
3. Restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration** and add `VTherm Inhibition`.

### Manual Installation

1. Copy [custom_components/vtherm_inhibition](custom_components/vtherm_inhibition) to your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add `VTherm Inhibition` from **Settings > Devices & services**.

### Upgrading from 0.1.x

Entries are migrated automatically: the former *physical stop policy* `turn_off` (or no value) becomes `stop_underlyings: on`, `do_not_turn_off` becomes `stop_underlyings: off`.

## Configuration

Add one integration entry **per VTherm**: first select the Versatile Thermostat, then its sources and settings. Everything except the VTherm can be changed later with **Configure**.

| Option (key) | Type | Default | Description |
| --- | --- | --- | --- |
| Versatile Thermostat (`target_entity`) | climate entity of `versatile_thermostat` | required | The VTherm to inhibit. Stored by its stable unique ID (`vtherm_unique_id`) and entity ID (`vtherm_entity_id`, kept up to date). One entry per VTherm. |
| Sources (`source_entities`) | list of `binary_sensor` / `input_boolean` / `switch` / `climate` | required, at least one | OR-combined sources. The VTherm itself is refused. |
| Inhibiting HVAC modes (`climate_hvac_modes`) | list among `heat`, `cool`, `heat_cool`, `auto`, `dry`, `fan_only` | `heat`, `cool`, `heat_cool`, `auto`, `dry` | Used for `climate` sources only. |
| Activation delay (`activation_delay`) | seconds, 0–86400 | `30` | Debounce before `active` is confirmed. |
| Deactivation delay (`deactivation_delay`) | seconds, 0–86400 | `60` | Debounce before `inactive` is confirmed. |
| Unavailable grace period (`unavailable_grace_period`) | seconds, 0–86400 | `300` | How long the last confirmed status is kept while sources are unavailable. |
| Turn the devices off when the inhibition starts (`stop_underlyings`) | boolean | on | Entry stop, see above. |

Saving the options applies them **in place**, without reloading the entry or the VTherm. Nothing is called unless the confirmed status changes.

### Examples

- **Electric heater on PV surplus in the bathroom**: source `climate.bathroom_electric_heater_heater`, inhibiting modes `heat`, entry stop on.
- **Room with an air conditioner** (heats on cheap/solar power, cools in the evening): source `climate.<room>_ac`, default inhibiting modes (`heat`, `cool`, `heat_cool`, `auto`, `dry`), entry stop on.
- **Office where someone heats manually at the valve**: an `input_boolean` (or any binary sensor detecting the manual mode) as source, entry stop off so that the valve is not turned off.
- **Replacing a "heating blocked" template binary sensor used as VTherm window sensor**: use that binary sensor (or directly the climate entities it was built from) as a source here and give the VTherm window feature back to a real window contact or to the automatic window detection.

## Entities

For each configured VTherm, attached to the VTherm device (or to a device named after the entry when the VTherm has no device):

| Entity | Unique ID | Description |
| --- | --- | --- |
| `binary_sensor.<vtherm>_inhibition` | `<vtherm_unique_id>_inhibition` | `on` = confirmed `active`, `off` = confirmed `inactive`, `unknown` = `unknown`. |
| `switch.<vtherm>_inhibition_bypass` | `<vtherm_unique_id>_inhibition_bypass` | Bypass (configuration entity). |

Attributes of the binary sensor:

| Attribute | Description |
| --- | --- |
| `status` | Confirmed status: `active`, `inactive` or `unknown`. |
| `raw_status` | Current aggregate, before delays. |
| `stop_underlyings` | Entry stop option passed to the next `set_inhibition`. |
| `bypass` | Bypass switch state. |
| `active_sources` | Sources currently active. |
| `sources` | Per source: its `state` and its `contribution` (`active`, `inactive`, `unavailable`). |
| `pending_status`, `pending_until` | Status a running timer will confirm, and when (UTC). `null` when no timer runs. |
| `last_change` | Time of the last confirmed change. |
| `unavailable_since` | Start of the current unavailability episode (UTC), `null` outside one. |
| `missing_sources` | Configured sources that no longer exist. |
| `vtherm_entity_id` | Current entity ID of the targeted VTherm. |
| `vtherm_available` | `true` while the VTherm entity exists. |
| `last_api_call`, `last_api_call_at` | Last call made (`set` or `unset`) and when (UTC). |
| `last_error` | Error of the last call, `null` once a call succeeded. |
| `retry_pending` | `true` while the call matching the status has not succeeded yet (VTherm not available, or retry scheduled). |
| `next_retry_at` | When the next retry runs (UTC), `null` when none is scheduled. |
| `waiting_for_vtherm_start` | `true` while the call waits for the VTherm to restore its state at startup. |

Versatile Thermostat publishes its own side under the `specific_states.inhibition` attribute of the VTherm (inhibited, command gate, entry stop requested/completed, last error code).

## Lifecycle

- **Options change**: applied in place to the running entry. A raw status change caused by the new sources or HVAC modes follows the normal rules (activation/deactivation delay, grace period). A timer already running keeps its start time and its deadline follows the new delay (confirmed at once if already elapsed). A change of `stop_underlyings` applies from the next inhibition. Entities reflect the new settings immediately.
- **Entry reload or Home Assistant restart**: unloading makes no call, so an inhibited VTherm stays inhibited. When the entry is set up again it restores its status and bypass and makes the matching call again once the VTherm is available.
- **Entry unloaded or disabled**: no call is made; an inhibited VTherm **stays inhibited** (unloading never means `inactive`). Turn the bypass on before disabling the entry if the VTherm must resume, or delete the entry.
- **Entry deleted**: `unset_inhibition` is called, the VTherm is released and works as before If this call fails (for example Versatile Thermostat is not loaded), a repair issue says that the VTherm may stay inhibited and how to release it (see below); it disappears once an entry for this VTherm is set up again.
- **VTherm reload**: the core restores its inhibition state; the plugin makes the matching call again as soon as the VTherm entity is back.

### Recovery if the plugin is removed while a VTherm is inhibited

Versatile Thermostat keeps an inhibition across restarts until a plugin releases it. If the plugin's code is removed (uninstalled) while one of its entries still inhibits a VTherm, that VTherm stays inhibited. To release it: reinstall the plugin, restart Home Assistant, then delete the plugin's entry for that VTherm; the deletion calls `unset_inhibition`. If the entry was already deleted while Versatile Thermostat was not running (repair issue *VTherm possibly still inhibited*), add an entry for that VTherm again: it releases the VTherm as soon as its sources are inactive (or turn its bypass on), then delete it. Deleting the entries **before** uninstalling the plugin avoids this.

## Limitations

- One inhibition entry per VTherm; combine several sources in one entry instead.
- The plugin does not command any device: it only tells Versatile Thermostat when to step back and when to resume.

## Development

```bash
pip install -r requirements_test.txt
pytest -q tests
```

The unit tests use a fake core API that records the `set_inhibition` / `unset_inhibition` calls.

### End-to-end tests

`tests_e2e/` runs this plugin together with the **real** Versatile Thermostat core (inhibition support, issue #2087) in the Home Assistant test harness: real VTherms (over_climate, over_switch TPI) with mock underlying devices, real plugin entries, timers, reloads and simulated restarts; the service calls reaching the devices are counted. The core is taken from a local checkout (its repository or its `custom_components` directory); its test requirements (`requirements_dev.txt` of the core) must be installed in the same environment:

```bash
VTHERM_CORE_PATH=/path/to/versatile_thermostat pytest -q tests_e2e
```

Without `VTHERM_CORE_PATH` (or if it does not point to a `versatile_thermostat` checkout) these modules are skipped. In CI, `.github/workflows/e2e.yml` runs them against `jmcollin78/versatile_thermostat` `main` by default (the core repository and ref can be chosen when running it manually); it does not block the CI until a Versatile Thermostat release with inhibition exists.
