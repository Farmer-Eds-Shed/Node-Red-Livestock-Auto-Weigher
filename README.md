# Node-Red-Livestock-Auto-Weigher

Raspberry Pi + Node-RED auto-weigh pipeline for livestock:

- **Tru-Test S3** weigh indicator connects to the Pi via **Bluetooth Low Energy (BLE)**
- A **Python BLE bridge** (`s3_ble_scale.py`) reads S3 weight notifications using **Bleak** and forwards them to **MQTT** (Mosquitto)
- **Node-RED** consumes MQTT messages (most importantly `auto_weigh/s3/stable_weight`) to capture and process weights
- Optional animal identification via **AWR250 EID reader** over serial (panel reader planned later)

## Architecture (high level)

1. Tru-Test S3 → BLE
2. Raspberry Pi runs `s3_ble_scale.py` (BLE client)
3. `s3_ble_scale.py` publishes weight messages to Mosquitto (MQTT)
4. Node-RED subscribes (primary: `auto_weigh/s3/stable_weight`)
5. Node-RED logs/acts on stable weights (storage, dashboards, rules, etc.)

## Repository contents

- `s3_ble_scale.py` — Tru-Test S3 BLE → MQTT bridge
- `Auto-Weigher.json` — Node-RED flow export (currently includes an MQTT “S3 error” input node)
- `LICENSE`

## Prerequisites (Raspberry Pi)

- Raspberry Pi OS (or any Linux with BlueZ)
- Bluetooth stack: `bluez` / `bluetoothd` running
- Mosquitto MQTT broker (local)
- Python 3
- Node-RED (for processing / UI / storage pipelines)

Install packages:

```bash
sudo apt-get update
sudo apt-get install -y bluetooth bluez python3 python3-pip
sudo apt-get install -y mosquitto mosquitto-clients
```

Install Python dependencies:

```bash
sudo pip3 install bleak paho-mqtt
```

## Bluetooth pairing requirement (important)

On this setup, the Tru-Test S3 required **manual pairing via `bluetoothctl`**. Without pairing first, the BLE connection would fail.
First time connection only.

### Pair using `bluetoothctl`

1) Start `bluetoothctl`:

```bash
bluetoothctl
```

2) In the `bluetoothctl` prompt, run (example flow):

```text
power on
agent on
default-agent
scan on
```

Wait until you see your S3 appear (often named like `S3 123456`) and note its MAC address.

3) Pair + trust the device (replace MAC):

```text
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
scan off
quit
```

After this, `s3_ble_scale.py` should be able to connect reliably.

> Tip: If you have repeated connection issues, removing and re-pairing can help:
> `remove AA:BB:CC:DD:EE:FF`

## MQTT interface

Default base topic is:

- `auto_weigh/s3`

### Primary topic (Node-RED should subscribe to this)
- `auto_weigh/s3/stable_weight`

This is only published when the S3 data indicates the reading is stable (based on the BLE payload’s stability byte).

### Other topics (debug / informational)
- `auto_weigh/s3/weight` — every decoded measurement
- `auto_weigh/s3/state` — connection state (published **retained**)
- `auto_weigh/s3/error` — decode/connect errors

### Payloads

`auto_weigh/s3/stable_weight` and `auto_weigh/s3/weight` payload example shape:

```json
{
  "ts": "2026-03-29T12:34:56Z",
  "name": "S3 123456",
  "address": "AA:BB:CC:DD:EE:FF",
  "weight": 512.0,
  "unit": "kg",
  "stable": true,
  "raw": 10240,
  "flags": 0,
  "stability_byte": 0,
  "raw_bytes": [0,0,0,0,0,0,0,0,0,0,0]
}
```

`auto_weigh/s3/state` payload shape:

```json
{
  "ts": "2026-03-29T12:34:56Z",
  "name": "S3 123456",
  "address": "AA:BB:CC:DD:EE:FF",
  "status": "subscribed"
}
```

