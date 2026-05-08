#!/usr/bin/env python3
"""
BTC Options Chain Analyzer — Headless Runner
============================================
Usage (cron example):
  0 18 * * 5  /usr/bin/python3 /Users/tyc/Desktop/workspace/btc_options_chain_analyzer/run_headless.py >> ~/logs/btc_options.log 2>&1

CLI args (all optional — defaults below):
  --symbol      BTC/USD or BTC/USDT (default: BTC/USD)
  --mode        deribit | binance (default: deribit)
  --expiry      nextfriday | nearest | YYYY-MM-DD (default: nextfriday)
  --wings       number of strikes each side (default: 5)
  --delta       target delta range, e.g. 2 = ±2 delta (default: 2)
  --backtest-symbol  symbol for Backtest Stats (default: BTC/USDT)
  --backtest-start   Backtest Stats start date (default: 2018-01-01)
  --backtest-end     Backtest Stats end date (default: local today)
  --output      JSON output path (default: stdout only)
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backtest_stats import calculate_backtest_stats

# ── Constants (must match main.py) ────────────────────────────────────────────
TRADING_DAYS = 365.0
EN_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class ChainRow:
    strike: float
    call_delta: float
    put_delta: float
    call_bid: float
    put_bid: float
    call_mark: float
    put_mark: float
    call_annual: float
    put_annual: float
    is_atm: bool


@dataclass
class DeltaRange:
    target_delta: float
    call_strike: float
    put_strike: float
    call_delta: float
    put_delta: float
    spread_pct: float
    mid_price: float


@dataclass
class AnalysisResult:
    timestamp: str
    symbol: str
    mode: str
    expiry_label: str
    spot: float
    base_price: float
    base_timestamp: str
    backtest_stats: dict
    iv_text: str
    delta_range: dict
    rows: List[dict]
    summary: str


# ── Deribit API ──────────────────────────────────────────────────────────────
class DeribitAnalyzer:
    BASE_URL = "https://www.deribit.com/api/v2"

    def __init__(self, currency: str = "BTC"):
        self.currency = currency.upper()

    def _get(self, method: str, **params) -> dict:
        query = urllib.parse.urlencode(params)
        url = f"{self.BASE_URL}/{method}?{query}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("error"):
            raise ValueError(f"Deribit API error: {payload['error']}")
        return payload["result"]

    def get_index_price(self) -> float:
        return float(
            self._get("public/get_index_price",
                       index_name=f"{self.currency.lower()}_usd")["index_price"]
        )

    def list_expiries(self) -> List[Tuple[int, str]]:
        instruments = self._get(
            "public/get_instruments",
            currency=self.currency,
            kind="option",
            expired="false",
        )
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expiries = sorted({int(x["expiration_timestamp"]) for x in instruments})
        choices = []
        for ts in expiries:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
            left_hours = max((ts - now_ms) / (1000 * 3600), 0.0)
            left_days = int(left_hours // 24)
            left_h = int(left_hours % 24)
            month = EN_MONTH_ABBR[dt.month - 1]
            label = f"{dt.day:02d} {month} {dt.year} ({left_days}d {left_h}h)"
            choices.append((ts, label))
        return choices

    def fetch_chain(
        self, expiry_ts: int, wing_count: int = 5
    ) -> Tuple[float, str, List[ChainRow]]:
        """Fetch options chain for a specific expiry."""
        spot = self.get_index_price()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        days_to_expiry = max((expiry_ts - now_ms) / (1000.0 * 3600.0 * 24.0), 1e-9)
        t_years = days_to_expiry / TRADING_DAYS

        instruments = self._get(
            "public/get_instruments",
            currency=self.currency,
            kind="option",
            expired="false",
        )
        summaries = self._get(
            "public/get_book_summary_by_currency",
            currency=self.currency,
            kind="option",
        )

        expiries = sorted({int(x["expiration_timestamp"]) for x in instruments})
        if expiry_ts not in expiries:
            expiry_ts = min(expiries, key=lambda ts: abs(ts - expiry_ts))

        selected_names = {
            x["instrument_name"]
            for x in instruments
            if int(x["expiration_timestamp"]) == expiry_ts
        }
        summary_map = {x["instrument_name"]: x for x in summaries if x.get("instrument_name") in selected_names}

        calls_by_strike: Dict[float, dict] = {}
        puts_by_strike: Dict[float, dict] = {}
        for ins in instruments:
            if int(ins["expiration_timestamp"]) != expiry_ts:
                continue
            strike = float(ins["strike"])
            summary = summary_map.get(ins["instrument_name"])
            if not summary:
                continue
            if ins["option_type"] == "call":
                calls_by_strike[strike] = summary
            else:
                puts_by_strike[strike] = summary

        common_strikes = sorted(set(calls_by_strike.keys()) & set(puts_by_strike.keys()))
        if not common_strikes:
            raise ValueError("No complete call/put strike pairs for target expiry")

        atm_idx = min(range(len(common_strikes)), key=lambda i: abs(common_strikes[i] - spot))
        start_idx = max(0, atm_idx - wing_count)
        end_idx = min(len(common_strikes), atm_idx + wing_count + 1)
        picked_strikes = common_strikes[start_idx:end_idx]
        atm_strike = common_strikes[atm_idx]

        def delta_from_mark_iv(underlying: float, strike: float,
                                mark_iv_pct: float) -> Tuple[float, float]:
            iv = max(mark_iv_pct / 100.0, 1e-9)
            t = max(t_years, 1e-9)
            d1 = (math.log(underlying / strike) + (0.5 * iv * iv) * t) / (iv * math.sqrt(t))
            call_delta = norm.cdf(d1)
            put_delta = norm.cdf(d1) - 1
            return call_delta, put_delta

        rows: List[ChainRow] = []
        for strike in picked_strikes:
            c = calls_by_strike.get(strike, {})
            p = puts_by_strike.get(strike, {})

            c_bid = float(c.get("bid_price") or 0.0)
            p_bid = float(p.get("bid_price") or 0.0)
            c_mark_iv = float(c.get("mark_iv") or 0.0)
            p_mark_iv = float(p.get("mark_iv") or 0.0)
            c_underlying = float(c.get("underlying_price") or spot)
            p_underlying = float(p.get("underlying_price") or spot)

            call_delta, put_delta = delta_from_mark_iv(c_underlying, strike, c_mark_iv)
            if c_mark_iv == 0:
                _, put_delta = delta_from_mark_iv(p_underlying, strike, p_mark_iv)

            rows.append(ChainRow(
                strike=strike,
                call_delta=call_delta,
                put_delta=put_delta,
                call_bid=c_bid,
                put_bid=p_bid,
                call_mark=float(c.get("mark_price") or 0.0),
                put_mark=float(p.get("mark_price") or 0.0),
                call_annual=c_bid * (TRADING_DAYS / days_to_expiry),
                put_annual=(p_bid * p_underlying / strike) * (TRADING_DAYS / days_to_expiry),
                is_atm=(strike == atm_strike),
            ))

        iv_text = f"spot={spot}"
        return spot, iv_text, rows


# ── Norm (pulled in from scipy) ──────────────────────────────────────────────
import math
from scipy.stats import norm

# ── Expiry helpers ───────────────────────────────────────────────────────────
def next_friday_08_utc(now: Optional[datetime] = None) -> datetime:
    """Return the next Deribit weekly Friday 08:00 UTC expiry after now."""
    now = now or datetime.now(timezone.utc)
    friday = now.replace(hour=8, minute=0, second=0, microsecond=0)
    friday += timedelta(days=(4 - now.weekday()) % 7)
    if friday <= now:
        friday += timedelta(days=7)
    return friday.astimezone(timezone.utc)


def find_expiry_for_time(expiry_list: List[Tuple[int, str]],
                          target_dt: datetime) -> Tuple[int, str]:
    """Find the expiry closest to (but not before) the given UTC datetime."""
    target_ts = int(target_dt.timestamp() * 1000)
    future_expiries = [item for item in expiry_list if item[0] >= target_ts]
    if future_expiries:
        return min(future_expiries, key=lambda item: item[0])
    return max(expiry_list, key=lambda item: item[0])


# ── Delta range analysis ─────────────────────────────────────────────────────
def _interp_or_extrap(strikes, deltas, target):
    """Interpolate or extrapolate strike for a target delta."""
    # Bracketed: interpolate
    for i in range(len(deltas) - 1):
        d_lo, d_hi = deltas[i], deltas[i + 1]
        if (d_lo - target) * (d_hi - target) <= 0:
            k_lo, k_hi = strikes[i], strikes[i + 1]
            frac = (target - d_lo) / (d_hi - d_lo) if d_hi != d_lo else 0.0
            return k_lo + frac * (k_hi - k_lo)
    # Out of range: extrapolate from nearest two
    diffs = [abs(d - target) for d in deltas]
    i = diffs.index(min(diffs))
    if i == 0:
        k_lo, k_hi = strikes[0], strikes[1]
        d_lo, d_hi = deltas[0], deltas[1]
    elif i == len(deltas) - 1:
        k_lo, k_hi = strikes[-2], strikes[-1]
        d_lo, d_hi = deltas[-2], deltas[-1]
    else:
        k_lo, k_hi = strikes[i - 1], strikes[i]
        d_lo, d_hi = deltas[i - 1], deltas[i]
    frac = (target - d_lo) / (d_hi - d_lo) if d_hi != d_lo else 0.0
    return k_lo + frac * (k_hi - k_lo)


def find_delta_range(rows: List[ChainRow], target_delta: float = 2.0) -> DeltaRange:
    """
    Find the call strike closest to +target_delta and put strike closest to -target_delta.
    target_delta: the "delta number" from user (e.g. 2 means delta = 0.02).
    Returns interpolated strikes even if no strike has exact target delta.
    """
    target = abs(target_delta) / 100.0  # e.g. 2 → 0.02

    sorted_rows = sorted(rows, key=lambda r: r.strike)
    strikes     = [r.strike for r in sorted_rows]
    call_deltas = [r.call_delta for r in sorted_rows]
    put_deltas  = [r.put_delta  for r in sorted_rows]

    call_strike = _interp_or_extrap(strikes, call_deltas, target)
    put_strike  = _interp_or_extrap(strikes, put_deltas,  -target)

    # Find nearest actual rows for delta reporting
    call_row = min(sorted_rows, key=lambda r: abs(r.call_delta - target))
    put_row  = min(sorted_rows, key=lambda r: abs(r.put_delta + target))

    spread_pct = (call_strike - put_strike) / call_strike * 100
    mid_price = (call_row.call_mark + put_row.put_mark) / 2

    return DeltaRange(
        target_delta=target_delta,
        call_strike=call_strike,
        put_strike=put_strike,
        call_delta=call_row.call_delta,
        put_delta=put_row.put_delta,
        spread_pct=spread_pct,
        mid_price=mid_price,
    )


def format_rows_for_display(rows: List[ChainRow]) -> List[dict]:
    return [
        {
            "strike": r.strike,
            "call_delta": f"{r.call_delta:.4f}",
            "put_delta": f"{r.put_delta:.4f}",
            "call_bid": r.call_bid,
            "put_bid": r.put_bid,
            "call_mark": r.call_mark,
            "put_mark": r.put_mark,
            "call_annual": f"{r.call_annual:.4f}",
            "put_annual": f"{r.put_annual:.4f}",
            "is_atm": r.is_atm,
        }
        for r in rows
    ]


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="BTC Options Chain — Headless")
    parser.add_argument("--symbol", default="BTC/USD")
    parser.add_argument("--mode", default="deribit", choices=["deribit", "binance"])
    parser.add_argument("--expiry", default="nextfriday")
    parser.add_argument("--wings", type=int, default=5)
    parser.add_argument("--delta", type=float, default=2.0)
    parser.add_argument("--backtest-symbol", default="BTC/USDT")
    parser.add_argument("--backtest-start", default="2018-01-01")
    parser.add_argument("--backtest-end", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    currency = args.symbol.split("/")[0].strip().upper()

    if args.mode == "deribit":
        analyzer = DeribitAnalyzer(currency=currency)
        expiry_list = analyzer.list_expiries()

        # Resolve expiry
        if args.expiry == "nextfriday":
            target_dt = next_friday_08_utc()
            expiry_ts, expiry_label = find_expiry_for_time(expiry_list, target_dt)
        elif args.expiry == "nearest":
            expiry_ts, expiry_label = min(expiry_list, key=lambda x: abs(x[0] - time.time() * 1000))
        else:
            # Assume YYYY-MM-DD format
            dt = datetime.fromisoformat(args.expiry).astimezone(timezone.utc)
            expiry_ts, expiry_label = find_expiry_for_time(expiry_list, dt)

        spot, iv_text, rows = analyzer.fetch_chain(expiry_ts=expiry_ts, wing_count=args.wings)

    else:
        print("binance mode: requires ccxt + market analyzer — not implemented in headless", file=sys.stderr)
        sys.exit(1)

    backtest_end = args.backtest_end or datetime.now(timezone.utc).astimezone().date().strftime("%Y-%m-%d")
    backtest = calculate_backtest_stats(
        symbol=args.backtest_symbol,
        start_text=args.backtest_start,
        end_text=backtest_end,
        timeframe="1h",
    )
    backtest_payload = {
        "symbol": args.backtest_symbol,
        "start_utc": backtest.start_utc.isoformat(),
        "end_utc": backtest.end_utc.isoformat(),
        "sample_count": backtest.sample_count,
        "settlement_count": backtest.settlement_count,
        "mean": backtest.avg_price,
        "std": backtest.std_price,
        "lower_2sigma": backtest.lower_2sigma,
        "upper_2sigma": backtest.upper_2sigma,
        "base_price": backtest.base_price,
        "base_timestamp": backtest.base_timestamp.isoformat(),
    }
    delta_range = find_delta_range(rows, target_delta=args.delta)

    result = AnalysisResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        symbol=args.symbol,
        mode=args.mode,
        expiry_label=expiry_label,
        spot=spot,
        base_price=backtest.base_price,
        base_timestamp=backtest.base_timestamp.isoformat(),
        backtest_stats=backtest_payload,
        iv_text=iv_text,
        delta_range=asdict(delta_range),
        rows=format_rows_for_display(rows),
        summary=(
            f"BTC Backtest Stats (±2σ):\n"
            f"Samples {backtest.sample_count} (weekly ratio changes, {backtest.settlement_count} settlements)\n"
            f"Mean {backtest.avg_price:+.3%}\n"
            f"Std {backtest.std_price:.3%}\n"
            f"±2σ Band {backtest.lower_2sigma:,.2f}~{backtest.upper_2sigma:,.2f} "
            f"(base {backtest.base_price:,.2f})"
        ),
    )

    output = json.dumps({k: v if not isinstance(v, float) else round(v, 6)
                          for k, v in asdict(result).items()}, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output)
        print(f"[{result.timestamp}] Saved → {args.output}")
        print(result.summary)
    else:
        print(output)

    print(result.summary, file=sys.stderr)


if __name__ == "__main__":
    main()
