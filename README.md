# node-tank

Native MQTT node for ADS1115-based resistive tank senders (fresh/grey
water). Ported from Victron's `dbus-ads1115` Venus OS driver per
`hub/docs/porting-dbus-to-mqtt-node.md`: keeps the voltage → resistance
→ percentage calibration math and layered config pattern, drops every
D-Bus/Venus-OS/GUI layer. Calibration is config-file only.

This repo is also the reference shape for other `node-*` repos in the
RenewVan ecosystem (per the **Node** term in
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
- `temperature_c` — number, degrees Celsius (optional, only if DS18B20 is configured per tank)

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

### DS18B20 water-temperature sensor (optional per tank)

One-Wire digital thermometer for measuring tank water temperature.

**1-Wire enablement on Raspberry Pi**:
1. Add `dtoverlay=w1-gpio` to `/boot/config.txt` (or `/u-boot/config.txt` / `/mnt/boot/config.txt` on Pi 4; check all three)
2. Optionally override the default GPIO pin (4): `dtoverlay=w1-gpio,gpiopin=x`
3. Reboot
4. Verify: `ls /sys/bus/w1/devices/` should list `28-<rom-id>` directories for each sensor

**Wiring: externally powered (recommended, 3-wire)**:

```
DS18B20 (TO-92 package):
  Pin 1 (GND)  → GND (Pi pin 6, 9, 14, 20, 25, 30, 34, or 39)
  Pin 2 (DQ)   → GPIO4 (Pi pin 7) with external 4.7kΩ pull-up resistor to 3.3V
  Pin 3 (VDD)  → 3.3V (Pi pin 1 or 17)
```

The **external 4.7 kΩ pull-up resistor is required**. The Pi's internal GPIO pull-ups (~50 kΩ) are too weak for reliable 1-Wire communication.

**Wiring: parasitic power (optional, 2-wire, less reliable)**:

If space is constrained, VDD can be tied to GND (chip powered via pull-up during conversion):
```
  Pin 1 (GND)  → GND
  Pin 2 (DQ)   → GPIO4 (Pi pin 7) with external 4.7kΩ pull-up to 3.3V
  Pin 3 (VDD)  → GND
```
Add `pullup="y"` to the overlay for stronger pull-up: `dtoverlay=w1-gpio,pullup="y"`

More error-prone; externally powered is preferred.

**Configuration**:
1. Discover sensor ROM ID: `ls /sys/bus/w1/devices/` → note the `28-<rom-id>` directory
2. In `config.default.ini` or `config.ini`, add `temp_sensor_id = 28-<rom-id>` to the tank section:
   ```ini
   [tank.fresh]
   temp_sensor_id = 28-0521a2e0cfff
   ```
3. Restart the node; it will publish `temperature_c` live (same interval as `level_pct`)

Omit `temp_sensor_id` to disable temperature sensing for a tank.

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
