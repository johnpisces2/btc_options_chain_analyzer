import json
import math
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
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

TRADING_DAYS = 365.0
RISK_FREE_RATE = 0.0


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


class DeribitAnalyzer:
    BASE_URL = "https://www.deribit.com/api/v2"
    _VOL_PERCENTILE_CACHE: Dict[str, Tuple[float, str]] = {}

    def __init__(self, currency: str):
        self.currency = currency.upper()

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
            label = f"{dt.strftime('%d %b %Y')} ({left_days}d {left_h}h)"
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

        end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ts = end_ts - lookback_days * 24 * 3600 * 1000
        payload = self._get(
            "public/get_volatility_index_data",
            currency=self.currency,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            resolution="1D",
        )

        rows = payload.get("data", [])
        closes: List[float] = []
        for row in rows:
            close_value = None
            if isinstance(row, dict):
                close_value = row.get("close")
            elif isinstance(row, list) and len(row) >= 5:
                close_value = row[4]
            if close_value is None:
                continue
            closes.append(float(close_value))

        if not closes:
            raise ValueError("No volatility-index data from Deribit")

        current_val = closes[-1]
        percentile = 100.0 * sum(1 for x in closes if x <= current_val) / len(closes)
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BTC Options Chain Analyzer")
        self.resize(1360, 870)
        self._base_window_w = 1360
        self._base_window_h = 870
        self._ui_scale = 1.0
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
        self._base_symbol_w = 130
        self._base_horizon_w = 190
        self._base_toggle_w = 190
        self._expiry_choices: List[Tuple[int, str]] = []
        self._expiry_currency: str = ""
        self._expiry_loaded_at: float = 0.0
        self._last_rows: List[ChainRow] = []
        self._active_row: Optional[int] = None
        self._active_side: Optional[str] = None
        self._refresh_thread: Optional[QThread] = None
        self._refresh_worker: Optional[DataFetchWorker] = None
        self._expiry_thread: Optional[QThread] = None
        self._expiry_worker: Optional[ExpiryLoadWorker] = None
        self._last_error_popup_ts: float = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh_data(manual=False))

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
        self.wings_spin.setValue(7)
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
        self.iv_card = self._make_info_card("IV", self.iv_label, 300)
        self.hook_card = self._make_info_card("ATM Annual (Bid)", self.hook_label, 280)
        self.source_card = self._make_info_card("Source", self.source_label, 420)
        self.source_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        info_row.addWidget(self.time_card)
        info_row.addWidget(self.spot_card)
        info_row.addWidget(self.iv_card)
        info_row.addWidget(self.hook_card)
        info_row.addWidget(self.source_card, 1)

        chain_header = QHBoxLayout()
        calls_title = QLabel("Calls")
        calls_title.setAlignment(Qt.AlignCenter)
        calls_title.setStyleSheet("font-weight: 700; color: #7cd89a; font-size: 14px;")
        strike_title = QLabel("Strike")
        strike_title.setAlignment(Qt.AlignCenter)
        strike_title.setStyleSheet("font-weight: 700; color: #d8deed; font-size: 14px;")
        puts_title = QLabel("Puts")
        puts_title.setAlignment(Qt.AlignCenter)
        puts_title.setStyleSheet("font-weight: 700; color: #f3a2a9; font-size: 14px;")
        chain_header.addWidget(calls_title, 6)
        chain_header.addWidget(strike_title, 1)
        chain_header.addWidget(puts_title, 6)

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
        self._apply_table_widths()

        layout.addWidget(top_bar)
        layout.addLayout(info_row)
        layout.addLayout(chain_header)
        layout.addWidget(self.table)

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
        return max(1, int(round(value * self._ui_scale)))

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
        deribit_w = self.deribit_check.sizeHint().width() + self._s(12)
        auto_w = self.auto_check.sizeHint().width() + self._s(12)
        self.deribit_check.setMinimumWidth(deribit_w)
        self.deribit_check.setMaximumWidth(16777215)
        self.auto_check.setMinimumWidth(auto_w)
        self.auto_check.setMaximumWidth(16777215)
        self.reload_expiry_btn.setFixedSize(self._s(92), self._s(30))
        min_toggle_w = (
            self.deribit_check.sizeHint().width()
            + self.auto_check.sizeHint().width()
            + self._s(32)
        )
        self.toggle_box.setFixedSize(max(self._s(self._base_toggle_w), min_toggle_w), self._s(34))
        self.refresh_btn.setFixedSize(self._s(90), self._s(34))

        for card in (self.time_card, self.spot_card, self.iv_card, self.hook_card):
            base_w = int(card.property("baseWidth") or 200)
            card.setFixedWidth(self._s(base_w))
        source_base = int(self.source_card.property("baseWidth") or 200)
        self.source_card.setMinimumWidth(self._s(source_base))
        self.source_card.setMaximumWidth(16777215)

        self._apply_table_widths()
        for i in range(self.table.rowCount()):
            self.table.setRowHeight(i, self._s(self._base_row_height))
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
            QLineEdit, QSpinBox {{
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
            """
        )


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
