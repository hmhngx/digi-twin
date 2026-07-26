"""Shared configuration loaded from environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DITTO_HTTP_URL = os.getenv("DITTO_HTTP_URL", "http://localhost:8081").rstrip("/")
EXTENDED_API_URL = os.getenv("EXTENDED_API_URL", "http://localhost:8080").rstrip("/")
DITTO_USER = os.getenv("DITTO_USER", "ditto")
DITTO_PASSWORD = os.getenv("DITTO_PASSWORD", "ditto")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086").rstrip("/")
INFLUX_ORG = os.getenv("INFLUX_ORG", "opentwins")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "default")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
NAMESPACE = os.getenv("NAMESPACE", "hnguyen.clustertwin")
PUBLISH_INTERVAL_SEC = float(os.getenv("PUBLISH_INTERVAL_SEC", "3"))
AGGREGATE_INTERVAL_SEC = float(os.getenv("AGGREGATE_INTERVAL_SEC", "7"))
FAILURE_TOLERANCE_SEC = float(os.getenv("FAILURE_TOLERANCE_SEC", "9"))
HEALTHY_CPU_THRESHOLD = float(os.getenv("HEALTHY_CPU_THRESHOLD", "80"))

POLICY_ID = f"{NAMESPACE}:basic_policy"
NODE_TYPE = f"{NAMESPACE}:NodeType"
RACK_TYPE = f"{NAMESPACE}:RackType"
CLUSTER_TYPE = f"{NAMESPACE}:ClusterType"
CLUSTER_ID = f"{NAMESPACE}:main_cluster"
RACK_IDS = [f"{NAMESPACE}:rack_0", f"{NAMESPACE}:rack_1"]
NODE_IDS = [f"{NAMESPACE}:node_{i}" for i in range(10)]
RACK_NODES = {
    RACK_IDS[0]: NODE_IDS[0:5],
    RACK_IDS[1]: NODE_IDS[5:10],
}


def thing(name: str) -> str:
    return f"{NAMESPACE}:{name}"
