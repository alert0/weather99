"""Bounded JSON transport with gzip support and header-only authentication."""

import gzip
import json
import time
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

from .network import proxy_url


class NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, "authenticated redirect rejected", headers, file)


def fetch(url, params=None, *, headers=None):
    if params:
        url += "?" + urlencode(params)
    result = {"url": url}
    request_headers = {
        "User-Agent": "weather99/0.2",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        **(headers or {}),
    }
    secrets = [
        value.removeprefix("Bearer ")
        for key, value in (headers or {}).items()
        if key.lower() in ("authorization", "x-qw-api-key")
    ]

    def redact(text):
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    proxy = proxy_url()
    handlers = []
    if proxy:
        handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
    if secrets:
        handlers.append(NoCredentialRedirect())
    opener = build_opener(*handlers).open if handlers else urlopen
    for attempt in range(3):
        result["fetched_at"] = datetime.now(UTC).isoformat()
        try:
            request = Request(url, headers=request_headers)
            with opener(request, timeout=20) as response:
                body = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                raw = redact(body.decode("utf-8"))
                result.update(
                    status=response.status, raw=raw, data=json.loads(raw) if raw.strip() else None
                )
                result.pop("error", None)
            return result
        except HTTPError as exc:
            result.update(status=exc.code, error=f"HTTP {exc.code}: {redact(str(exc.reason))}")
            exc.close()
            if exc.code != 429 and exc.code < 500:
                break
        except (URLError, TimeoutError, OSError, ValueError, EOFError) as exc:
            result.update(error=f"{type(exc).__name__}: {redact(str(exc))}")
        if attempt < 2:
            time.sleep(1 + attempt)
    return result
