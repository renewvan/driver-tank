# node-tank

Native MQTT node for ADS1115-based resistive tank senders (fresh/grey
water). Ported from Victron's `dbus-ads1115` Venus OS driver per
`hub/docs/porting-dbus-to-mqtt-node.md`: keeps the voltage → resistance
→ percentage calibration math and layered config pattern, drops every
D-Bus/Venus-OS/GUI layer. Calibration is config-file only.

This repo is also the reference shape for other `node-*` repos in the **renewvan** ecosystem (per the **Node** term in
`hub/docs/architecture.md`'s Terminology section: "anything that
publishes one specific device or feed onto the renewvan bus, normalized
into the device model"). A new node repo should follow the same layout:
pure, unit-testable conversion math with no I/O (`calibration.py`), a
layered `config.default.ini`/`config.ini` loader (`config.py`), a
persistent MQTT publisher with LWT (`publisher.py`), a thin publish
loop (`driver.py`), a systemd unit, and its own Dockerfile + CI
publishing a versioned image.

## Topics published

Retained, under `renewvan/tank/<fresh|grey>/`:

**Identity fields** (published once at startup):
- `fluid_type` — string (e.g., `fresh_water`, `grey_water`)
- `capacity_l` — number, liters (canonical unit; configured via `tank_capacity` + `volume_unit`)
- `alarm_direction` — `low` / `high` (optional, only if alarm is configured per tank)
- `alarm_threshold_pct` — number 0–100, level that trips `alarm_state` to `alarm` (optional, only if alarm is configured per tank)
- `alarm_restore_pct` — number 0–100, level that clears `alarm_state` back to `ok` (optional, only if alarm is configured per tank)
- `last_inspected_at` — string, ISO-8601 timestamp with local UTC offset (optional, only if set via `/set` command topic)

**Live fields** (republished on every sensor read, ~3s default):
- `level_pct` — number 0–100 (raw sender reading; alarms/full-empty latching key off this)
- `status` — `ok` / `open_circuit` / `short_circuit` (ADS1115 sensor health)
- `alarm_state` — `ok` / `alarm` (optional, only if alarm is configured per tank)
- `temperature_c` — number, degrees Celsius (optional, only if an NTC temp channel is configured per tank)

**Command topics** (unretained, for external control):
- `renewvan/tank/<id>/last_inspected_at/set` — payload: JSON string, ISO-8601 timestamp (e.g., `"2026-09-29T14:32:05-04:00"`) or bare `YYYY-MM-DD` (e.g., `"2026-09-29"`, normalized to local midnight); node validates and republishes to state topic on success

**Node liveness** (not tank-keyed):
- `renewvan/tank/health` — `online`/`offline` via MQTT LWT — deliberately 3 segments, not 4, so it can't be mistaken for a `tank` entity keyed by a fake `health`/`node` id

Payload shapes match `hub`'s `schema/tank.schema.json`.

## Wiring

### ADS1115 level sender (required)

The ADS1115 is an I2C ADC (analog-to-digital converter) that reads tank-level senders.

**I2C connection to Raspberry Pi**:
- VDD (power) → 3.3V (Pi pin 1 or 17)
- GND → GND (Pi pin 6, 9, 14, 20, 25, 30, 34, or 39)
- SDA (data) → GPIO2/SDA (Pi pin 3)
- SCL (clock) → GPIO3/SCL (Pi pin 5)
- ADDR (address select) → GND (ties I2C address to 0x48; see `config.default.ini`)

**Tank sender wiring** (voltage divider):

For each tank, the ADS1115 reads a variable resistor (the tank-level sender).
```
        +3.3V
          |
        [fixed_resistor]  (e.g., 220Ω, configured per tank)
          |
    ---[ADS1115]---  (ADC channel configured per tank)
          |
        [sender]  (variable resistor: 0Ω at FULL, ~180Ω at EMPTY for European standard)
          |
         GND
```

Each tank's calibration maps sender resistance → level %.  
Configure `sensor_min`/`sensor_max` (resistance in ohms at EMPTY/FULL) and `fixed_resistor` in `config.default.ini`.

**Enable I2C on Raspberry Pi**:
1. Run `raspi-config` → Interfacing Options → I2C → Enable
2. Reboot
3. Verify: `i2cdetect -y 1` should list your ADS1115 at address 0x48

### NTC water-temperature sensor (optional per tank)

A 10 kΩ NTC thermistor wired as a voltage divider on a **spare ADS1115 channel** — the same chip and read path as the level senders; no 1-Wire support (DS18B20 was removed in v0.8.0).

Channel convention: the temp channel pairs with the tank's level channel — level on A0 → temp on **A2**, level on A1 → temp on **A3**.

**Wiring** (10 kΩ fixed resistor + 10 kΩ NTC across the 3.3 V rail, ADC tap in the middle):

```
Thermistor low-side (default, temp_ntc_thermistor_low_side = true):
  3.3V ──[10k fixed]──┬── ADS1115 A2 (or A3)
                      └──[NTC 10k]── GND

Thermistor high-side (temp_ntc_thermistor_low_side = false):
  3.3V ──[NTC 10k]────┬── ADS1115 A2 (or A3)
                      └──[10k fixed]── GND
```

**Which orientation do I have?** Warm the thermistor with a finger and watch the tap voltage (or the published `temperature_c`): low-side wiring makes the tap voltage **fall** as it warms, high-side makes it **rise**. If readings move the wrong way, flip `temp_ntc_thermistor_low_side` in `config.ini` — no rewiring needed.

**Configuration** — in `config.ini`, set the tank's divider channel:

```ini
[tank.fresh]
temp_channel = 2
; defaults (generic 10k NTC, B=3950, 10k fixed resistor) — override per install:
; temp_ntc_nominal_ohm       = 10000
; temp_ntc_beta              = 3950
; temp_ntc_fixed_resistor_ohm = 10000
; temp_ntc_thermistor_low_side = true
```

Restart the node; it publishes `temperature_c` live (same interval as `level_pct`), converted via the beta equation `1/T = 1/T₀ + (1/B)·ln(R/R₀)`.

Omit `temp_channel` to disable temperature sensing for a tank. An open thermistor (reads >10× nominal) or short (<nominal/20) is logged as a warning and skips the publish — the tank's `status` field stays level-sender health only.

## Development

```bash
git clone git@github.com:renewvan/node-tank.git
cd node-tank
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-test.txt
```

Calibration, config, and driver-state-machine logic (flow rate, alarm
hysteresis, full/empty latching) are pure functions tested against fixture
values — no MQTT broker, ADS1115, or Raspberry Pi required for any of it;
see **Testing** below. `python -m node_tank.main` itself does need real
I2C hardware (the ADS1115), so it isn't runnable off-Pi; iterate against
the unit tests instead, and verify hardware-dependent changes on-device
per **Running**.

Before opening a PR: `pytest tests/ -v` must pass (same command CI runs),
and any change to a published field or its semantics needs a matching
`hub/schema/tank.schema.json` update in a `hub` PR — the two repos'
contracts must land together (per `hub/docs/adr/0001-compose-services-via-pinned-images-not-git-submodules.md`,
they're never coupled at build time, only at the schema-version level).

## Configuration

`config.default.ini` ships every key with a default. Copy
`config.ini.example` to `config.ini` (gitignored) and override only what
differs — MQTT broker host and each sender's calibrated `sensor_min`/
`sensor_max` resistance.

## Running

```bash
pip install -r requirements.txt
python -m node_tank.main --config config.ini
```

Install `node_tank/systemd/node-tank.service` to run at boot
(`Restart=on-failure`) on the Pi the ADS1115 is I2C-wired to.

## Testing

```bash
pip install -r requirements.txt -r requirements-test.txt
pytest tests/ -v
```

Calibration and config tests run with fixture voltages/files only — no
MQTT broker or ADS1115 hardware required.

## Releasing

Tagging a GitHub release builds and publishes
`ghcr.io/<owner>/node-tank:<tag>`, which `hub`'s deployment compose
file pins by tag (never builds from source — see
`hub/docs/adr/0001-compose-services-via-pinned-images-not-git-submodules.md`).
