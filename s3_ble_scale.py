#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

from bleak import BleakClient, BleakScanner
import paho.mqtt.client as mqtt


WEIGHT_SERVICE_UUID = "0000181d-0000-1000-8000-00805f9b34fb"
WEIGHT_CHAR_UUID = "00002a9d-0000-1000-8000-00805f9b34fb"
NAME_REGEX = re.compile(r"^S3 \d{6}$")

SCAN_TIMEOUT = 10.0
RECONNECT_DELAY = 5.0

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_BASE = "auto_weigh/s3"


@dataclass
class ScaleState:
    name: str = ""
    address: str = ""
    last_weight: Optional[float] = None
    stable_weight: Optional[float] = None


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(event: str, **fields) -> None:
    msg = {"ts": now_iso(), "event": event, **fields}
    print(json.dumps(msg, separators=(",", ":")), flush=True)


def decode_weight_measurement(data: bytearray) -> dict:
    if len(data) < 3:
        raise ValueError(f"Notification too short: {len(data)} bytes")

    flags = data[0]
    raw = int.from_bytes(data[1:3], byteorder="little", signed=False)

    is_lb = bool(flags & 0x01)
    if is_lb:
        unit = "lb"
        weight = raw * 0.1
    else:
        unit = "kg"
        weight = raw * 0.05

    stability_byte = data[10] if len(data) > 10 else None

    stable = None
    if stability_byte == 0x00:
        stable = True
    elif stability_byte == 0xFF:
        stable = False

    return {
        "flags": flags,
        "raw": raw,
        "unit": unit,
        "weight": weight,
        "stability_byte": stability_byte,
        "stable": stable,
        "raw_bytes": list(data),
    }


async def find_device(name_hint: Optional[str], address_hint: Optional[str]):
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)

    if address_hint:
        for d in devices:
            if d.address.lower() == address_hint.lower():
                return d

    if name_hint:
        for d in devices:
            n = d.name or ""
            if n == name_hint or name_hint.lower() in n.lower():
                return d

    for d in devices:
        n = d.name or ""
        if NAME_REGEX.match(n):
            return d

    return None


class MqttPub:
    def __init__(self, host: str, port: int, base: str):
        self.host = host
        self.port = port
        self.base = base.rstrip("/")
        self.client = mqtt.Client()

    def start(self):
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def publish(self, topic: str, payload: dict, retain: bool = False):
        full_topic = f"{self.base}/{topic.lstrip('/')}"
        self.client.publish(full_topic, json.dumps(payload), qos=1, retain=retain)


class S3ScaleClient:
    def __init__(self, name_hint: Optional[str], address_hint: Optional[str], mqtt_pub: MqttPub) -> None:
        self.name_hint = name_hint
        self.address_hint = address_hint
        self.stop_event = asyncio.Event()
        self.state = ScaleState()
        self.mqtt = mqtt_pub

    def stop(self) -> None:
        self.stop_event.set()

    def publish_state(self, status: str, **extra):
        payload = {
            "ts": now_iso(),
            "name": self.state.name,
            "address": self.state.address,
            "status": status,
            **extra,
        }
        self.mqtt.publish("state", payload, retain=True)

    def handle_measurement(self, _char, data: bytearray) -> None:
        try:
            decoded = decode_weight_measurement(data)

            msg = {
                "ts": now_iso(),
                "name": self.state.name,
                "address": self.state.address,
                "weight": decoded["weight"],
                "unit": decoded["unit"],
                "stable": decoded["stable"],
                "raw": decoded["raw"],
                "flags": decoded["flags"],
                "stability_byte": decoded["stability_byte"],
                "raw_bytes": decoded["raw_bytes"],
            }

            self.state.last_weight = decoded["weight"]

            if decoded["stable"] is True:
                self.state.stable_weight = decoded["weight"]

            emit("weight", **msg)
            self.mqtt.publish("weight", msg)

            if decoded["stable"] is True:
                self.mqtt.publish("stable_weight", msg)

        except Exception as e:
            err = {
                "ts": now_iso(),
                "error": str(e),
                "error_type": type(e).__name__,
                "error_repr": repr(e),
                "raw_bytes": list(data),
            }
            emit("decode_error", **err)
            self.mqtt.publish("error", err)

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                emit("scan_start")
                device = await find_device(self.name_hint, self.address_hint)

                if not device:
                    emit("device_not_found", name_hint=self.name_hint, address_hint=self.address_hint)
                    self.publish_state("not_found")
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                self.state.name = device.name or "S3"
                self.state.address = device.address

                emit("device_found", name=self.state.name, address=self.state.address)
                emit("connecting", name=self.state.name, address=self.state.address)
                self.publish_state("connecting")

                async with BleakClient(device, timeout=30.0) as client:
                    emit("connected", name=self.state.name, address=self.state.address)
                    self.publish_state("connected")

                    emit("starting_subscribe", char_uuid=WEIGHT_CHAR_UUID)
                    await asyncio.wait_for(
                        client.start_notify(WEIGHT_CHAR_UUID, self.handle_measurement),
                        timeout=10.0,
                    )

                    emit("subscribe_started", char_uuid=WEIGHT_CHAR_UUID)
                    self.publish_state("subscribed")
                    emit("waiting_for_measurements")

                    while client.is_connected and not self.stop_event.is_set():
                        await asyncio.sleep(1.0)

                    emit("disconnected", name=self.state.name, address=self.state.address)
                    self.publish_state("disconnected")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                err = {
                    "ts": now_iso(),
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "error_repr": repr(e),
                }
                emit("error", **err)
                self.mqtt.publish("error", err)
                self.publish_state("error", **err)
                await asyncio.sleep(RECONNECT_DELAY)


async def amain() -> int:
    parser = argparse.ArgumentParser(description="Read BLE weights from a Tru-Test S3 and publish to MQTT")
    parser.add_argument("--name", default=None)
    parser.add_argument("--address", default=None)
    parser.add_argument("--mqtt-host", default=MQTT_HOST)
    parser.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    parser.add_argument("--mqtt-base", default=MQTT_BASE)
    args = parser.parse_args()

    mqtt_pub = MqttPub(args.mqtt_host, args.mqtt_port, args.mqtt_base)
    mqtt_pub.start()

    client = S3ScaleClient(
        name_hint=args.name,
        address_hint=args.address,
        mqtt_pub=mqtt_pub,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, client.stop)
        except NotImplementedError:
            pass

    try:
        await client.run()
    finally:
        mqtt_pub.stop()

    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