## Running the BLE bridge manually (for testing)

From the repo directory:

```bash
cd /opt/auto-weigh/Node-Red-Livestock-Auto-Weigher
python3 s3_ble_scale.py --mqtt-host 127.0.0.1 --mqtt-port 1883 --mqtt-base auto_weigh/s3
```

Optional: target a specific device

```bash
python3 s3_ble_scale.py --address "AA:BB:CC:DD:EE:FF"
```

Optional: target by name (exact match or substring)

```bash
python3 s3_ble_scale.py --name "S3 123456"
```

### Watching MQTT messages

Stable weights:

```bash
mosquitto_sub -h 127.0.0.1 -t 'auto_weigh/s3/stable_weight' -v
```

Debug stream:

```bash
mosquitto_sub -h 127.0.0.1 -t 'auto_weigh/s3/#' -v
```

## Run `s3_ble_scale.py` as a systemd service (recommended)

This sets the bridge to start on boot and restart if it crashes.

### 1) Create a dedicated service user

```bash
sudo useradd --system --home /opt/auto-weigh --create-home --shell /usr/sbin/nologin auto-weigh
sudo usermod -aG bluetooth auto-weigh
```

### 2) Ensure repo lives here

- `/opt/auto-weigh/Node-Red-Livestock-Auto-Weigher`

and the script is executable:

```bash
sudo chmod +x /opt/auto-weigh/Node-Red-Livestock-Auto-Weigher/s3_ble_scale.py
```

### 3) Create the systemd unit file

Create:

- `/etc/systemd/system/s3-ble-scale.service`

With contents:

```ini
[Unit]
Description=Tru-Test S3 BLE -> MQTT bridge (Node-Red Livestock Auto Weigher)
After=network-online.target mosquitto.service bluetooth.service
Wants=network-online.target
Requires=bluetooth.service

[Service]
Type=simple
User=auto-weigh
WorkingDirectory=/opt/auto-weigh/Node-Red-Livestock-Auto-Weigher
ExecStart=/usr/bin/env python3 /opt/auto-weigh/Node-Red-Livestock-Auto-Weigher/s3_ble_scale.py --mqtt-host 127.0.0.1 --mqtt-port 1883 --mqtt-base auto_weigh/s3
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 4) Enable + start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now s3-ble-scale.service
```

### 5) Check status + logs

```bash
sudo systemctl status s3-ble-scale.service -n 50 --no-pager
journalctl -u s3-ble-scale.service -f
```

## Node-RED

### Import flow
Import `Auto-Weigher.json` into Node-RED:

- Node-RED menu → **Import** → **Clipboard**
- Paste the JSON from `Auto-Weigher.json`

Current flow notes:
- `Auto-Weigher.json` includes an MQTT-in node subscribed to `auto_weigh/s3/error` (debug/error handling).

### Recommended subscription
For weighing logic, subscribe to:

- `auto_weigh/s3/stable_weight`

Use `auto_weigh/s3/state` and `auto_weigh/s3/error` for health monitoring and troubleshooting.

## EID reader (AWR250)

Planned/optional:
- AWR250 EID reader over serial (for now)
- Panel reader planned later

(Implementation details TBD: port, baud rate, message format, mapping to animal IDs.)

## Troubleshooting

### No BLE device found
- Ensure Bluetooth is enabled: `sudo systemctl status bluetooth`
- Ensure the S3 is powered on and advertising
- Ensure you have paired the device (see **Bluetooth pairing requirement** above)
- Try pinning the device with `--address`

### Script connects but no weight messages
- Verify the S3 is actively producing weight notifications
- Subscribe to `auto_weigh/s3/#` and watch for `state`, `error`, and `weight`

### Permissions
- Ensure the service user is in the `bluetooth` group:
  - `groups auto-weigh`
- If needed, temporarily run the script manually as your main user to confirm BLE works.

## License
See `LICENSE`.
