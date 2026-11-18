"""Deterministic evaluation: no I/O, wall clock, mutable account state or order placement."""

from itertools import combinations

from .config import StrategyConfig
from .domain import Candidate, Evaluation, MarketSnapshot, RiskState, timestamp
from .probability import mode_gap_c, partition, probability
from .risk import check_risk


def evaluate(
    snapshot: MarketSnapshot, config: StrategyConfig, risk: RiskState | None = None
) -> Evaluation:
    probability_floor, edge_floor = config.thresholds(snapshot.layer)
    reasons = list(check_risk(risk, snapshot.as_of, config))
    as_of = timestamp(snapshot.as_of)
    if timestamp(snapshot.closes_at) <= as_of:
        reasons.append("market_closed")
    if timestamp(snapshot.forecast.available_at) > as_of:
        reasons.append("model_not_available_at_snapshot")
    if snapshot.forecast.disagreement_c >= config.extreme_disagreement_c:
        reasons.append("extreme_model_disagreement")
    weather_mode = market_mode = None
    probabilities, candidates = (), ()

    def result(selected=None):
        return Evaluation(
            snapshot.snapshot_id,
            config.version,
            snapshot.forecast.model_version,
            snapshot.target_date,
            snapshot.layer,
            snapshot.as_of,
            tuple(reasons),
            weather_mode,
            market_mode,
            probabilities,
            candidates,
            selected,
        )

    if len(snapshot.buckets) > config.max_market_buckets:
        reasons.append("too_many_market_buckets")
        return result()
    try:
        buckets = partition(snapshot.buckets)
    except ValueError as exc:
        reasons.append(f"invalid_bucket_partition:{exc}")
        return result()
    if any(bucket.market_probability is None for bucket in buckets):
        reasons.append("market_probability_missing")
        return result()
    mass = sum(bucket.market_probability for bucket in buckets)
    if mass <= 0:
        reasons.append("market_probability_zero_mass")
        return result()
    try:
        weather = {
            bucket.key: probability(bucket, snapshot.forecast, snapshot.observed_max_c)
            for bucket in buckets
        }
    except ValueError as exc:
        reasons.append(f"invalid_observation_conditioning:{exc}")
        return result()
    market = {bucket.key: bucket.market_probability / mass for bucket in buckets}
    probabilities = tuple((b.key, weather[b.key], market[b.key]) for b in buckets)
    weather_bucket = max(buckets, key=lambda b: weather[b.key])
    market_bucket = max(buckets, key=lambda b: market[b.key])
    weather_mode, market_mode = weather_bucket.key, market_bucket.key
    if mode_gap_c(weather_bucket, market_bucket) > config.mode_gap_max_c:
        reasons.append("modal_temperature_gap")
    quote_reasons = {}
    for bucket in buckets:
        issues = []
        age = (as_of - timestamp(bucket.quoted_at)).total_seconds()
        if age < 0 or age > config.quote_max_age_seconds:
            issues.append(f"{bucket.key}:quote_stale_or_future")
        if bucket.bid is None or bucket.ask is None:
            issues.append(f"{bucket.key}:one_sided_book")
        else:
            if not config.ask_min <= bucket.ask <= config.ask_max:
                issues.append(f"{bucket.key}:ask_out_of_range")
            if bucket.bid > bucket.ask:
                issues.append(f"{bucket.key}:crossed_book")
            elif bucket.ask - bucket.bid > config.max_spread + 1e-12:
                issues.append(f"{bucket.key}:spread_too_wide")
        quote_reasons[bucket.key] = issues
    # Modal probabilities must be fresh even when their bucket is not selected.
    if any("quote_stale_or_future" in reason for reason in quote_reasons[market_mode]):
        reasons.append("market_mode_quote_stale")
    rows = []
    for size in range(1, min(config.max_buckets, len(buckets)) + 1):
        for combo in combinations(buckets, size):
            issues = [reason for bucket in combo for reason in quote_reasons[bucket.key]]
            keys = tuple(bucket.key for bucket in combo)
            weather_p = sum(weather[key] for key in keys)
            market_p = sum(market[key] for key in keys)
            blended_p = (1 - config.market_weight) * weather_p + config.market_weight * market_p
            # One share in each mutually exclusive bucket: payout is $1 on any selected outcome.
            cost = sum(bucket.ask or 0 for bucket in combo)
            reserve = config.fee_reserve + config.slippage_reserve
            edge = weather_p - cost - reserve
            if blended_p + 1e-12 < probability_floor:
                issues.append("blended_probability_below_threshold")
            if edge + 1e-12 < edge_floor:
                issues.append("net_edge_below_threshold")
            if config.must_include_market_mode and market_mode not in keys:
                issues.append("market_mode_not_in_combo")
            rows.append(
                Candidate(keys, weather_p, market_p, blended_p, cost, reserve, edge, tuple(issues))
            )
    # Preserve rejected candidates for audit; select the highest-edge passing candidate deterministically.
    candidates = tuple(sorted(rows, key=lambda row: (-row.net_edge, len(row.buckets), row.buckets)))
    selected = next((row for row in candidates if not row.reasons), None)
    if selected is None:
        reasons.append("no_eligible_combo")
    return result(selected)
