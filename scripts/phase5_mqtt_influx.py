"""
Phase 5 helpers:
  - Manual MQTT→Telegraf→Influx round-trip test
  - Create Ditto MQTT target connection (devops Connectivity API)
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paho.mqtt.publish as mqtt_publish
import requests
from influxdb_client import InfluxDBClient
from requests.auth import HTTPBasicAuth

from twin.config import (
    DITTO_HTTP_URL,
    INFLUX_BUCKET,
    INFLUX_ORG,
    INFLUX_TOKEN,
    INFLUX_URL,
    MQTT_HOST,
    MQTT_PORT,
    NAMESPACE,
)

CONNECTION_NAME = "ClusterTwin MQTT events to Mosquitto"
CONNECTION_ID_FILE = ROOT / "notes" / "_connection_id.txt"
DEVOPS_AUTH = HTTPBasicAuth("devops", "foobar")

CONNECTION_BODY = {
    "name": CONNECTION_NAME,
    "connectionType": "mqtt",
    "connectionStatus": "open",
    "uri": "tcp://opentwins-mosquitto:1883",
    "sources": [],
    "targets": [
        {
            "address": f"opentwins/{NAMESPACE}/{{{{ thing:name }}}}",
            "topics": [
                (
                    "_/_/things/twin/events"
                    "?extraFields=thingId,attributes/_parents,"
                    "features/idSimulationRun/properties/value"
                )
            ],
            "authorizationContext": ["nginx:ditto"],
            "qos": 1,
        }
    ],
    "clientCount": 1,
    "failoverEnabled": True,
    "validateCertificates": False,
    "processorPoolSize": 1,
    "specificConfig": {
        "clientId": "ditto-clustertwin-mqtt",
        "cleanSession": "true",
    },
}


def manual_mqtt_test() -> str:
    """Publish one Telegraf-shaped Ditto-protocol JSON message; return marker."""
    marker = f"manual-test-{uuid.uuid4().hex[:8]}"
    payload = {
        "topic": f"{NAMESPACE}/test/things/twin/events/modified",
        "headers": {
            "content-type": "application/json",
            "correlation-id": marker,
            "ditto-originator": "nginx:ditto",
        },
        "path": "/features/cpu_utilization/properties/value",
        "value": 12.34,
        "extra": {
            "thingId": f"{NAMESPACE}:test",
            "attributes": {"_parents": f"{NAMESPACE}:rack_0"},
            "features": {
                "idSimulationRun": {"properties": {"value": marker}},
                "cpu_utilization": {"properties": {"value": 12.34}},
            },
        },
    }
    # Also include value as object form Telegraf can flatten
    payload["value"] = {
        "features": {
            "cpu_utilization": {"properties": {"value": 12.34}},
            "marker": {"properties": {"value": marker}},
        }
    }
    topic = "opentwins/test"
    mqtt_publish.single(
        topic,
        payload=json.dumps(payload),
        hostname=MQTT_HOST,
        port=MQTT_PORT,
        qos=1,
    )
    print(f"Published manual test to {topic}, marker={marker}")
    return marker


def query_influx_recent(seconds: int = 120) -> list:
    if not INFLUX_TOKEN:
        raise RuntimeError("INFLUX_TOKEN missing in .env")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{seconds}s)
  |> limit(n: 20)
'''
    tables = client.query_api().query(query, org=INFLUX_ORG)
    rows = []
    for table in tables:
        for rec in table.records:
            rows.append(
                {
                    "measurement": rec.get_measurement(),
                    "time": str(rec.get_time()),
                    "values": {k: v for k, v in rec.values.items() if not k.startswith("_") or k in ("_field", "_value", "_measurement")},
                    "field": rec.get_field(),
                    "value": rec.get_value(),
                    "thingId": rec.values.get("thingId"),
                    "correlationId": rec.values.get("correlationId"),
                }
            )
    client.close()
    return rows


def wait_for_marker(marker: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = query_influx_recent(180)
        for row in rows:
            blob = json.dumps(row, default=str)
            if marker in blob:
                print("Found marker in Influx:")
                print(json.dumps(row, indent=2, default=str))
                return True
        print(f"Waiting for marker {marker} in Influx... ({len(rows)} recent rows)")
        time.sleep(5)
    return False


def list_connections() -> list:
    r = requests.get(f"{DITTO_HTTP_URL}/api/2/connections", auth=DEVOPS_AUTH, timeout=15)
    r.raise_for_status()
    return r.json()


def find_connection_id() -> str | None:
    for c in list_connections():
        if c.get("name") == CONNECTION_NAME:
            return c.get("id")
    if CONNECTION_ID_FILE.exists():
        return CONNECTION_ID_FILE.read_text(encoding="utf-8").strip() or None
    return None


def create_connection() -> None:
    existing = find_connection_id()
    if existing:
        print(f"Connection exists id={existing}; deleting first")
        d = requests.delete(
            f"{DITTO_HTTP_URL}/api/2/connections/{existing}",
            auth=DEVOPS_AUTH,
            timeout=30,
        )
        print(f"DELETE connection: HTTP {d.status_code}")
        time.sleep(2)

    r = requests.post(
        f"{DITTO_HTTP_URL}/api/2/connections",
        auth=DEVOPS_AUTH,
        headers={"Content-Type": "application/json"},
        json=CONNECTION_BODY,
        timeout=30,
    )
    print(f"POST connection: HTTP {r.status_code}")
    print(r.text[:2000])
    if r.status_code >= 400:
        raise RuntimeError(f"Failed to create connection: {r.status_code} {r.text}")

    created = r.json()
    conn_id = created.get("id")
    CONNECTION_ID_FILE.write_text(conn_id, encoding="utf-8")
    print(f"Saved connection id: {conn_id}")

    o = requests.post(
        f"{DITTO_HTTP_URL}/api/2/connections/{conn_id}/command",
        auth=DEVOPS_AUTH,
        headers={"Content-Type": "text/plain"},
        data="connectivity.commands:openConnection",
        timeout=30,
    )
    print(f"OPEN connection: HTTP {o.status_code} {o.text[:300]}")


def connection_status() -> None:
    conn_id = find_connection_id()
    if not conn_id:
        print("No connection id found")
        print("All:", json.dumps(list_connections(), indent=2)[:2000])
        return
    r = requests.get(
        f"{DITTO_HTTP_URL}/api/2/connections/{conn_id}",
        auth=DEVOPS_AUTH,
        timeout=15,
    )
    print(f"GET connection {conn_id}: HTTP {r.status_code}")
    print(r.text[:2500])


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["manual-test", "create-connection", "status", "query"])
    args = p.parse_args()
    if args.action == "manual-test":
        marker = manual_mqtt_test()
        ok = wait_for_marker(marker)
        if not ok:
            print("FAIL: marker not found in Influx")
            # dump recent for debugging
            print(json.dumps(query_influx_recent(180), indent=2, default=str)[:3000])
            sys.exit(1)
        print("PASS: manual MQTT->Influx round-trip confirmed")
    elif args.action == "create-connection":
        create_connection()
        time.sleep(3)
        connection_status()
    elif args.action == "status":
        connection_status()
    elif args.action == "query":
        print(json.dumps(query_influx_recent(300), indent=2, default=str)[:4000])
