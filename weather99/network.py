"""Application-only proxy configuration; never change OS-wide proxy settings."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_NETWORK = Path(__file__).resolve().parents[1] / "runtime/network.json"


def proxy_url():
    value = os.environ.get("WEATHER99_PROXY_URL")
    if value is None and LOCAL_NETWORK.exists():
        value = json.loads(LOCAL_NETWORK.read_text(encoding="utf-8")).get("proxy_url")
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not parsed.port
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("WEATHER99_PROXY_URL requires an HTTP CONNECT proxy URL including port")
    return value
