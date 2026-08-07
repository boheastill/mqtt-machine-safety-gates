"""MQTT front door for the supervisor.

An app — a phone, a browser, another service — publishes JSON commands to
`machine/cmd`. This bridge hands each one to `Supervisor.submit()`, which is the
only path to the machine, and publishes the verdict. Nothing on the network can
reach the axis except through the gates.

    machine/cmd        <- commands in  {"type": "move_to", "target_mm": 400}
    machine/telemetry  -> state out, ~10 Hz
    machine/verdict    -> one message per command: allowed/refused + why
    machine/event      -> safe-stops fired by the watchdogs

Works against any broker (Mosquitto, EMQX, HiveMQ). Nothing here is
broker-specific, and no credentials are required for a local broker:

    docker run --rm -p 1883:1883 eclipse-mosquitto
    python3 -m machine_gates.mqtt_bridge --broker localhost

The point worth copying: the transport is replaceable and the gates are not.
Reading a command off MQTT does not make it trusted — the bridge has no power
the gates don't grant it.
"""
from __future__ import annotations

import argparse
import json
import threading
import time

from .machine import Supervisor, new_supervisor

TOPIC_CMD = "machine/cmd"
TOPIC_TELEMETRY = "machine/telemetry"
TOPIC_VERDICT = "machine/verdict"
TOPIC_EVENT = "machine/event"


class MqttBridge:
    def __init__(self, supervisor: Supervisor, host: str = "localhost", port: int = 1883,
                 prefix: str = "") -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - dependency guidance
            raise SystemExit(
                "paho-mqtt is not installed. Either `pip install paho-mqtt`, or run "
                "`python3 run_demo.py` which needs no broker and no dependencies."
            ) from exc

        self.sup = supervisor
        self.host, self.port = host, port
        self.p = prefix
        self._stop = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def t(self, topic: str) -> str:
        return f"{self.p}{topic}"

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(self.t(TOPIC_CMD))
        print(f"connected to {self.host}:{self.port} — listening on {self.t(TOPIC_CMD)}")

    def _on_message(self, client, userdata, msg):
        try:
            command = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(command, dict):
                raise ValueError("payload is not a JSON object")
        except Exception as exc:
            # Malformed input is a refusal, never a crash and never a guess.
            client.publish(self.t(TOPIC_VERDICT), json.dumps(
                {"allowed": False, "reason": f"unparseable command: {exc}"}))
            return

        # The heartbeat rides on traffic: an app that is talking to us is alive.
        if command.get("type") == "heartbeat":
            self.sup.feed_heartbeat()
            return

        verdict = self.sup.submit(command)
        client.publish(self.t(TOPIC_VERDICT), json.dumps({
            "command": command,
            "allowed": verdict.allowed,
            "consequence": verdict.consequence.name,
            "reason": verdict.reason,
            "checks_passed": list(verdict.checks),
        }))

    def _loop_machine(self) -> None:
        """Run the machine and publish what it is doing."""
        last_pub = 0.0
        while not self._stop.is_set():
            self.sup.feed_sensor()          # a real rig gets this from the encoder
            reason = self.sup.tick(0.02)
            if reason:
                self.client.publish(self.t(TOPIC_EVENT), json.dumps(
                    {"event": "safe_stop", "reason": reason}))
            now = time.monotonic()
            if now - last_pub >= 0.1:
                self.client.publish(self.t(TOPIC_TELEMETRY),
                                    json.dumps(self.sup.telemetry()))
                last_pub = now
            time.sleep(0.02)

    def run(self) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        threading.Thread(target=self._loop_machine, daemon=True).start()
        try:
            self.client.loop_forever()
        except KeyboardInterrupt:
            self._stop.set()
            self.client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description="Expose the gated machine over MQTT.")
    ap.add_argument("--broker", default="localhost", help="broker host (default: localhost)")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="", help="topic prefix, e.g. 'cell1/'")
    args = ap.parse_args()
    MqttBridge(new_supervisor(), args.broker, args.port, args.prefix).run()


if __name__ == "__main__":
    main()
