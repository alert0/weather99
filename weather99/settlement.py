"""Rule-specific evidence from the market's AviationWeather resolution source."""

import math
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from urllib.parse import parse_qs, urlsplit

from .domain import SHANGHAI, timestamp
from .http import fetch


def routine_temperature(raw):
    """Use the T remark, then the body temperature; never use rounded API temp."""
    raw = str(raw or "").strip()
    if raw.startswith("SPECI ") or not re.match(r"^(?:(?:METAR\s+)?(?:COR\s+)?)ZSPD\s", raw):
        return None
    body, _, remarks = raw.partition(" RMK ")
    precise = re.search(r"(?:^|\s)T([01])(\d{3})[01]\d{3}(?=\s|$)", remarks)
    if precise:
        return (-1 if precise[1] == "1" else 1) * int(precise[2]) / 10
    whole = re.search(r"(?:^|\s)(M?\d{2})/(?:M?\d{2}|//)?(?=\s|$)", body)
    if whole:
        return -int(whole[1][1:]) if whole[1].startswith("M") else int(whole[1])
    return None


def metar_evidence(rows, target_date, as_of):
    """Keep receipt-latest corrections and exclude future, foreign and special reports."""
    cutoff = timestamp(as_of)
    chosen = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("icaoId") != "ZSPD":
            continue
        try:
            moment = datetime.fromtimestamp(float(row["obsTime"]), UTC)
            receipt = timestamp(row["receiptTime"]) if row.get("receiptTime") else moment
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            continue
        if moment > cutoff or receipt > cutoff:
            continue
        if moment not in chosen or receipt >= chosen[moment][0]:
            chosen[moment] = (receipt, row)
    points, next_day = [], None
    target = date.fromisoformat(target_date)
    for moment, (_, row) in sorted(chosen.items()):
        raw = str(row.get("rawOb") or "").strip()
        value = routine_temperature(raw)
        local_day = moment.astimezone(SHANGHAI).date()
        if local_day == target + timedelta(days=1) and not raw.startswith("SPECI ") and "ZSPD" in raw:
            next_day = next_day or moment.isoformat()
        if local_day == target and value is not None:
            points.append({"observed_at": moment.isoformat(), "temperature_c": value, "raw_metar": raw})
    high = max((p["temperature_c"] for p in points), default=None)
    rounded = int((Decimal(str(high)) + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)) if high is not None else None
    return {
        "observations": points,
        "observation_count": len(points),
        "observed_max_c": high,
        "rounded_max_c": rounded,
        "latest_observed_at": points[-1]["observed_at"] if points else None,
        "next_day_first_report_at": next_day,
        "status": "day_closed_evidence" if points and next_day else "provisional" if points else "no_observations",
        # Only Polymarket final resolution can settle the paper ledger.
        "final": False,
    }


def collect_settlement(event, target_date, *, as_of=None):
    as_of = as_of or datetime.now(UTC).isoformat()
    event = event if isinstance(event, dict) else {}
    source_url = event.get("resolutionSource") or ""
    rules = event.get("description") or ""
    parsed = urlsplit(source_url)
    query = parse_qs(parsed.query)
    supported = (
        parsed.scheme == "https" and parsed.netloc == "aviationweather.gov"
        and parsed.path == "/api/data/metar" and query.get("ids") == ["ZSPD"]
        and "hourly temperature remark" in rules and "SPECI" in rules
        and "half rounded up" in rules
    )
    result = {
        "source": "aviationweather" if supported else "unsupported",
        "source_url": source_url,
        "station": "ZSPD",
        "target_date": target_date,
        "timezone": "Asia/Shanghai",
        "rules_verified": supported,
        "rules": rules,
        "collected_at": as_of,
        "status": "unsupported_rules",
        "observations": [],
        "final": False,
        "fallback": {"source": "wunderground", "status": "not_activated"},
    }
    if not supported:
        return result, None
    start = datetime.combine(date.fromisoformat(target_date), time.min, SHANGHAI)
    now = timestamp(as_of)
    if start > now:
        result["status"] = "awaiting_target_day"
        return result, None
    # Include the full local date and following-day reports, within AWC's recent archive.
    end = min(now, start + timedelta(hours=32))
    params = {"ids": "ZSPD", "format": "json", "date": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "hours": math.ceil((end - start).total_seconds() / 3600) + 1}
    response = fetch("https://aviationweather.gov/api/data/metar", params)
    result["collected_at"] = response.get("fetched_at", as_of)
    result["request_url"] = response.get("url")
    if response.get("error"):
        result.update(status="source_error", error=response["error"])
    else:
        result.update(metar_evidence(response.get("data"), target_date, result["collected_at"]))
    return result, response
