# renewvan-tank

Native MQTT driver for ADS1115-based resistive tank senders (fresh/grey
water). Ported from Victron's `dbus-ads1115` Venus OS driver per
`hub/docs/porting-dbus-driver-to-mqtt.md`: keeps the voltage → resistance
→ percentage calibration math and layered config pattern, drops every
D-Bus/Venus-OS/GUI layer. Calibration is config-file only.

## Topics published

Retained, under `van/tank/<fresh|grey>/`:

- `fluid_type` — string, published once at startup (identity field)
- `capacity_l` — number, published once at startup (identity field)
- `level_pct` — number 0–100, republished every read
- `status` — `ok` / `open_circuit` / `short_circuit`, republished every read

Driver liveness: `van/tank/driver/status` (`online`/`offline` via MQTT LWT).

Payload shapes match `hub`'s `schema/tank.schema.json`.

## Configuration

`config.default.ini` ships every key with a default. Copy
`config.ini.example` to `config.ini` (gitignored) and override only what
differs — MQTT broker host and each sender's calibrated `sensor_min`/
`sensor_max` resistance.

## Running

```bash
pip install -r requirements.txt
python -m renewvan_tank.main --config config.ini
```

Install `renewvan_tank/systemd/renewvan-tank.service` to run at boot
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
`ghcr.io/<owner>/renewvan-tank:<tag>`, which `hub`'s deployment compose
file pins by tag (never builds from source — see
`hub/docs/adr/0001-compose-services-via-pinned-images-not-git-submodules.md`).
