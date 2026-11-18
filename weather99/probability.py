"""Normal temperature distribution projected onto disjoint integer Celsius buckets."""

import itertools
import math
import re

from .domain import Bucket, ForecastEstimate


def parse_bounds(label: str) -> tuple[int | None, int | None]:
    text = label.strip().replace("−", "-").replace("–", "-")
    exact = re.fullmatch(r"(-?\d+)\s*°?C", text, re.IGNORECASE)
    if exact:
        value = int(exact[1])
        return value, value
    tail = re.fullmatch(r"(-?\d+)\s*°?C\s+or\s+(below|lower|higher|above)", text, re.IGNORECASE)
    if tail:
        value = int(tail[1])
        return (None, value) if tail[2].lower() in ("below", "lower") else (value, None)
    interval = re.fullmatch(r"(-?\d+)\s*(?:-|to)\s*(-?\d+)\s*°?C", text, re.IGNORECASE)
    if interval:
        low, high = int(interval[1]), int(interval[2])
        if low <= high:
            return low, high
    raise ValueError(f"unsupported Celsius bucket label: {label!r}")


def partition(buckets: tuple[Bucket, ...]) -> tuple[Bucket, ...]:
    if len(buckets) < 2 or len({bucket.key for bucket in buckets}) != len(buckets):
        raise ValueError("market needs at least two uniquely identified buckets")
    ordered = tuple(
        sorted(buckets, key=lambda bucket: -math.inf if bucket.lower_c is None else bucket.lower_c)
    )
    if ordered[0].lower_c is not None or ordered[-1].upper_c is not None:
        raise ValueError("market is missing lower or upper tail")
    for left, right in itertools.pairwise(ordered):
        if left.upper_c is None or right.lower_c is None or left.upper_c + 1 != right.lower_c:
            raise ValueError("market buckets overlap or have gaps")
    return ordered


def probability(
    bucket: Bucket, estimate: ForecastEstimate, observed_max_c: float | None = None
) -> float:
    # erfc retains precision in the upper tail; integer buckets represent half-degree edges.
    def survival(bound: float) -> float:
        return 0.5 * math.erfc((bound - estimate.center_c) / (estimate.sigma_c * math.sqrt(2)))

    low = -math.inf if bucket.lower_c is None else bucket.lower_c - 0.5
    high = math.inf if bucket.upper_c is None else bucket.upper_c + 0.5
    if observed_max_c is not None:
        low = max(low, observed_max_c)
        if high <= low:
            return 0.0
        mass = survival(observed_max_c)
        if mass <= 0:
            raise ValueError(
                "observed maximum incompatible with forecast: numerical tail underflow"
            )
        return min(1.0, max(0.0, (survival(low) - survival(high)) / mass))
    if low >= estimate.center_c:
        value = survival(low) - survival(high)
    else:
        value = survival(-high + 2 * estimate.center_c) - survival(-low + 2 * estimate.center_c)
    return min(1.0, max(0.0, value))


def mode_gap_c(left: Bucket, right: Bucket) -> float:
    """Minimum integer separation of the two modal intervals (tails are not assigned midpoints)."""
    if left.upper_c is not None and right.lower_c is not None and left.upper_c < right.lower_c:
        return right.lower_c - left.upper_c
    if right.upper_c is not None and left.lower_c is not None and right.upper_c < left.lower_c:
        return left.lower_c - right.upper_c
    return 0.0
