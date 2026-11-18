"""Simulated cash, depth-aware fills, conservative marks and verified resolution settlement."""

import json
import sqlite3
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from .domain import SHANGHAI, RiskState, digest, timestamp
from .ingest import json_array, yes_token

SCALE = 1_000_000


def micros(value, *, up=True):
    return int(
        (Decimal(str(value)) * SCALE).to_integral_value(
            rounding=ROUND_CEILING if up else ROUND_FLOOR
        )
    )


def depth_value(book, side, shares):
    """Fill exactly the requested integer shares across real positive-size price levels."""
    levels = []
    for row in book.get(side, []):
        price, size = Decimal(str(row["price"])), Decimal(str(row["size"]))
        if not price.is_finite() or not size.is_finite() or not 0 <= price <= 1 or size < 0:
            raise ValueError("invalid order book level")
        if size:
            levels.append((price, size))
    remaining, value = Decimal(shares), Decimal(0)
    for price, size in sorted(levels, reverse=side == "bids"):
        quantity = min(remaining, size)
        value += price * quantity
        remaining -= quantity
        if remaining == 0:
            return micros(value, up=side == "asks")
    return None


class PaperLedger:
    def __init__(self, path: Path, initial_cash_usd=50.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_positions (
                id TEXT PRIMARY KEY, target_date TEXT UNIQUE NOT NULL,
                opened_at TEXT NOT NULL, signal_id TEXT UNIQUE NOT NULL,
                shares INTEGER NOT NULL, cost INTEGER NOT NULL, reserve INTEGER NOT NULL,
                legs TEXT NOT NULL, model_version TEXT NOT NULL, config_version TEXT NOT NULL,
                status TEXT NOT NULL, settled_at TEXT, payout INTEGER
            );
            CREATE TABLE IF NOT EXISTS paper_cashflows (
                id TEXT PRIMARY KEY, position_id TEXT, kind TEXT NOT NULL, day TEXT NOT NULL,
                as_of TEXT NOT NULL, cash_delta INTEGER NOT NULL, realized_delta INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_marks (
                position_id TEXT NOT NULL REFERENCES paper_positions(id), as_of TEXT NOT NULL,
                value INTEGER NOT NULL, complete INTEGER NOT NULL,
                PRIMARY KEY(position_id, as_of)
            );
            CREATE TABLE IF NOT EXISTS paper_cycles (
                id INTEGER PRIMARY KEY, as_of TEXT NOT NULL, target_date TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)
        initial = micros(initial_cash_usd)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT value FROM paper_meta WHERE key='initial_cash'"
            ).fetchone()
            if existing and int(existing[0]) != initial:
                raise ValueError("existing paper account initial balance cannot be changed")
            self.db.execute(
                "INSERT OR IGNORE INTO paper_meta VALUES ('initial_cash', ?)", (str(initial),)
            )
            self.db.execute(
                "INSERT OR IGNORE INTO paper_meta VALUES ('peak_equity', ?)", (str(initial),)
            )
            self.db.execute(
                """INSERT OR IGNORE INTO paper_cashflows VALUES
                ('initial', NULL, 'deposit', '', '', ?, 0)""",
                (initial,),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def positions(self, *, open_only=True):
        query = "SELECT * FROM paper_positions"
        if open_only:
            query += " WHERE status='open'"
        return [dict(row) | {"legs": json.loads(row["legs"])} for row in self.db.execute(query)]

    def account(self, as_of):
        cash = self.db.execute("SELECT SUM(cash_delta) FROM paper_cashflows").fetchone()[0]
        marks, valuation_complete = 0, True
        positions = self.positions()
        for position in positions:
            mark = self.db.execute(
                "SELECT * FROM paper_marks WHERE position_id=? ORDER BY as_of DESC LIMIT 1",
                (position["id"],),
            ).fetchone()
            if mark:
                marks += mark["value"]
            if (
                not mark
                or not mark["complete"]
                or (timestamp(as_of) - timestamp(mark["as_of"])).total_seconds() > 1800
            ):
                valuation_complete = False
        equity = cash + marks
        peak = max(
            int(
                self.db.execute("SELECT value FROM paper_meta WHERE key='peak_equity'").fetchone()[
                    0
                ]
            ),
            equity,
        )
        self.db.execute("UPDATE paper_meta SET value=? WHERE key='peak_equity'", (str(peak),))
        day = timestamp(as_of).astimezone(SHANGHAI).date().isoformat()
        realized = self.db.execute(
            "SELECT COALESCE(SUM(realized_delta),0) FROM paper_cashflows WHERE day=?", (day,)
        ).fetchone()[0]
        losses = 0
        for row in self.db.execute(
            "SELECT payout-cost-reserve AS pnl FROM paper_positions WHERE status='settled' ORDER BY settled_at DESC, id DESC"
        ):
            if row["pnl"] >= 0:
                break
            losses += 1
        return {
            "initial_cash_usd": int(
                self.db.execute("SELECT value FROM paper_meta WHERE key='initial_cash'").fetchone()[
                    0
                ]
            )
            / SCALE,
            "cash_usd": cash / SCALE,
            "equity_usd": equity / SCALE,
            "peak_equity_usd": peak / SCALE,
            "daily_realized_usd": realized / SCALE,
            "consecutive_losses": losses,
            "open_positions": len(positions),
            "valuation_complete": valuation_complete,
            "trading_date": day,
        }

    def risk(self, as_of):
        state = self.account(as_of)
        return RiskState(
            state["trading_date"],
            state["daily_realized_usd"],
            state["consecutive_losses"],
            state["equity_usd"],
            state["peak_equity_usd"],
        )

    def mark(self, raw, quote_max_age=300):
        as_of = raw["collected_at"]
        for position in self.positions():
            if position["target_date"] != raw["forecast"]["date"]:
                continue
            total, complete = 0, True
            for token in position["legs"]:
                book = raw.get("sources", {}).get(f"book:{token}", {}).get("data") or {}
                book_at = book.get("timestamp")
                age = (
                    timestamp(as_of).timestamp() - float(book_at) / 1000
                    if book_at
                    else float("inf")
                )
                value = (
                    depth_value(book, "bids", position["shares"])
                    if 0 <= age <= quote_max_age
                    else None
                )
                if value is None:
                    complete = False
                else:
                    total += value
            self.db.execute(
                "INSERT OR IGNORE INTO paper_marks VALUES (?, ?, ?, ?)",
                (position["id"], as_of, total, int(complete)),
            )

    def buy(self, signal, snapshot, raw, settings, strategy):
        if signal["status"] != "confirmed_research_signal" or not signal.get("eligible"):
            return {"status": "no_fill", "reason": "signal_not_confirmed"}
        selected = signal["selected"]
        shares = settings["shares_per_bucket"]
        cost = 0
        for token in selected["buckets"]:
            book = raw.get("sources", {}).get(f"book:{token}", {}).get("data") or {}
            minimum = Decimal(str(book.get("min_order_size", shares)))
            if minimum > shares:
                return {"status": "no_fill", "reason": "below_book_min_order_size"}
            value = depth_value(book, "asks", shares)
            if value is None:
                return {"status": "no_fill", "reason": "insufficient_ask_depth"}
            cost += value
        reserve = micros(Decimal(str(selected["reserve_per_share_set"])) * shares)
        if cost + reserve > micros(settings["position_budget_usd"]):
            return {"status": "no_fill", "reason": "position_budget_exceeded"}
        edge = selected["weather_probability"] - cost / SCALE / shares - reserve / SCALE / shares
        if edge + 1e-12 < strategy.thresholds(snapshot.layer)[1]:
            return {"status": "no_fill", "reason": "depth_adjusted_edge_below_threshold"}
        identifier = digest({"account": "paper", "target_date": snapshot.target_date})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute(
                "SELECT 1 FROM paper_positions WHERE id=?", (identifier,)
            ).fetchone():
                result = {"status": "no_fill", "reason": "market_already_traded"}
            elif len(self.positions()) >= settings["max_open_positions"]:
                result = {"status": "no_fill", "reason": "max_open_positions"}
            elif not self.account(snapshot.as_of)["valuation_complete"]:
                result = {"status": "no_fill", "reason": "existing_positions_unpriced"}
            elif micros(self.account(snapshot.as_of)["cash_usd"]) < cost + reserve:
                result = {"status": "no_fill", "reason": "insufficient_cash"}
            else:
                self.db.execute(
                    """INSERT INTO paper_positions VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL)""",
                    (
                        identifier,
                        snapshot.target_date,
                        snapshot.as_of,
                        signal["id"],
                        shares,
                        cost,
                        reserve,
                        json.dumps(selected["buckets"]),
                        snapshot.forecast.model_version,
                        strategy.version,
                    ),
                )
                day = timestamp(snapshot.as_of).astimezone(SHANGHAI).date().isoformat()
                self.db.execute(
                    "INSERT INTO paper_cashflows VALUES (?, ?, 'buy', ?, ?, ?, ?)",
                    (
                        identifier + ":buy",
                        identifier,
                        day,
                        snapshot.as_of,
                        -cost - reserve,
                        -reserve,
                    ),
                )
                result = {
                    "status": "paper_filled",
                    "position_id": identifier,
                    "cost_usd": cost / SCALE,
                    "reserve_usd": reserve / SCALE,
                    "shares_per_bucket": shares,
                    "legs": selected["buckets"],
                }
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def settle(self, raw):
        """Require every event market to be closed/resolved with exactly one winning Yes."""
        event = raw.get("sources", {}).get("event", {}).get("data") or {}
        markets = event.get("markets", [])
        if not event.get("closed") or not markets or event.get("slug") != raw.get("event_slug"):
            return []
        prices = {}
        for market in markets:
            if not market.get("closed") or market.get("umaResolutionStatus") != "resolved":
                return []
            token, outcomes, values = (
                yes_token(market),
                json_array(market.get("outcomes")),
                json_array(market.get("outcomePrices")),
            )
            if not token or len(outcomes) != 2 or len(values) != 2:
                return []
            numeric = [Decimal(str(value)) for value in values]
            if set(numeric) != {Decimal(0), Decimal(1)}:
                return []
            prices[token] = int(
                numeric[next(i for i, outcome in enumerate(outcomes) if outcome.lower() == "yes")]
            )
        if sum(prices.values()) != 1:
            return []
        settled = []
        as_of = raw["collected_at"]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for position in self.positions():
                if position["target_date"] != raw["forecast"]["date"] or any(
                    token not in prices for token in position["legs"]
                ):
                    continue
                payout = (
                    sum(prices[token] for token in position["legs"]) * position["shares"] * SCALE
                )
                self.db.execute(
                    "UPDATE paper_positions SET status='settled', settled_at=?, payout=? WHERE id=? AND status='open'",
                    (as_of, payout, position["id"]),
                )
                self.db.execute(
                    "INSERT INTO paper_cashflows VALUES (?, ?, 'settle', ?, ?, ?, ?)",
                    (
                        position["id"] + ":settle",
                        position["id"],
                        timestamp(as_of).astimezone(SHANGHAI).date().isoformat(),
                        as_of,
                        payout,
                        payout - position["cost"],
                    ),
                )
                settled.append(
                    {
                        "position_id": position["id"],
                        "payout_usd": payout / SCALE,
                        "net_pnl_usd": (payout - position["cost"] - position["reserve"]) / SCALE,
                    }
                )
            self.db.execute("COMMIT")
            return settled
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def record_cycle(self, target_date, as_of, payload):
        self.db.execute(
            "INSERT INTO paper_cycles(as_of,target_date,status,payload) VALUES (?, ?, ?, ?)",
            (as_of, target_date, payload["status"], json.dumps(payload, ensure_ascii=False)),
        )
