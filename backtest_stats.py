from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import ccxt
import numpy as np


@dataclass
class BacktestStatsResult:
    start_utc: datetime
    end_utc: datetime
    sample_count: int
    settlement_count: int
    avg_price: float
    std_price: float
    lower_2sigma: float
    upper_2sigma: float
    base_price: float
    base_timestamp: datetime


def parse_local_datetime_input(raw_text: str, end_of_day_for_date: bool) -> datetime:
    text = raw_text.strip()
    if not text:
        raise ValueError("Please input both start and end time.")

    date_only_formats = ("%Y-%m-%d", "%Y/%m/%d")
    all_formats = ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", *date_only_formats)
    parsed: Optional[datetime] = None
    used_date_only = False
    for fmt in all_formats:
        try:
            parsed = datetime.strptime(text, fmt)
            used_date_only = fmt in date_only_formats
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Invalid time format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM.")

    if used_date_only and end_of_day_for_date:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return parsed.replace(tzinfo=local_tz).astimezone(timezone.utc)


class BacktestMarketAnalyzer:
    _TIMEFRAME_TO_MS = {
        "1m": 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
        "1d": 24 * 60 * 60_000,
    }

    def __init__(self, exchange_id: str, symbol: str):
        self.exchange_id = exchange_id
        self.symbol = symbol
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"Unsupported exchange in ccxt: {exchange_id}")
        exchange_cls = getattr(ccxt, exchange_id)
        self.exchange = exchange_cls({"enableRateLimit": True})

    @classmethod
    def _timeframe_to_ms(cls, timeframe: str) -> int:
        step = cls._TIMEFRAME_TO_MS.get(timeframe)
        if step is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        return step

    def fetch_close_series(
        self, start_utc: datetime, end_utc: datetime, timeframe: str = "1h"
    ) -> List[Tuple[int, float]]:
        start_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        if end_ms <= start_ms:
            raise ValueError("End time must be later than start time.")

        step_ms = self._timeframe_to_ms(timeframe)
        since = start_ms
        series: List[Tuple[int, float]] = []
        last_seen_ts = -1
        limit = 1000

        while since < end_ms:
            batch = self.exchange.fetch_ohlcv(self.symbol, timeframe=timeframe, since=since, limit=limit)
            if not batch:
                break
            for candle in batch:
                ts = int(candle[0])
                if ts < start_ms:
                    continue
                if ts >= end_ms:
                    break
                if ts <= last_seen_ts:
                    continue
                series.append((ts, float(candle[4])))
                last_seen_ts = ts
            batch_last_ts = int(batch[-1][0])
            next_since = batch_last_ts + step_ms
            if next_since <= since:
                break
            since = next_since
            if batch_last_ts >= end_ms:
                break
            if len(batch) < limit:
                break

        return series


def _first_weekly_settlement_anchor(start_utc: datetime) -> datetime:
    anchor = start_utc.astimezone(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    days_to_friday = (4 - anchor.weekday()) % 7
    anchor = anchor + timedelta(days=days_to_friday)
    if anchor < start_utc:
        anchor = anchor + timedelta(days=7)
    return anchor


def _sample_weekly_settlement_points(
    series: List[Tuple[int, float]], start_utc: datetime, end_utc: datetime
) -> List[Tuple[int, float]]:
    if not series:
        return []
    points: List[Tuple[int, float]] = []
    idx = 0
    n = len(series)
    anchor = _first_weekly_settlement_anchor(start_utc)
    end_ms = int(end_utc.timestamp() * 1000)

    while anchor.timestamp() * 1000 <= end_ms:
        anchor_ms = int(anchor.timestamp() * 1000)
        while idx < n and series[idx][0] < anchor_ms:
            idx += 1
        if idx >= n:
            break
        points.append(series[idx])
        anchor = anchor + timedelta(days=7)
    return points


def calculate_backtest_stats(
    symbol: str,
    start_text: str,
    end_text: str,
    timeframe: str = "1h",
) -> BacktestStatsResult:
    start_utc = parse_local_datetime_input(start_text, end_of_day_for_date=False)
    end_utc = parse_local_datetime_input(end_text, end_of_day_for_date=True)
    analyzer = BacktestMarketAnalyzer(exchange_id="binance", symbol=symbol)
    series = analyzer.fetch_close_series(start_utc=start_utc, end_utc=end_utc, timeframe=timeframe)
    if len(series) < 2:
        raise ValueError("Not enough candles in selected range. Please widen the time window.")

    settlement_points = _sample_weekly_settlement_points(series, start_utc=start_utc, end_utc=end_utc)
    if len(settlement_points) < 3:
        raise ValueError("Need at least 3 weekly settlement samples in range for weekly-ratio stats.")

    prices = np.array([price for _, price in settlement_points], dtype=float)
    returns = np.diff(prices) / prices[:-1]
    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns, ddof=1))
    base_ts, base_price = settlement_points[-1]
    lower_mult = max(0.0, 1.0 + mean_ret - 2.0 * std_ret)
    upper_mult = max(0.0, 1.0 + mean_ret + 2.0 * std_ret)
    return BacktestStatsResult(
        start_utc=start_utc,
        end_utc=end_utc,
        sample_count=len(returns),
        settlement_count=len(settlement_points),
        avg_price=mean_ret,
        std_price=std_ret,
        lower_2sigma=float(base_price) * lower_mult,
        upper_2sigma=float(base_price) * upper_mult,
        base_price=float(base_price),
        base_timestamp=datetime.fromtimestamp(base_ts / 1000, tz=timezone.utc),
    )
