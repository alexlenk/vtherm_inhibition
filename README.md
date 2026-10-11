# VTherm Inhibition

[Lire cette documentation en français](README-fr.md)

Home Assistant plugin that lets a [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm) **step back while another device takes over** the room: an electric heater running on PV surplus, an air conditioner heating or cooling, a person heating manually at the valve...

It implements the inhibition provider of [issue #2087](https://github.com/jmcollin78/versatile_thermostat/issues/2087) ("controlled VTherm withdrawal"). The plugin only **detects** the condition and reports a confirmed status; the Versatile Thermostat core does the rest: it resolves the effective HVAC mode to `OFF` (reason `hvac_off_inhibition`), suspends regulation and repair, sends at most one physical stop (depending on the policy below), and resumes the **current** requested state once the inhibition ends. The plugin never commands the VTherm or its underlying devices, and never changes the requested mode, preset or setpoint.

## How It Works

For each configured VTherm, the plugin watches one or more **source** entities and computes a status:

| Status | Meaning | Effect in VTherm |
| --- | --- | --- |
| `active` | Another device has taken over (confirmed). | Withdrawal, `OFF`, at most one stop. |
| `inactive` | No source is active (confirmed). | Normal operation. |
| `unknown` | The plugin cannot tell (sources unavailable for longer than the grace period, plugin stopped or reloading). | Safe withdrawal, no resumption until `inactive` is confirmed. |

### Sources

| Source domain | Inhibits when |
| --- | --- |
| `binary_sensor`, `input_boolean`, `switch` | its state is `on` |
| `climate` | its HVAC mode is one of the *inhibiting HVAC modes* (default: `heat`, `cool`, `heat_cool`, `auto`, `dry`; i.e. everything except `off` and `fan_only`) |

A source that is missing, `unavailable` or `unknown` is *unavailable*; any other state of an on/off source (neither `on` nor `off`) is also treated as unavailable.

### Aggregation rule (several sources)

Sources are combined with a three-valued **OR**:

1. at least one available source is active → `active`, even if other sources are unavailable;
2. otherwise, at least one source is unavailable → `unknown` (subject to the grace period below);
3. otherwise (every source available and inactive) → `inactive`.

### Delays (debounce)

Only confirmed statuses are reported to VTherm; a raw change and a running timer are never reported.

- **Activation delay**: the aggregate must stay `active` for this long before `active` is confirmed.
- **Deactivation delay**: the aggregate must stay `inactive` for this long before `inactive` is confirmed.
- Any change of the aggregate cancels the running timer; if the new aggregate differs from the confirmed status a new timer starts from zero. Going back to the confirmed status before the timer ends (flapping) cancels it without any report.
- Attribute-only updates of a source (same state) do not restart a timer.
- A zero delay confirms immediately.

### Unavailable grace period

HomeKit bridges, Zigbee or Wi-Fi devices often drop to `unavailable` for a few seconds. When the aggregate becomes `unknown`, the plugin **keeps the last confirmed status** for the *unavailable grace period*; only after it expires is `unknown` reported. If the sources come back before, nothing is reported (same status) or the normal activation/deactivation delay starts from the moment they came back (different status). With a grace period of `0`, `unknown` is reported immediately. Recovery from a reported `unknown` also uses the activation/deactivation delay.

### Startup, restart and reload

- The **first** known aggregate after the plugin starts (Home Assistant start, entry reload) is confirmed **immediately**, without the activation/deactivation delay, so VTherm can resume (or stay withdrawn) without waiting. Delays apply to every later change.
- If the sources are not available yet at that moment, the status is `unknown`, unless the grace period is not `0` and a status was confirmed before the restart: that **restored** status (saved by the inhibition binary sensor) is then kept for at most the grace period, counted from the plugin start. The first live aggregate replaces it immediately.
- The bypass switch keeps its state across restarts.

### Bypass

Each VTherm gets a **bypass switch**. While it is on, every source is ignored and the plugin reports a confirmed `inactive` **immediately** (pending timers are cancelled, unavailable sources do not matter): VTherm regulates normally. When it is turned off, the sources are evaluated again and the normal delays apply (for example, an active source is confirmed after the activation delay).

### Physical stop policy

| Value | Behavior on inhibition |
| --- | --- |
| `turn_off` (default) | VTherm withdraws and turns its underlying devices off once. |
| `do_not_turn_off` | VTherm withdraws (effective mode `OFF`, no regulation, no repair) but sends no `turn_off`: the devices are left as they are, e.g. a valve a person is controlling by hand. |

Any missing or invalid value is treated as `turn_off`. The policy is also applied by VTherm to the precautionary stop it may send while the status is `unknown`.

## Prerequisites

- Home Assistant 2026.7.1 or later.
- [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) with inhibition provider support (issue #2087).
- `vtherm_api` version `0.6.0` or later (installed automatically).

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

## Configuration

Add one integration entry **per VTherm**: first select the Versatile Thermostat, then its sources and settings. Everything except the VTherm can be changed later with **Configure**.

| Option (key) | Type | Default | Description |
| --- | --- | --- | --- |
| Versatile Thermostat (`target_entity`) | climate entity of `versatile_thermostat` | required | The VTherm to inhibit. Stored by its stable unique ID (`vtherm_unique_id`) and entity ID (`vtherm_entity_id`). One entry per VTherm. |
| Sources (`source_entities`) | list of `binary_sensor` / `input_boolean` / `switch` / `climate` | required, at least one | OR-combined sources. The VTherm itself is refused. |
| Inhibiting HVAC modes (`climate_hvac_modes`) | list among `heat`, `cool`, `heat_cool`, `auto`, `dry`, `fan_only` | `heat`, `cool`, `heat_cool`, `auto`, `dry` | Used for `climate` sources only. |
| Activation delay (`activation_delay`) | seconds, 0–86400 | `30` | Debounce before `active` is confirmed. |
| Deactivation delay (`deactivation_delay`) | seconds, 0–86400 | `60` | Debounce before `inactive` is confirmed. |
| Unavailable grace period (`unavailable_grace_period`) | seconds, 0–86400 | `300` | How long the last confirmed status is kept while sources are unavailable. |
| On inhibition (`physical_stop_policy`) | `turn_off` / `do_not_turn_off` | `turn_off` | Physical stop policy, see above. |

Saving the options reloads the entry; while Home Assistant is running, the plugin then reloads the targeted VTherm so that it attaches the new provider.

### Examples

- **Electric heater on PV surplus in the bathroom**: source `climate.bathroom_electric_heater_heater`, inhibiting modes `heat`, policy `turn_off`.
- **Room with an air conditioner** (heats on cheap/solar power, cools in the evening): source `climate.<room>_ac`, default inhibiting modes (`heat`, `cool`, `heat_cool`, `auto`, `dry`), policy `turn_off`.
- **Office where someone heats manually at the valve**: an `input_boolean` (or any binary sensor detecting the manual mode) as source, policy `do_not_turn_off` so that the valve is not turned off.
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
| `pending_status`, `pending_until` | Status a running timer will confirm, and when (UTC). `null` when no timer runs. |
| `last_change` | Time of the last confirmed change. |
| `sources` | Per source: its `state` and its `contribution` (`active`, `inactive`, `unavailable`). |
| `bypass` | Bypass switch state. |
| `physical_stop_policy` | Effective policy. |
| `climate_hvac_modes`, `activation_delay`, `deactivation_delay`, `unavailable_grace_period` | Effective settings. |
| `vtherm_entity_id` | Targeted VTherm. |
| `provider_attached` | `true` once VTherm has attached the provider. |

## Lifecycle

- The provider factory is registered in the VTherm API when the first entry is set up and unregistered when the last entry is unloaded. It accepts a VTherm only if an entry targets its unique ID (its entity ID is used only when the VTherm runtime has no unique ID).
- At Home Assistant startup, VTherm discovers the factory itself (its `async_startup` runs after Home Assistant has started). When an entry is added, changed or re-enabled while Home Assistant is running, the plugin reloads the targeted VTherm so that it attaches the provider.
- When an entry is unloaded or reloaded, the attached provider reports `unknown` before it detaches: VTherm stays safely withdrawn until a provider confirms `inactive`. When an entry is **deleted or disabled**, the VTherm is reloaded without provider and works as before.

## Limitations

- One inhibition provider per VTherm (V1 of the VTherm design); combine several sources in one entry instead.
- The plugin does not command any device: it only tells VTherm when to step back.

## Development

```bash
pip install -r requirements_test.txt
pytest -q tests
```

The tests use a fake VTherm core that only relies on the `vtherm_api` contract.
