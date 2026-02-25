import json
import math
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import ccxt
import numpy as np
from scipy.stats import norm
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from iv_history import DeribitIVHistory, IVPoint

TRADING_DAYS = 365.0
RISK_FREE_RATE = 0.0
EN_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class ChainRow:
    strike: float
    call_open: float
    call_delta: float
    call_annual: float
    call_bid: float
    call_mark: float
    call_ask: float
    call_hook_annual: float
    put_bid: float
    put_mark: float
    put_ask: float
    put_annual: float
    put_hook_annual: float
    put_delta: float
    put_open: float
    is_atm: bool = False


@dataclass
class RefreshResult:
    spot: float
    spot_ccy: str
    iv_text: str
    rows: List[ChainRow]
    source: str
    is_deribit: bool


@dataclass
class StrategyLeg:
    action: str  # BUY or SELL
    option_type: str  # CALL or PUT
    strike: float
    entry_price: float
    latest_mark: Optional[float] = None
    close_price: Optional[float] = None


@dataclass
class StrategyPosition:
    position_id: int
    opened_at: str
    source: str
    quantity: int
    open_spot: Optional[float]
    legs: List[StrategyLeg]
    entry_cashflow: float
    last_unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None
    closed_at: str = ""
    status: str = "OPEN"
    legacy_import: bool = False


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


def _parse_local_datetime_input(raw_text: str, end_of_day_for_date: bool) -> datetime:
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


class DataFetchWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        symbol: str,
        days: int,
        wing_count: int,
        deribit_enabled: bool,
        expiry_ts: Optional[int],
        expiry_label: str,
    ):
        super().__init__()
        self.symbol = symbol
        self.days = days
        self.wing_count = wing_count
        self.deribit_enabled = deribit_enabled
        self.expiry_ts = expiry_ts
        self.expiry_label = expiry_label

    @Slot()
    def run(self):
        try:
            if self.deribit_enabled:
                currency = self.symbol.split("/")[0].strip().upper()
                if self.expiry_ts is None:
                    raise ValueError("Please select a Deribit expiry")
                deribit = DeribitAnalyzer(currency=currency)
                spot, iv_text, rows = deribit.fetch_chain(expiry_ts=self.expiry_ts, wing_count=self.wing_count)
                result = RefreshResult(
                    spot=spot,
                    spot_ccy="USD",
                    iv_text=iv_text,
                    rows=rows,
                    source=f"deribit real chain ({self.expiry_label})",
                    is_deribit=True,
                )
            else:
                analyzer = MarketAnalyzer(exchange_id="binance", symbol=self.symbol)
                spot = analyzer.fetch_spot()
                hv = analyzer.fetch_historical_vol_30d()
                rows = PricingEngine.build_model_rows(
                    spot=spot, iv=hv, days_to_expiry=self.days, strike_step=1000, wing_count=self.wing_count
                )
                quote_ccy = self.symbol.split("/")[1].strip().upper() if "/" in self.symbol else "USDT"
                result = RefreshResult(
                    spot=spot,
                    spot_ccy=quote_ccy,
                    iv_text=f"{hv:.2%} (30D HV as IV proxy)",
                    rows=rows,
                    source="binance model",
                    is_deribit=False,
                )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class ExpiryLoadWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, currency: str):
        super().__init__()
        self.currency = currency

    @Slot()
    def run(self):
        try:
            deribit = DeribitAnalyzer(currency=self.currency)
            choices = deribit.fetch_expiry_choices()
            self.finished.emit(choices)
        except Exception as exc:
            self.failed.emit(str(exc))


class MarketAnalyzer:
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

    def fetch_spot(self) -> float:
        ticker = self.exchange.fetch_ticker(self.symbol)
        last = ticker.get("last")
        if last is None:
            raise ValueError("Unable to fetch latest trade price (last)")
        return float(last)

    def fetch_historical_vol_30d(self) -> float:
        ohlcv = self.exchange.fetch_ohlcv(self.symbol, timeframe="1d", limit=60)
        if len(ohlcv) < 31:
            raise ValueError("Insufficient daily candles: at least 31 are required")

        closes = np.array([row[4] for row in ohlcv], dtype=float)
        log_returns = np.diff(np.log(closes))
        log_returns_30 = log_returns[-30:]
        daily_std = np.std(log_returns_30, ddof=1)
        return float(daily_std * math.sqrt(TRADING_DAYS))

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

    def fetch_close_prices(self, start_utc: datetime, end_utc: datetime, timeframe: str = "1h") -> List[float]:
        return [close for _, close in self.fetch_close_series(start_utc=start_utc, end_utc=end_utc, timeframe=timeframe)]


class BacktestStatsWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, symbol: str, start_text: str, end_text: str, timeframe: str = "1h"):
        super().__init__()
        self.symbol = symbol
        self.start_text = start_text
        self.end_text = end_text
        self.timeframe = timeframe

    @staticmethod
    def _first_weekly_settlement_anchor(start_utc: datetime) -> datetime:
        # Deribit weekly settlement anchor: Friday 08:00 UTC.
        anchor = start_utc.astimezone(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
        days_to_friday = (4 - anchor.weekday()) % 7
        anchor = anchor + timedelta(days=days_to_friday)
        if anchor < start_utc:
            anchor = anchor + timedelta(days=7)
        return anchor

    @classmethod
    def _sample_weekly_settlement_prices(
        cls, series: List[Tuple[int, float]], start_utc: datetime, end_utc: datetime
    ) -> List[float]:
        if not series:
            return []
        prices: List[float] = []
        idx = 0
        n = len(series)
        anchor = cls._first_weekly_settlement_anchor(start_utc)
        end_ms = int(end_utc.timestamp() * 1000)

        while anchor.timestamp() * 1000 <= end_ms:
            anchor_ms = int(anchor.timestamp() * 1000)
            while idx < n and series[idx][0] < anchor_ms:
                idx += 1
            if idx >= n:
                break
            prices.append(series[idx][1])
            anchor = anchor + timedelta(days=7)
        return prices

    @Slot()
    def run(self):
        try:
            start_utc = _parse_local_datetime_input(self.start_text, end_of_day_for_date=False)
            end_utc = _parse_local_datetime_input(self.end_text, end_of_day_for_date=True)
            analyzer = MarketAnalyzer(exchange_id="binance", symbol=self.symbol)
            series = analyzer.fetch_close_series(start_utc=start_utc, end_utc=end_utc, timeframe=self.timeframe)
            if len(series) < 2:
                raise ValueError("Not enough candles in selected range. Please widen the time window.")

            settlement_prices = self._sample_weekly_settlement_prices(series, start_utc=start_utc, end_utc=end_utc)
            if len(settlement_prices) < 3:
                raise ValueError("Need at least 3 weekly settlement samples in range for weekly-ratio stats.")
            prices = np.array(settlement_prices, dtype=float)
            returns = np.diff(prices) / prices[:-1]
            mean_ret = float(np.mean(returns))
            std_ret = float(np.std(returns, ddof=1))
            base_price = float(prices[-1])
            lower_mult = max(0.0, 1.0 + mean_ret - 2.0 * std_ret)
            upper_mult = max(0.0, 1.0 + mean_ret + 2.0 * std_ret)
            result = BacktestStatsResult(
                start_utc=start_utc,
                end_utc=end_utc,
                sample_count=len(returns),
                settlement_count=len(settlement_prices),
                avg_price=mean_ret,
                std_price=std_ret,
                lower_2sigma=base_price * lower_mult,
                upper_2sigma=base_price * upper_mult,
                base_price=base_price,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class DeribitAnalyzer:
    BASE_URL = "https://www.deribit.com/api/v2"
    _VOL_PERCENTILE_CACHE: Dict[str, Tuple[float, str]] = {}

    def __init__(self, currency: str):
        self.currency = currency.upper()
        self.iv_history = DeribitIVHistory(currency=self.currency)

    def _get(self, method: str, **params) -> dict:
        query = urllib.parse.urlencode(params)
        url = f"{self.BASE_URL}/{method}?{query}"
        with urllib.request.urlopen(url, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("error"):
            raise ValueError(f"Deribit API error: {payload['error']}")
        return payload["result"]

    def fetch_expiry_choices(self) -> List[Tuple[int, str]]:
        instruments = self._get(
            "public/get_instruments", currency=self.currency, kind="option", expired="false"
        )
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expiries = sorted({int(x["expiration_timestamp"]) for x in instruments})
        choices: List[Tuple[int, str]] = []
        for ts in expiries:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
            left_hours = max((ts - now_ms) / (1000 * 3600), 0.0)
            left_days = int(left_hours // 24)
            left_h = int(left_hours % 24)
            month = EN_MONTH_ABBR[dt.month - 1]
            label = f"{dt.day:02d} {month} {dt.year} ({left_days}d {left_h}h)"
            choices.append((ts, label))
        return choices

    @staticmethod
    def _delta_from_mark_iv(spot: float, strike: float, mark_iv_pct: float, t_years: float) -> Tuple[float, float]:
        iv = max(mark_iv_pct / 100.0, 1e-9)
        t = max(t_years, 1e-9)
        _, _, call_delta, put_delta = PricingEngine.bs_call_put(spot, strike, iv, t)
        return call_delta, put_delta

    def _fetch_volatility_percentile_text(self, lookback_days: int = 365) -> str:
        cache_key = f"{self.currency}:{lookback_days}"
        cached = self._VOL_PERCENTILE_CACHE.get(cache_key)
        now_monotonic = time.monotonic()
        if cached and now_monotonic - cached[0] < 120.0:
            return cached[1]

        # Fetch 1Y data for percentile calculation
        points = self.iv_history.fetch_dvol_index_history(timeframe_days=lookback_days, resolution="1D")
        if not points:
            raise ValueError("No volatility-index data from Deribit")

        current_val = points[-1].iv
        percentile = self.iv_history.calculate_percentile(points, current_value=current_val)

        # Fetch 30D data for high/low calculation
        points_30d = self.iv_history.fetch_dvol_index_history(timeframe_days=30, resolution="1D")
        if points_30d:
            iv_values_30d = [p.iv for p in points_30d]
            iv_high_30d = max(iv_values_30d)
            iv_low_30d = min(iv_values_30d)
            text = f"DVOL L:{iv_low_30d:.2f}/ CUR:{current_val:.2f} / H:{iv_high_30d:.2f} ({percentile:.1f}%, 1Y)"
        else:
            text = f"DVOL {current_val:.2f} ({percentile:.1f}%, 1Y)"

        self._VOL_PERCENTILE_CACHE[cache_key] = (now_monotonic, text)
        return text

    def fetch_chain(self, expiry_ts: int, wing_count: int) -> Tuple[float, str, List[ChainRow]]:
        spot = float(self._get("public/get_index_price", index_name=f"{self.currency.lower()}_usd")["index_price"])

        instruments = self._get(
            "public/get_instruments", currency=self.currency, kind="option", expired="false"
        )
        summaries = self._get("public/get_book_summary_by_currency", currency=self.currency, kind="option")

        if not instruments:
            raise ValueError("No available option instruments on Deribit")

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expiries = sorted({int(x["expiration_timestamp"]) for x in instruments})
        if expiry_ts not in expiries:
            expiry_ts = min(expiries, key=lambda ts: abs(ts - expiry_ts))
        t_years = max((expiry_ts - now_ms) / (1000.0 * 3600.0 * 24.0 * TRADING_DAYS), 1e-9)
        days_to_expiry = max((expiry_ts - now_ms) / (1000.0 * 3600.0 * 24.0), 1.0 / 24.0)

        selected_names = {
            x["instrument_name"]
            for x in instruments
            if int(x["expiration_timestamp"]) == expiry_ts
        }
        summary_map = {
            x["instrument_name"]: x
            for x in summaries
            if x.get("instrument_name") in selected_names
        }

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
            raise ValueError("No complete call/put strike pairs for target expiry on Deribit")
        wing_count = max(1, wing_count)
        atm_idx = min(range(len(common_strikes)), key=lambda i: abs(common_strikes[i] - spot))
        start_idx = max(0, atm_idx - wing_count)
        end_idx = min(len(common_strikes), atm_idx + wing_count + 1)
        picked_strikes = common_strikes[start_idx:end_idx]
        atm_strike = common_strikes[atm_idx]

        iv_candidates = []
        atm_call = calls_by_strike.get(atm_strike)
        atm_put = puts_by_strike.get(atm_strike)
        for leg in (atm_call, atm_put):
            if not leg:
                continue
            mark_iv = leg.get("mark_iv")
            if mark_iv is not None:
                iv_candidates.append(float(mark_iv) / 100.0)
        atm_iv_text = f"{(sum(iv_candidates)/len(iv_candidates)):.2%} (Deribit ATM IV)" if iv_candidates else "--"
        try:
            iv_text = self._fetch_volatility_percentile_text(lookback_days=365)
        except Exception:
            iv_text = atm_iv_text

        rows: List[ChainRow] = []
        for strike in picked_strikes:
            c = calls_by_strike[strike]
            p = puts_by_strike[strike]

            c_mark = float(c.get("mark_price") or 0.0)
            p_mark = float(p.get("mark_price") or 0.0)
            c_underlying = float(c.get("underlying_price") or spot)
            p_underlying = float(p.get("underlying_price") or spot)
            c_bid = float(c.get("bid_price") or 0.0)
            c_mark_iv = float(c.get("mark_iv") or 0.0)
            p_bid = float(p.get("bid_price") or 0.0)
            p_mark_iv = float(p.get("mark_iv") or 0.0)
            call_delta, _ = self._delta_from_mark_iv(c_underlying, strike, c_mark_iv, t_years)
            _, put_delta = self._delta_from_mark_iv(p_underlying, strike, p_mark_iv, t_years)

            rows.append(
                ChainRow(
                    strike=strike,
                    call_open=float(c.get("open_interest") or 0.0),
                    call_delta=call_delta,
                    call_annual=c_bid * (TRADING_DAYS / days_to_expiry),
                    call_bid=c_bid,
                    call_mark=c_mark,
                    call_ask=float(c.get("ask_price") or 0.0),
                    call_hook_annual=c_bid * (TRADING_DAYS / days_to_expiry),
                    put_bid=p_bid,
                    put_mark=p_mark,
                    put_ask=float(p.get("ask_price") or 0.0),
                    put_annual=(p_bid * p_underlying / strike) * (TRADING_DAYS / days_to_expiry),
                    put_hook_annual=(p_bid * p_underlying / strike) * (TRADING_DAYS / days_to_expiry),
                    put_delta=put_delta,
                    put_open=float(p.get("open_interest") or 0.0),
                    is_atm=(strike == atm_strike),
                )
            )

        return spot, iv_text, rows


class PricingEngine:
    @staticmethod
    def _d1_d2(spot: float, strike: float, iv: float, t: float):
        sigma_sqrt_t = iv * math.sqrt(t)
        if sigma_sqrt_t <= 0:
            return float("nan"), float("nan")
        d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * iv * iv) * t) / sigma_sqrt_t
        d2 = d1 - sigma_sqrt_t
        return d1, d2

    @staticmethod
    def bs_call_put(spot: float, strike: float, iv: float, t: float):
        d1, d2 = PricingEngine._d1_d2(spot, strike, iv, t)
        if math.isnan(d1):
            return 0.0, 0.0, 0.5, -0.5

        discount = math.exp(-RISK_FREE_RATE * t)
        call = spot * norm.cdf(d1) - strike * discount * norm.cdf(d2)
        put = strike * discount * norm.cdf(-d2) - spot * norm.cdf(-d1)
        call_delta = norm.cdf(d1)
        put_delta = call_delta - 1.0
        return call, put, call_delta, put_delta

    @staticmethod
    def itm_probability(spot: float, strike: float, iv: float, t: float, side: str) -> float:
        if iv <= 0 or t <= 0:
            return 0.5

        sigma = iv * math.sqrt(t)
        z = math.log(strike / spot) / sigma

        if side == "call":
            return float(1.0 - norm.cdf(z))
        return float(norm.cdf(z))

    @staticmethod
    def build_model_rows(
        spot: float, iv: float, days_to_expiry: int, strike_step: int = 1000, wing_count: int = 10
    ) -> List[ChainRow]:
        t = days_to_expiry / TRADING_DAYS
        rows: List[ChainRow] = []
        strike_step = max(100, int(strike_step))
        wing_count = max(1, int(wing_count))
        atm_strike = round(spot / strike_step) * strike_step
        # Match Deribit-style direction: top -> bottom is low strike -> high strike.
        strikes = [atm_strike + k * strike_step for k in range(-wing_count, wing_count + 1)]

        for strike in strikes:
            call_premium, put_premium, call_delta, put_delta = PricingEngine.bs_call_put(spot, strike, iv, t)
            call_itm = PricingEngine.itm_probability(spot, strike, iv, t, side="call")
            put_itm = PricingEngine.itm_probability(spot, strike, iv, t, side="put")

            call_bid = max(call_premium * 0.92, 0.0)
            call_ask = call_premium * 1.08
            put_bid = max(put_premium * 0.92, 0.0)
            put_ask = put_premium * 1.08

            rows.append(
                ChainRow(
                    strike=strike,
                    call_open=call_itm,
                    call_delta=call_delta,
                    call_annual=(call_bid / spot) * (TRADING_DAYS / days_to_expiry),
                    call_bid=call_bid,
                    call_mark=call_premium,
                    call_ask=call_ask,
                    call_hook_annual=(call_bid / spot) * (TRADING_DAYS / days_to_expiry),
                    put_bid=put_bid,
                    put_mark=put_premium,
                    put_ask=put_ask,
                    put_annual=(put_bid / strike) * (TRADING_DAYS / days_to_expiry),
                    put_hook_annual=(put_bid / strike) * (TRADING_DAYS / days_to_expiry),
                    put_delta=put_delta,
                    put_open=put_itm,
                    is_atm=(strike == atm_strike),
                )
            )

        return rows


class MainWindow(QMainWindow):
    POSITIONS_FILENAME = "strategy_positions.json"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BTC Options Chain Analyzer")
        self.resize(1460, 960)
        self._base_window_w = 1360
        self._base_window_h = 960
        self._ui_scale = 1.0
        self._font_scale = 0.82
        self._base_table_widths = {
            0: 78,
            1: 88,
            2: 88,
            3: 102,
            4: 108,
            5: 102,
            6: 205,
            7: 102,
            8: 108,
            9: 102,
            10: 88,
            11: 88,
            12: 78,
        }
        self._base_row_height = 44
        self._base_chain_visible_rows = 13
        self._base_symbol_w = 130
        self._base_horizon_w = 190
        self._base_toggle_w = 190
        self._expiry_choices: List[Tuple[int, str]] = []
        self._expiry_currency: str = ""
        self._expiry_loaded_at: float = 0.0
        self._last_rows: List[ChainRow] = []
        self._active_row: Optional[int] = None
        self._active_side: Optional[str] = None
        self._positions: List[StrategyPosition] = []
        self._next_position_id: int = 1
        self._positions_file = Path(__file__).resolve().parent / self.POSITIONS_FILENAME
        self._refresh_thread: Optional[QThread] = None
        self._refresh_worker: Optional[DataFetchWorker] = None
        self._expiry_thread: Optional[QThread] = None
        self._expiry_worker: Optional[ExpiryLoadWorker] = None
        self._backtest_thread: Optional[QThread] = None
        self._backtest_worker: Optional[BacktestStatsWorker] = None
        self._last_error_popup_ts: float = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh_data(manual=False))

        self._load_positions()
        self._build_ui()
        self.refresh_data()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        self._apply_dark_theme()

        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        self.top_bar = top_bar
        top_row = QHBoxLayout(top_bar)
        top_row.setContentsMargins(8, 8, 8, 8)
        top_row.setSpacing(8)
        top_row.setAlignment(Qt.AlignVCenter)

        self.symbol_input = QLineEdit("BTC/USDT")
        self.symbol_input.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 60)
        self.days_spin.setValue(7)
        self.days_spin.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.days_spin.valueChanged.connect(lambda _: self.refresh_data(manual=False))

        self.expiry_combo = QComboBox()
        self.expiry_combo.addItem("Enable Deribit and Refresh")
        self.expiry_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.expiry_combo.currentIndexChanged.connect(lambda: self.refresh_data(manual=False))

        self.horizon_label = QLabel("Horizon")
        self.horizon_stack = QStackedWidget()
        self.horizon_stack.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.horizon_stack.addWidget(self.days_spin)
        self.horizon_stack.addWidget(self.expiry_combo)

        self.wings_spin = QSpinBox()
        self.wings_spin.setRange(3, 30)
        self.wings_spin.setValue(6)
        self.wings_spin.valueChanged.connect(lambda _: self.refresh_data(manual=False))

        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(5, 300)
        self.refresh_spin.setValue(20)
        self.refresh_spin.valueChanged.connect(lambda _: self._toggle_timer(self.auto_check.isChecked()))

        self.deribit_check = QCheckBox("Deribit Chain")
        self.deribit_check.setChecked(False)
        self.deribit_check.toggled.connect(self._on_mode_changed)

        self.auto_check = QCheckBox("Auto")
        self.auto_check.setChecked(True)
        self.auto_check.toggled.connect(self._toggle_timer)

        top_row.addWidget(QLabel("Symbol"))
        top_row.addWidget(self.symbol_input)
        top_row.addWidget(self.horizon_label)
        top_row.addWidget(self.horizon_stack)
        self.reload_expiry_btn = QPushButton("Reload Exp")
        self.reload_expiry_btn.setMinimumWidth(100)
        self.reload_expiry_btn.clicked.connect(self._force_reload_expiries)
        top_row.addWidget(self.reload_expiry_btn)
        top_row.addWidget(QLabel("ATM Wings"))
        top_row.addWidget(self.wings_spin)
        top_row.addStretch(1)
        top_row.addWidget(QLabel("Refresh Sec"))
        top_row.addWidget(self.refresh_spin)

        toggle_box = QFrame()
        self.toggle_box = toggle_box
        toggle_box.setObjectName("toggleBox")
        toggle_row = QHBoxLayout(toggle_box)
        toggle_row.setContentsMargins(8, 4, 8, 4)
        toggle_row.setSpacing(10)
        toggle_row.addWidget(self.deribit_check)
        toggle_row.addWidget(self.auto_check)
        top_row.addWidget(toggle_box)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setMinimumWidth(80)
        self.refresh_btn.clicked.connect(lambda: self.refresh_data(manual=True))
        top_row.addWidget(self.refresh_btn)

        info_row = QHBoxLayout()
        info_row.setSpacing(8)

        self.time_label = QLabel("--")
        self.spot_label = QLabel("--")
        self.iv_label = QLabel("--")
        self.source_label = QLabel("--")
        self.hook_label = QLabel("--")
        self.time_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.spot_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.iv_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.source_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hook_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.time_card = self._make_info_card("Time", self.time_label, 205)
        self.spot_card = self._make_info_card("Spot", self.spot_label, 150)
        self.iv_card = self._make_info_card("IV", self.iv_label, 480)
        self.hook_card = self._make_info_card("ATM Annual (Bid)", self.hook_label, 280)
        self.source_card = self._make_info_card("Source", self.source_label, 420)
        self.source_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        info_row.addWidget(self.time_card)
        info_row.addWidget(self.spot_card)
        info_row.addWidget(self.iv_card)
        info_row.addWidget(self.hook_card)
        info_row.addWidget(self.source_card, 1)

        sim_bar = QFrame()
        self.sim_bar = sim_bar
        sim_bar.setObjectName("simBar")
        sim_layout = QVBoxLayout(sim_bar)
        sim_layout.setContentsMargins(8, 3, 8, 3)
        sim_layout.setSpacing(2)

        self.ic_qty_spin = QSpinBox()
        self.ic_qty_spin.setRange(1, 100)
        self.ic_qty_spin.setValue(1)
        self.ic_qty_spin.setFixedWidth(80)

        self.leg_controls: List[Tuple[QComboBox, QComboBox, str]] = []
        self.leg_titles: List[QLabel] = []
        leg_grid = QGridLayout()
        leg_grid.setContentsMargins(0, 0, 0, 0)
        leg_grid.setHorizontalSpacing(8)
        leg_grid.setVerticalSpacing(2)
        left_title = QLabel("Calls")
        right_title = QLabel("Puts")
        left_title.setAlignment(Qt.AlignCenter)
        right_title.setAlignment(Qt.AlignCenter)
        left_title.setStyleSheet("font-weight: 700; color: #7cd89a;")
        right_title.setStyleSheet("font-weight: 700; color: #f3a2a9;")
        self.leg_titles = [left_title, right_title]
        leg_grid.addWidget(left_title, 0, 0, 1, 2)
        leg_grid.addWidget(right_title, 0, 2, 1, 2)

        for idx in range(4):
            action_combo = QComboBox()
            action_combo.addItems(["", "BUY", "SELL"])
            action_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            strike_combo = QComboBox()
            strike_combo.addItem("")
            strike_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            option_type = "CALL" if idx % 2 == 0 else "PUT"
            self.leg_controls.append((action_combo, strike_combo, option_type))
            row = 1 + idx // 2
            col = (idx % 2) * 2
            leg_grid.addWidget(action_combo, row, col)
            leg_grid.addWidget(strike_combo, row, col + 1)
        leg_grid.setColumnStretch(0, 1)
        leg_grid.setColumnStretch(1, 1)
        leg_grid.setColumnStretch(2, 1)
        leg_grid.setColumnStretch(3, 1)

        self.backtest_box = QFrame()
        self.backtest_box.setObjectName("backtestBox")
        self.backtest_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        backtest_layout = QVBoxLayout(self.backtest_box)
        backtest_layout.setContentsMargins(10, 8, 10, 8)
        backtest_layout.setSpacing(4)
        self.backtest_title = QLabel("Backtest Stats (±2σ)")
        self.backtest_title.setObjectName("backtestTitle")
        backtest_layout.addWidget(self.backtest_title)

        self.bt_start_input = QLineEdit()
        self.bt_start_input.setPlaceholderText("Start YYYY-MM-DD")
        self.bt_end_input = QLineEdit()
        self.bt_end_input.setPlaceholderText("End YYYY-MM-DD")
        today_local = datetime.now(timezone.utc).astimezone().date()
        self.bt_start_input.setText("2018-01-01")
        self.bt_end_input.setText(today_local.strftime("%Y-%m-%d"))
        self.bt_calc_btn = QPushButton("Calc ±2σ")
        self.bt_calc_btn.clicked.connect(self._calculate_backtest_stats)

        bt_input_row = QHBoxLayout()
        bt_input_row.setContentsMargins(0, 0, 0, 0)
        bt_input_row.setSpacing(6)
        bt_input_row.addWidget(QLabel("Weekly Settlement (Weekly Return)"))
        bt_input_row.addStretch(1)
        bt_input_row.addWidget(QLabel("From"))
        bt_input_row.addWidget(self.bt_start_input)
        bt_input_row.addWidget(QLabel("To"))
        bt_input_row.addWidget(self.bt_end_input)
        bt_input_row.addWidget(self.bt_calc_btn)
        backtest_layout.addLayout(bt_input_row)

        bt_grid = QGridLayout()
        bt_grid.setContentsMargins(0, 0, 0, 0)
        bt_grid.setHorizontalSpacing(8)
        bt_grid.setVerticalSpacing(2)
        self.bt_samples_value = QLabel("--")
        self.bt_avg_value = QLabel("--")
        self.bt_std_value = QLabel("--")
        self.bt_band_value = QLabel("--")
        for value_label in (
            self.bt_samples_value,
            self.bt_avg_value,
            self.bt_std_value,
            self.bt_band_value,
        ):
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bt_grid.addWidget(QLabel("Samples"), 0, 0)
        bt_grid.addWidget(self.bt_samples_value, 0, 1)
        bt_grid.addWidget(QLabel("Mean"), 0, 2)
        bt_grid.addWidget(self.bt_avg_value, 0, 3)
        bt_grid.addWidget(QLabel("Std"), 1, 0)
        bt_grid.addWidget(self.bt_std_value, 1, 1)
        bt_grid.addWidget(QLabel("±2σ Band"), 1, 2)
        bt_grid.addWidget(self.bt_band_value, 1, 3)
        backtest_layout.addLayout(bt_grid)

        self.open_ic_btn = QPushButton("Open Strategy")
        self.open_ic_btn.setMinimumWidth(120)
        self.open_ic_btn.clicked.connect(self._open_strategy_position)
        self.close_ic_btn = QPushButton("Close Selected")
        self.close_ic_btn.setMinimumWidth(120)
        self.close_ic_btn.clicked.connect(self._close_selected_position)
        self.delete_ic_btn = QPushButton("Delete Selected")
        self.delete_ic_btn.setMinimumWidth(120)
        self.delete_ic_btn.clicked.connect(self._delete_selected_position)
        self.sim_summary_label = QLabel("Unrealized PnL +0.0000 | Realized PnL +0.0000")
        self.sim_summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sim_summary_label.setWordWrap(True)

        action_grid = QGridLayout()
        action_grid.setContentsMargins(0, 0, 0, 0)
        action_grid.setHorizontalSpacing(8)
        action_grid.setVerticalSpacing(2)
        action_grid.addWidget(QLabel("Qty"), 0, 0)
        action_grid.addWidget(self.ic_qty_spin, 0, 1)
        action_grid.addWidget(self.open_ic_btn, 0, 2)
        action_grid.addWidget(self.close_ic_btn, 0, 3)
        action_grid.addWidget(self.delete_ic_btn, 0, 4)
        action_grid.setRowStretch(1, 1)
        action_grid.addWidget(self.sim_summary_label, 2, 2, 1, 3)
        action_grid.setColumnStretch(5, 1)

        left_panel = QVBoxLayout()
        left_panel.setContentsMargins(0, 0, 0, 0)
        left_panel.setSpacing(2)
        left_panel.addLayout(leg_grid)
        left_panel.addLayout(action_grid, 1)

        sim_main_row = QHBoxLayout()
        sim_main_row.setContentsMargins(0, 0, 0, 0)
        sim_main_row.setSpacing(10)
        sim_main_row.addLayout(left_panel, 4)
        sim_main_row.addWidget(self.backtest_box, 7)
        sim_layout.addLayout(sim_main_row)

        self.pos_table = QTableWidget(0, 9)
        self.pos_table.setObjectName("posTable")
        self.pos_table.setHorizontalHeaderLabels(
            [
                "ID",
                "Status",
                "Qty",
                "Open Px",
                "Puts",
                "Calls",
                "Entry Net",
                "PnL",
                "Opened",
            ]
        )
        self.pos_table.verticalHeader().setVisible(False)
        self.pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.pos_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.pos_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.pos_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.pos_table.setFocusPolicy(Qt.NoFocus)

        chain_header = QHBoxLayout()
        chain_header.setContentsMargins(0, 0, 0, 0)
        chain_header.setSpacing(0)
        self.calls_title = QLabel("Calls")
        self.calls_title.setAlignment(Qt.AlignCenter)
        self.calls_title.setStyleSheet("font-weight: 700; color: #7cd89a;")
        self.strike_title = QLabel("Strike")
        self.strike_title.setAlignment(Qt.AlignCenter)
        self.strike_title.setStyleSheet("font-weight: 700; color: #d8deed;")
        self.puts_title = QLabel("Puts")
        self.puts_title.setAlignment(Qt.AlignCenter)
        self.puts_title.setStyleSheet("font-weight: 700; color: #f3a2a9;")
        chain_header.addWidget(self.calls_title, 6)
        chain_header.addWidget(self.strike_title, 1)
        chain_header.addWidget(self.puts_title, 6)

        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels(
            [
                "Open",
                "Δ|Delta",
                "Annual",
                "Bid",
                "Mark",
                "Ask",
                "Strike",
                "Bid",
                "Mark",
                "Ask",
                "Annual",
                "Δ|Delta",
                "Open",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectItems)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.setWordWrap(True)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._apply_table_widths()

        layout.addWidget(top_bar)
        layout.addLayout(info_row)
        layout.addLayout(chain_header)
        layout.addWidget(self.table, 3)
        layout.addWidget(sim_bar)
        layout.addWidget(self.pos_table, 1)

        self._refresh_positions_table()
        self._on_mode_changed(self.deribit_check.isChecked())
        self._apply_ui_scale(force=True)
        self._toggle_timer(True)

    def _make_info_card(self, title: str, value_label: QLabel, width: int) -> QFrame:
        card = QFrame()
        card.setObjectName("infoCard")
        card.setProperty("baseWidth", width)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 7, 10, 7)
        card_layout.setSpacing(2)

        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        value_label.setObjectName("cardValue")
        card_layout.addWidget(title_label)
        card_layout.addWidget(value_label)
        return card

    def _toggle_timer(self, enabled: bool):
        if enabled:
            self.timer.start(self.refresh_spin.value() * 1000)
        else:
            self.timer.stop()

    def _on_mode_changed(self, deribit_enabled: bool):
        self.horizon_stack.setCurrentIndex(1 if deribit_enabled else 0)
        self.horizon_label.setText("Expiry" if deribit_enabled else "Model Days")
        self.days_spin.setEnabled(not deribit_enabled)
        self.expiry_combo.setEnabled(True)
        self.reload_expiry_btn.setVisible(deribit_enabled)
        self.reload_expiry_btn.setEnabled(deribit_enabled)
        self.wings_spin.setEnabled(True)
        if deribit_enabled:
            symbol = self.symbol_input.text().strip()
            currency = symbol.split("/")[0].strip().upper() if symbol else "BTC"
            self._start_expiry_load(currency)
        self.refresh_data(manual=False)

    def _start_expiry_load(self, currency: str):
        currency = currency.upper()
        cache_ttl_sec = 300.0
        if (
            self._expiry_currency == currency
            and self._expiry_choices
            and (time.monotonic() - self._expiry_loaded_at) < cache_ttl_sec
        ):
            return
        if self._expiry_thread and self._expiry_thread.isRunning():
            return

        self.expiry_combo.blockSignals(True)
        self.expiry_combo.clear()
        self.expiry_combo.addItem("Loading...")
        self.expiry_combo.blockSignals(False)

        self._expiry_thread = QThread(self)
        self._expiry_worker = ExpiryLoadWorker(currency=currency)
        self._expiry_worker.moveToThread(self._expiry_thread)
        self._expiry_thread.started.connect(self._expiry_worker.run)
        self._expiry_worker.finished.connect(lambda choices: self._on_expiry_loaded(currency, choices))
        self._expiry_worker.failed.connect(self._on_expiry_load_failed)
        self._expiry_worker.finished.connect(self._cleanup_expiry_worker)
        self._expiry_worker.failed.connect(self._cleanup_expiry_worker)
        self._expiry_thread.start()

    def _on_expiry_loaded(self, currency: str, choices: List[Tuple[int, str]]):
        if not choices:
            self._on_expiry_load_failed(f"No available expiries on Deribit for {currency}")
            return
        previous_ts = self._selected_expiry_ts()
        self.expiry_combo.blockSignals(True)
        self.expiry_combo.clear()
        for ts, label in choices:
            self.expiry_combo.addItem(label, ts)
        if previous_ts is not None:
            idx = self.expiry_combo.findData(previous_ts)
            if idx >= 0:
                self.expiry_combo.setCurrentIndex(idx)
        self.expiry_combo.blockSignals(False)
        self._expiry_choices = choices
        self._expiry_currency = currency
        self._expiry_loaded_at = time.monotonic()
        if self.deribit_check.isChecked():
            self.refresh_data(manual=False)

    def _on_expiry_load_failed(self, message: str):
        self.expiry_combo.blockSignals(True)
        self.expiry_combo.clear()
        self.expiry_combo.addItem("Failed to load expiries")
        self.expiry_combo.blockSignals(False)
        self.source_label.setText(f"Error: {message}")

    def _cleanup_expiry_worker(self, *_):
        if self._expiry_thread:
            self._expiry_thread.quit()
            self._expiry_thread.wait()
        if self._expiry_worker:
            self._expiry_worker.deleteLater()
        if self._expiry_thread:
            self._expiry_thread.deleteLater()
        self._expiry_worker = None
        self._expiry_thread = None

    def _selected_expiry_ts(self) -> Optional[int]:
        data = self.expiry_combo.currentData()
        if data is None:
            return None
        return int(data)

    def _force_reload_expiries(self):
        symbol = self.symbol_input.text().strip()
        currency = symbol.split("/")[0].strip().upper() if symbol else "BTC"
        self._expiry_currency = ""
        self._expiry_choices = []
        self._expiry_loaded_at = 0.0
        self._start_expiry_load(currency)

    def refresh_data(self, manual: bool = False):
        if self._refresh_thread and self._refresh_thread.isRunning():
            return

        self._active_row = None
        self._active_side = None
        symbol = self.symbol_input.text().strip()
        days = self.days_spin.value()
        wing_count = self.wings_spin.value()
        deribit_enabled = self.deribit_check.isChecked()
        expiry_ts = self._selected_expiry_ts() if deribit_enabled else None
        expiry_label = self.expiry_combo.currentText() if deribit_enabled else ""
        if deribit_enabled and self._expiry_thread and self._expiry_thread.isRunning():
            self.source_label.setText("Loading expiries...")
            return
        if deribit_enabled and expiry_ts is None:
            self.source_label.setText("Please select a Deribit expiry")
            return

        self.refresh_btn.setEnabled(False)
        self._refresh_thread = QThread(self)
        self._refresh_worker = DataFetchWorker(
            symbol=symbol,
            days=days,
            wing_count=wing_count,
            deribit_enabled=deribit_enabled,
            expiry_ts=expiry_ts,
            expiry_label=expiry_label,
        )
        self._refresh_worker.moveToThread(self._refresh_thread)
        self._refresh_thread.started.connect(self._refresh_worker.run)
        self._refresh_worker.finished.connect(self._on_refresh_success)
        self._refresh_worker.failed.connect(lambda msg: self._on_refresh_failed(msg, manual))
        self._refresh_worker.finished.connect(self._cleanup_refresh_worker)
        self._refresh_worker.failed.connect(self._cleanup_refresh_worker)
        self._refresh_thread.start()

    def _on_refresh_success(self, result: RefreshResult):
        self._set_open_headers(result.is_deribit)
        self._render(
            spot=result.spot,
            spot_ccy=result.spot_ccy,
            iv_text=result.iv_text,
            rows=result.rows,
            source_text=result.source,
        )

    def _on_refresh_failed(self, message: str, manual: bool):
        self.source_label.setText(f"Error: {message}")
        now = time.monotonic()
        if manual and now - self._last_error_popup_ts > 5.0:
            self._last_error_popup_ts = now
            QMessageBox.warning(self, "Update Failed", message)

    def _cleanup_refresh_worker(self, *_):
        self.refresh_btn.setEnabled(True)
        if self._refresh_thread:
            self._refresh_thread.quit()
            self._refresh_thread.wait()
        if self._refresh_worker:
            self._refresh_worker.deleteLater()
        if self._refresh_thread:
            self._refresh_thread.deleteLater()
        self._refresh_worker = None
        self._refresh_thread = None

    def _render(self, spot: float, spot_ccy: str, iv_text: str, rows: List[ChainRow], source_text: str):
        self._last_rows = list(rows)

        now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.time_label.setText(now)
        self.spot_label.setText(f"{spot:,.2f} {spot_ccy}")
        self.iv_label.setText(iv_text)
        self.source_label.setText(source_text)
        atm_row = next((x for x in rows if x.is_atm), None)
        if atm_row:
            self.hook_label.setText(f"Call {atm_row.call_annual:.3%} / Put {atm_row.put_annual:.3%}")
        else:
            self.hook_label.setText("--")

        self._populate_table(rows)
        self._refresh_leg_strike_options(rows)
        self._mark_positions_to_market(rows)
        self._refresh_positions_table()

    def _calculate_backtest_stats(self):
        if self._backtest_thread and self._backtest_thread.isRunning():
            return
        symbol = self.symbol_input.text().strip()
        if not symbol:
            QMessageBox.warning(self, "Backtest Failed", "Please input a valid symbol first.")
            return
        start_text = self.bt_start_input.text().strip()
        end_text = self.bt_end_input.text().strip()
        if not start_text or not end_text:
            QMessageBox.warning(self, "Backtest Failed", "Please input both start and end time.")
            return

        self.bt_calc_btn.setEnabled(False)
        self.bt_band_value.setText("Calculating...")
        self._backtest_thread = QThread(self)
        self._backtest_worker = BacktestStatsWorker(
            symbol=symbol,
            start_text=start_text,
            end_text=end_text,
            timeframe="1h",
        )
        self._backtest_worker.moveToThread(self._backtest_thread)
        self._backtest_thread.started.connect(self._backtest_worker.run)
        self._backtest_worker.finished.connect(self._on_backtest_success)
        self._backtest_worker.failed.connect(self._on_backtest_failed)
        self._backtest_worker.finished.connect(self._cleanup_backtest_worker)
        self._backtest_worker.failed.connect(self._cleanup_backtest_worker)
        self._backtest_thread.start()

    def _on_backtest_success(self, result: BacktestStatsResult):
        self.bt_samples_value.setText(
            f"{result.sample_count} (weekly ratio changes, {result.settlement_count} settlements)"
        )
        self.bt_avg_value.setText(f"{result.avg_price:+.3%}")
        self.bt_std_value.setText(f"{result.std_price:.3%}")
        self.bt_band_value.setText(
            f"{result.lower_2sigma:,.2f} ~ {result.upper_2sigma:,.2f} (base {result.base_price:,.2f})"
        )

    def _on_backtest_failed(self, message: str):
        self.bt_band_value.setText(f"Error: {message}")
        QMessageBox.warning(self, "Backtest Failed", message)

    def _cleanup_backtest_worker(self, *_):
        self.bt_calc_btn.setEnabled(True)
        if self._backtest_thread:
            self._backtest_thread.quit()
            self._backtest_thread.wait()
        if self._backtest_worker:
            self._backtest_worker.deleteLater()
        if self._backtest_thread:
            self._backtest_thread.deleteLater()
        self._backtest_worker = None
        self._backtest_thread = None

    def _set_open_headers(self, deribit_mode: bool):
        left = "Open Int" if deribit_mode else "ITM Prob"
        right = "Open Int" if deribit_mode else "ITM Prob"
        self.table.setHorizontalHeaderItem(0, QTableWidgetItem(left))
        self.table.setHorizontalHeaderItem(12, QTableWidgetItem(right))

    def _populate_table(self, rows: List[ChainRow]):
        self.table.setRowCount(len(rows))
        self._apply_table_widths()

        for r, row in enumerate(rows):
            self._repaint_row(r)

    def _row_values(self, row: ChainRow) -> List[str]:
        return [
            self._fmt_open(row.call_open),
            f"{row.call_delta:+.4f}",
            f"{row.call_annual:.3%}",
            f"{row.call_bid:,.4f}",
            f"{row.call_mark:,.4f}",
            f"{row.call_ask:,.4f}",
            f"{row.strike:,.0f}",
            f"{row.put_bid:,.4f}",
            f"{row.put_mark:,.4f}",
            f"{row.put_ask:,.4f}",
            f"{row.put_annual:.3%}",
            f"{row.put_delta:+.4f}",
            self._fmt_open(row.put_open),
        ]

    @staticmethod
    def _fmt_price(value: Optional[float]) -> str:
        if value is None:
            return "--"
        return f"{value:,.4f}"

    @staticmethod
    def _fmt_price_int(value: Optional[float]) -> str:
        if value is None:
            return "--"
        return f"{value:,.0f}"

    @staticmethod
    def _fmt_pnl(value: Optional[float]) -> str:
        if value is None:
            return "--"
        return f"{value:+,.4f}"

    def _build_strike_map(self) -> Dict[float, ChainRow]:
        return {float(row.strike): row for row in self._last_rows}

    @staticmethod
    def _leg_sign(action: str) -> int:
        return 1 if action == "BUY" else -1

    @staticmethod
    def _leg_bucket_key(leg: StrategyLeg) -> Tuple[str, str]:
        return leg.action.upper(), leg.option_type.upper()

    @staticmethod
    def _quote_from_row(row: ChainRow, option_type: str) -> Tuple[float, float, float]:
        if option_type == "CALL":
            return row.call_bid, row.call_ask, row.call_mark
        return row.put_bid, row.put_ask, row.put_mark

    def _refresh_leg_strike_options(self, rows: List[ChainRow]):
        strikes = [f"{row.strike:,.0f}" for row in rows]
        for _, strike_combo, _ in self.leg_controls:
            current_text = strike_combo.currentText()
            strike_combo.blockSignals(True)
            strike_combo.clear()
            strike_combo.addItem("")
            if strikes:
                strike_combo.addItems(strikes)
            if current_text and current_text in strikes:
                idx = strike_combo.findText(current_text)
                if idx >= 0:
                    strike_combo.setCurrentIndex(idx)
            else:
                strike_combo.setCurrentIndex(0)
            strike_combo.blockSignals(False)

    def _mark_positions_to_market(self, rows: List[ChainRow]):
        strike_map = {float(row.strike): row for row in rows}
        for position in self._positions:
            if position.status != "OPEN":
                continue
            if position.legacy_import:
                continue
            leg_pnl = 0.0
            complete = True
            for leg in position.legs:
                row = strike_map.get(float(leg.strike))
                if row is None:
                    complete = False
                    break
                _, _, mark = self._quote_from_row(row, leg.option_type)
                leg.latest_mark = mark
                leg_pnl += self._leg_sign(leg.action) * (mark - leg.entry_price)
            position.last_unrealized_pnl = leg_pnl * position.quantity if complete else None

    def _open_strategy_position(self):
        if not self._last_rows:
            QMessageBox.warning(self, "Open Strategy Failed", "No option chain data to build strategy.")
            return

        strike_map = self._build_strike_map()
        quantity = int(self.ic_qty_spin.value())
        legs: List[StrategyLeg] = []
        entry_cashflow = 0.0
        try:
            for action_combo, strike_combo, option_type in self.leg_controls:
                action = action_combo.currentText().strip().upper()
                strike_text = strike_combo.currentText().replace(",", "").strip()
                if not action and not strike_text:
                    continue
                if not action or not strike_text:
                    raise ValueError("Each used leg must select BUY/SELL and Strike.")
                strike = float(strike_text)
                row = strike_map.get(strike)
                if row is None:
                    raise ValueError(f"Strike {strike:,.0f} is not in current chain.")
                bid, ask, mark = self._quote_from_row(row, option_type)
                entry_price = ask if action == "BUY" else bid
                if entry_price <= 0:
                    raise ValueError(f"{action} {option_type} {strike:,.0f} has invalid quote.")
                entry_cashflow += (-self._leg_sign(action) * entry_price)
                legs.append(
                    StrategyLeg(
                        action=action,
                        option_type=option_type,
                        strike=strike,
                        entry_price=entry_price,
                        latest_mark=mark,
                    )
                )
            if not legs:
                raise ValueError("Please configure at least one leg.")
        except Exception as exc:
            QMessageBox.warning(self, "Open Strategy Failed", str(exc))
            return

        spot_text = self.spot_label.text().strip()
        open_spot: Optional[float] = None
        try:
            open_spot = float(spot_text.split(" ")[0].replace(",", ""))
        except Exception:
            open_spot = None

        now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        position = StrategyPosition(
            position_id=self._next_position_id,
            opened_at=now,
            source=self.source_label.text().strip(),
            quantity=quantity,
            open_spot=open_spot,
            legs=legs,
            entry_cashflow=entry_cashflow,
        )
        self._next_position_id += 1
        self._positions.append(position)
        self._mark_positions_to_market(self._last_rows)
        self._save_positions()
        self._refresh_positions_table()

    def _selected_position_rows(self) -> List[int]:
        model = self.pos_table.selectionModel()
        if model is None:
            return []
        return sorted({idx.row() for idx in model.selectedRows()})

    def _close_selected_position(self):
        selected_rows = self._selected_position_rows()
        if not selected_rows:
            QMessageBox.warning(self, "Close Failed", "Please select one or more simulated positions first.")
            return
        changed = False
        for row in selected_rows:
            if 0 <= row < len(self._positions):
                changed = self._close_position(self._positions[row], persist=False) or changed
        if changed:
            self._save_positions()
        self._refresh_positions_table()

    def _delete_selected_position(self):
        selected_rows = self._selected_position_rows()
        if not selected_rows:
            QMessageBox.warning(self, "Delete Failed", "Please select one or more simulated positions first.")
            return
        for row in sorted(selected_rows, reverse=True):
            if 0 <= row < len(self._positions):
                del self._positions[row]
        self._save_positions()
        self._refresh_positions_table()

    def _close_position(self, position: StrategyPosition, persist: bool = True) -> bool:
        if position.status != "OPEN":
            return False
        if position.legacy_import:
            QMessageBox.warning(
                self,
                "Close Failed",
                f"Position #{position.position_id} is a legacy record with unknown leg entry prices.",
            )
            return False
        strike_map = self._build_strike_map()
        leg_pnl = 0.0
        for leg in position.legs:
            row = strike_map.get(float(leg.strike))
            if row is None:
                QMessageBox.warning(
                    self,
                    "Close Failed",
                    f"Position #{position.position_id} legs are not in current chain window.",
                )
                return False
            _, _, mark = self._quote_from_row(row, leg.option_type)
            leg.close_price = mark
            leg_pnl += self._leg_sign(leg.action) * (mark - leg.entry_price)
        position.realized_pnl = leg_pnl * position.quantity
        position.last_unrealized_pnl = position.realized_pnl
        position.status = "CLOSED"
        position.closed_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if persist:
            self._save_positions()
        return True

    def _refresh_positions_table(self):
        self.pos_table.setRowCount(len(self._positions))
        unrealized = 0.0
        realized = 0.0
        for idx, position in enumerate(self._positions):
            pnl_value: Optional[float]
            if position.status == "OPEN":
                pnl_value = position.last_unrealized_pnl
                if pnl_value is not None:
                    unrealized += pnl_value
            else:
                pnl_value = position.realized_pnl
                if pnl_value is not None:
                    realized += pnl_value

            leg_buckets: Dict[Tuple[str, str], List[str]] = {
                ("BUY", "PUT"): [],
                ("SELL", "PUT"): [],
                ("BUY", "CALL"): [],
                ("SELL", "CALL"): [],
            }
            for leg in position.legs:
                key = self._leg_bucket_key(leg)
                if key in leg_buckets:
                    leg_buckets[key].append(f"{leg.strike:,.0f}")
            puts_text_parts: List[str] = []
            calls_text_parts: List[str] = []
            if leg_buckets[("BUY", "PUT")]:
                puts_text_parts.append(f"B:{'/'.join(leg_buckets[('BUY', 'PUT')])}")
            if leg_buckets[("SELL", "PUT")]:
                puts_text_parts.append(f"S:{'/'.join(leg_buckets[('SELL', 'PUT')])}")
            if leg_buckets[("SELL", "CALL")]:
                calls_text_parts.append(f"S:{'/'.join(leg_buckets[('SELL', 'CALL')])}")
            if leg_buckets[("BUY", "CALL")]:
                calls_text_parts.append(f"B:{'/'.join(leg_buckets[('BUY', 'CALL')])}")
            values = [
                str(position.position_id),
                position.status,
                str(position.quantity),
                self._fmt_price_int(position.open_spot),
                " ".join(puts_text_parts) or "--",
                " ".join(calls_text_parts) or "--",
                self._fmt_price(position.entry_cashflow * position.quantity),
                self._fmt_pnl(pnl_value),
                position.opened_at,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 7 and pnl_value is not None:
                    item.setForeground(QColor("#00d37f" if pnl_value >= 0 else "#ff6378"))
                self.pos_table.setItem(idx, col, item)

        total = unrealized + realized
        self.sim_summary_label.setText(
            f"Unrealized PnL {unrealized:+.4f} | Realized PnL {realized:+.4f} | Total {total:+.4f}"
        )

    def _position_to_dict(self, position: StrategyPosition) -> dict:
        return {
            "position_id": position.position_id,
            "opened_at": position.opened_at,
            "source": position.source,
            "quantity": position.quantity,
            "open_spot": position.open_spot,
            "entry_cashflow": position.entry_cashflow,
            "last_unrealized_pnl": position.last_unrealized_pnl,
            "realized_pnl": position.realized_pnl,
            "closed_at": position.closed_at,
            "status": position.status,
            "legacy_import": position.legacy_import,
            "legs": [
                {
                    "action": leg.action,
                    "option_type": leg.option_type,
                    "strike": leg.strike,
                    "entry_price": leg.entry_price,
                    "latest_mark": leg.latest_mark,
                    "close_price": leg.close_price,
                }
                for leg in position.legs
            ],
        }

    def _position_from_dict(self, data: dict) -> StrategyPosition:
        legs_data = data.get("legs", [])
        legacy_import = False
        legs: List[StrategyLeg] = []
        if isinstance(legs_data, list) and legs_data:
            for raw in legs_data:
                if not isinstance(raw, dict):
                    continue
                legs.append(
                    StrategyLeg(
                        action=str(raw.get("action", "BUY")).upper(),
                        option_type=str(raw.get("option_type", "CALL")).upper(),
                        strike=float(raw.get("strike", 0.0)),
                        entry_price=float(raw.get("entry_price", 0.0)),
                        latest_mark=(None if raw.get("latest_mark") is None else float(raw.get("latest_mark"))),
                        close_price=(None if raw.get("close_price") is None else float(raw.get("close_price"))),
                    )
                )
        else:
            legacy_import = True
            short_put = float(data.get("short_put", 0.0))
            long_put = float(data.get("long_put", 0.0))
            short_call = float(data.get("short_call", 0.0))
            long_call = float(data.get("long_call", 0.0))
            legs = [
                StrategyLeg("SELL", "PUT", short_put, 0.0),
                StrategyLeg("BUY", "PUT", long_put, 0.0),
                StrategyLeg("SELL", "CALL", short_call, 0.0),
                StrategyLeg("BUY", "CALL", long_call, 0.0),
            ]

        parsed_status = str(data.get("status", "OPEN"))
        parsed_qty = int(data.get("quantity", 1))
        parsed_realized = (None if data.get("realized_pnl") is None else float(data.get("realized_pnl")))
        parsed_unrealized = (None if data.get("last_unrealized_pnl") is None else float(data.get("last_unrealized_pnl")))
        if legacy_import and parsed_unrealized is None and parsed_status == "OPEN":
            legacy_entry_credit = data.get("entry_credit")
            legacy_last_close = data.get("last_close_debit")
            if legacy_entry_credit is not None and legacy_last_close is not None:
                try:
                    parsed_unrealized = (float(legacy_entry_credit) - float(legacy_last_close)) * parsed_qty
                except Exception:
                    parsed_unrealized = None

        return StrategyPosition(
            position_id=int(data.get("position_id", 0)),
            opened_at=str(data.get("opened_at", "")),
            source=str(data.get("source", "")),
            quantity=parsed_qty,
            open_spot=(None if data.get("open_spot") is None else float(data.get("open_spot"))),
            legs=legs,
            entry_cashflow=float(data.get("entry_cashflow", data.get("entry_credit", 0.0))),
            last_unrealized_pnl=parsed_unrealized,
            realized_pnl=parsed_realized,
            closed_at=str(data.get("closed_at", "")),
            status=parsed_status,
            legacy_import=bool(data.get("legacy_import", False)) or legacy_import,
        )

    def _load_positions(self):
        if not self._positions_file.exists():
            return
        try:
            payload = json.loads(self._positions_file.read_text(encoding="utf-8"))
            raw_positions = payload.get("positions", [])
            self._positions = [self._position_from_dict(item) for item in raw_positions if isinstance(item, dict)]
            configured_next = int(payload.get("next_position_id", 1))
            max_seen_id = max((p.position_id for p in self._positions), default=0)
            self._next_position_id = max(configured_next, max_seen_id + 1, 1)
        except Exception as exc:
            print(f"Failed to load positions file: {exc}", file=sys.stderr)
            self._positions = []
            self._next_position_id = 1

    def _save_positions(self):
        if not self._positions:
            self._next_position_id = 1
            if self._positions_file.exists():
                self._positions_file.unlink()
            return

        payload = {
            "next_position_id": self._next_position_id,
            "positions": [self._position_to_dict(p) for p in self._positions],
            "updated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp_path = self._positions_file.with_suffix(self._positions_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp_path.replace(self._positions_file)

    def _repaint_row(self, row_idx: int):
        if row_idx < 0 or row_idx >= len(self._last_rows):
            return
        row = self._last_rows[row_idx]
        self.table.setRowHeight(row_idx, self._s(self._base_row_height))
        data = self._row_values(row)
        for c, value in enumerate(data):
            item = self._make_item(row_idx, c, value, row.is_atm)
            self.table.setItem(row_idx, c, item)

    def _s(self, value: int) -> int:
        return max(1, int(round(value * self._ui_scale * self._font_scale)))

    def _apply_ui_scale(self, force: bool = False):
        if self._base_window_w <= 0 or self._base_window_h <= 0:
            return
        scale = min(self.width() / self._base_window_w, self.height() / self._base_window_h)
        scale = min(max(scale, 0.85), 2.0)
        if not force and abs(scale - self._ui_scale) < 1e-6:
            return
        self._ui_scale = scale

        self.top_bar.setMinimumHeight(self._s(64))
        self.top_bar.setMaximumHeight(self._s(64))
        self.symbol_input.setFixedWidth(self._base_symbol_w)
        self.symbol_input.setFixedHeight(self._s(30))
        self.days_spin.setFixedWidth(self._base_horizon_w)
        self.days_spin.setFixedHeight(self._s(30))
        self.expiry_combo.setFixedWidth(self._base_horizon_w)
        self.expiry_combo.setFixedHeight(self._s(30))
        self.horizon_stack.setFixedWidth(self._base_horizon_w)
        self.horizon_stack.setFixedHeight(self._s(30))
        self.wings_spin.setFixedSize(self._s(70), self._s(30))
        self.refresh_spin.setFixedSize(self._s(70), self._s(30))
        self.ic_qty_spin.setFixedSize(self._s(80), self._s(30))
        title_font_px = self._s(12)
        for title in self.leg_titles:
            title.setStyleSheet(f"font-weight: 700; font-size: {title_font_px}px;")
        if len(self.leg_titles) >= 2:
            self.leg_titles[0].setStyleSheet(f"font-weight: 700; color: #7cd89a; font-size: {title_font_px}px;")
            self.leg_titles[1].setStyleSheet(f"font-weight: 700; color: #f3a2a9; font-size: {title_font_px}px;")
        for action_combo, strike_combo, _ in self.leg_controls:
            combo_h = self._s(28)
            action_combo.setMinimumSize(0, combo_h)
            action_combo.setMaximumHeight(combo_h)
            strike_combo.setMinimumSize(0, combo_h)
            strike_combo.setMaximumHeight(combo_h)
        deribit_w = self.deribit_check.sizeHint().width() + self._s(12)
        auto_w = self.auto_check.sizeHint().width() + self._s(12)
        self.deribit_check.setMinimumWidth(deribit_w)
        self.deribit_check.setMaximumWidth(16777215)
        self.auto_check.setMinimumWidth(auto_w)
        self.auto_check.setMaximumWidth(16777215)
        self.reload_expiry_btn.setFixedSize(self._s(128), self._s(30))
        min_toggle_w = (
            self.deribit_check.sizeHint().width()
            + self.auto_check.sizeHint().width()
            + self._s(32)
        )
        self.toggle_box.setFixedSize(max(self._s(self._base_toggle_w), min_toggle_w), self._s(34))
        self.refresh_btn.setFixedSize(self._s(90), self._s(34))
        self.open_ic_btn.setFixedSize(self._s(180), self._s(34))
        self.close_ic_btn.setFixedSize(self._s(170), self._s(34))
        self.delete_ic_btn.setFixedSize(self._s(176), self._s(34))
        self.bt_start_input.setFixedSize(self._s(136), self._s(30))
        self.bt_end_input.setFixedSize(self._s(136), self._s(30))
        self.bt_calc_btn.setFixedSize(self._s(118), self._s(34))
        self.backtest_box.setMinimumHeight(self._s(94))
        self.sim_bar.setMinimumHeight(self._s(138))
        self.sim_bar.setMaximumHeight(self._s(198))
        self.pos_table.setMinimumHeight(self._s(120))
        self.pos_table.setMaximumHeight(self._s(200))
        title_h = self._s(14)
        title_font_px = self._s(12)
        self.calls_title.setFixedHeight(title_h)
        self.strike_title.setFixedHeight(title_h)
        self.puts_title.setFixedHeight(title_h)
        self.calls_title.setStyleSheet(f"font-weight: 700; color: #7cd89a; font-size: {title_font_px}px;")
        self.strike_title.setStyleSheet(f"font-weight: 700; color: #d8deed; font-size: {title_font_px}px;")
        self.puts_title.setStyleSheet(f"font-weight: 700; color: #f3a2a9; font-size: {title_font_px}px;")

        for card in (self.time_card, self.spot_card, self.iv_card, self.hook_card):
            base_w = int(card.property("baseWidth") or 200)
            card.setFixedWidth(self._s(base_w))
        source_base = int(self.source_card.property("baseWidth") or 200)
        self.source_card.setMinimumWidth(self._s(source_base))
        self.source_card.setMaximumWidth(16777215)

        self._apply_table_widths()
        scaled_row_h = self._s(self._base_row_height)
        for i in range(self.table.rowCount()):
            self.table.setRowHeight(i, scaled_row_h)
        header_h = self.table.horizontalHeader().height()
        frame_h = self.table.frameWidth() * 2
        target_table_h = header_h + scaled_row_h * self._base_chain_visible_rows + frame_h + self._s(2)
        self.table.setMinimumHeight(target_table_h)
        self.table.setMaximumHeight(target_table_h)
        self._apply_dark_theme()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_ui_scale()

    def _on_table_cell_clicked(self, row: int, col: int):
        prev_row = self._active_row
        if col <= 5:
            side = "call"
        elif col >= 7:
            side = "put"
        else:
            side = None

        self._active_row = row if side else None
        self._active_side = side
        rows_to_repaint = set()
        if prev_row is not None:
            rows_to_repaint.add(prev_row)
        if self._active_row is not None:
            rows_to_repaint.add(self._active_row)
        for row_idx in rows_to_repaint:
            self._repaint_row(row_idx)

    def closeEvent(self, event):
        self.timer.stop()
        if self._refresh_thread and self._refresh_thread.isRunning():
            self._refresh_thread.quit()
            self._refresh_thread.wait()
        if self._expiry_thread and self._expiry_thread.isRunning():
            self._expiry_thread.quit()
            self._expiry_thread.wait()
        if self._backtest_thread and self._backtest_thread.isRunning():
            self._backtest_thread.quit()
            self._backtest_thread.wait()
        super().closeEvent(event)

    @staticmethod
    def _fmt_open(value: float) -> str:
        if value <= 1.0:
            return f"{value:.2%}"
        return f"{value:,.1f}"

    def _apply_table_widths(self):
        scaled_widths = {col: self._s(width) for col, width in self._base_table_widths.items()}
        total_scaled = sum(scaled_widths.values())
        viewport_w = max(1, self.table.viewport().width())
        fill_ratio = (viewport_w / total_scaled) if total_scaled > 0 else 1.0

        # Keep original ratio, but expand to fill available horizontal space when window is wide.
        if fill_ratio > 1.0:
            fitted = {col: max(1, int(round(width * fill_ratio))) for col, width in scaled_widths.items()}
            diff = viewport_w - sum(fitted.values())
            if diff != 0:
                center_col = 6
                fitted[center_col] = max(1, fitted.get(center_col, 1) + diff)
            scaled_widths = fitted

        for col, width in scaled_widths.items():
            self.table.setColumnWidth(col, width)

    def _make_item(self, row_idx: int, col_idx: int, value: str, is_atm: bool) -> QTableWidgetItem:
        item = QTableWidgetItem(value)
        item.setTextAlignment(Qt.AlignCenter)

        if col_idx <= 5:
            bg = QColor("#18241b") if row_idx % 2 == 0 else QColor("#131d15")
        elif col_idx == 6:
            bg = QColor("#1b1f2a")
        else:
            bg = QColor("#25181b") if row_idx % 2 == 0 else QColor("#1f1316")

        if self._active_row == row_idx:
            if self._active_side == "call" and col_idx <= 5:
                bg = QColor("#2b5a35")
            elif self._active_side == "put" and col_idx >= 7:
                bg = QColor("#5a2730")

        if is_atm and col_idx == 6:
            bg = QColor("#3d2d86")
            item.setText(value)
            item.setForeground(QColor("#ffffff"))
            atm_font = item.font()
            atm_font.setBold(True)
            item.setFont(atm_font)
        elif col_idx in (3, 7):
            item.setForeground(QColor("#00d37f"))
        elif col_idx in (5, 9):
            item.setForeground(QColor("#ff6378"))
        elif col_idx in (1, 11):
            item.setForeground(QColor("#d7ddf2"))
        else:
            item.setForeground(QColor("#c9d1e8"))

        item.setBackground(bg)
        return item

    def _apply_dark_theme(self):
        card_title_size = self._s(11)
        card_value_size = self._s(14)
        control_min_h = self._s(24)
        control_pad_v = self._s(3)
        control_pad_h = self._s(6)
        border_radius = self._s(6)
        button_radius = self._s(4)
        line_radius = self._s(3)
        header_pad = self._s(6)
        self.setStyleSheet(
            f"""
            QWidget {{
                background-color: #0c1016;
                color: #d4dbeb;
            }}
            QLabel {{
                background-color: transparent;
            }}
            #topBar {{
                background-color: #0f1621;
                border: 1px solid #283243;
                border-radius: {border_radius}px;
            }}
            #toggleBox {{
                background-color: #121c2b;
                border: 1px solid #2d3d58;
                border-radius: {border_radius}px;
            }}
            #simBar {{
                background-color: #101726;
                border: 1px solid #283243;
                border-radius: {border_radius}px;
            }}
            #backtestBox {{
                background-color: #0f1c2b;
                border: 1px solid #2a3d58;
                border-radius: {border_radius}px;
            }}
            #backtestTitle {{
                color: #9cb6df;
                font-weight: 700;
            }}
            #infoCard {{
                background-color: #101a28;
                border: 1px solid #283243;
                border-radius: {border_radius}px;
            }}
            #cardTitle {{
                color: #8fa1bf;
                font-size: {card_title_size}px;
            }}
            #cardValue {{
                color: #d9e3f5;
                font-size: {card_value_size}px;
                font-weight: 600;
            }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                background-color: #121826;
                border: 1px solid #31405a;
                border-radius: {line_radius}px;
                padding: {control_pad_v}px {control_pad_h}px;
                min-height: {control_min_h}px;
            }}
            QPushButton {{
                background-color: #2463eb;
                color: #ffffff;
                border: none;
                border-radius: {button_radius}px;
                padding: {self._s(4)}px {self._s(10)}px;
                min-height: {control_min_h}px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: #2d73ff;
            }}
            QCheckBox {{
                spacing: 6px;
                padding-right: 2px;
            }}
            QHeaderView::section {{
                background-color: #0f1621;
                color: #98a5bf;
                border: 0;
                border-bottom: 1px solid #283243;
                padding: {header_pad}px;
            }}
            QTableWidget {{
                border: 1px solid #283243;
                gridline-color: #273041;
                selection-background-color: #2a3a56;
                selection-color: #ffffff;
            }}
            QTableWidget::item:selected {{
                background-color: transparent;
                color: inherit;
            }}
            #posTable::item:selected {{
                background-color: #2a3a56;
                color: #ffffff;
            }}
            """
        )


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
