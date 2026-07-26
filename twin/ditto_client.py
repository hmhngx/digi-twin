"""Thin HTTP helpers for Ditto + Extended API (ditto:ditto Basic Auth)."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import requests

from twin.config import (
    DITTO_HTTP_URL,
    DITTO_PASSWORD,
    DITTO_USER,
    EXTENDED_API_URL,
)

AUTH = (DITTO_USER, DITTO_PASSWORD)
JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def enc(thing_id: str) -> str:
    """URL-encode a Thing ID for path segments (colon → %3A)."""
    return quote(thing_id, safe="")


def ext(method: str, path: str, json: Any = None, timeout: float = 30) -> requests.Response:
    url = f"{EXTENDED_API_URL}{path}"
    r = requests.request(method, url, auth=AUTH, headers=JSON_HEADERS, json=json, timeout=timeout)
    return r


def ditto(method: str, path: str, json: Any = None, timeout: float = 30,
          content_type: Optional[str] = None) -> requests.Response:
    url = f"{DITTO_HTTP_URL}{path}"
    headers = dict(JSON_HEADERS)
    if content_type:
        headers["Content-Type"] = content_type
    r = requests.request(method, url, auth=AUTH, headers=headers, json=json, timeout=timeout)
    return r


def ensure_ok(resp: requests.Response, context: str) -> requests.Response:
    if resp.status_code >= 400:
        raise RuntimeError(f"{context}: HTTP {resp.status_code} body={resp.text[:800]}")
    print(f"OK {context}: HTTP {resp.status_code}")
    return resp
