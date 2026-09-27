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

- `fluid_type` — string, published once at startup (identity field)
- `capacity_l` — number, published once at startup (identity field)
- `level_pct` — number 0–100, republished every read
- `status` — `ok` / `open_circuit` / `short_circuit`, republished every read

Node liveness: `renewvan/tank/health` (`online`/`offline` via MQTT LWT) —
deliberately 3 segments, not 4, so it can't be mistaken for a `tank`
entity keyed by a fake `health`/`node` id.

Payload shapes match `hub`'s `schema/tank.schema.json`.

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
