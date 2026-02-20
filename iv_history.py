import csv
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class IVPoint:
    timestamp_ms: int
    iv: float


class DeribitIVHistory:
    BASE_URL = "https://www.deribit.com/api/v2"

    def __init__(self, currency: str, storage_dir: Path):
        self.currency = currency.upper()
        self.storage_dir = storage_dir

    def _get(self, method: str, **params):
        query = urllib.parse.urlencode(params)
        url = f"{self.BASE_URL}/{method}?{query}"
        with urllib.request.urlopen(url, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("error"):
            raise ValueError(f"Deribit API error: {payload['error']}")
        return payload.get("result")

    def fetch_historical_volatility(self, timeframe_days: int = 30) -> List[IVPoint]:
        """Fetch Deribit historical volatility points and keep the last timeframe window."""
        result = self._get("public/get_historical_volatility", currency=self.currency)
        rows = result if isinstance(result, list) else []
        points: List[IVPoint] = []
        for row in rows:
            ts = None
            value = None
            if isinstance(row, dict):
                ts = row.get("timestamp") or row.get("time")
                value = row.get("value") or row.get("volatility")
            elif isinstance(row, list) and len(row) >= 2:
                ts = row[0]
                value = row[1]
            if ts is None or value is None:
                continue
            points.append(IVPoint(timestamp_ms=int(ts), iv=float(value)))

        return self._filter_timeframe(points, timeframe_days=timeframe_days)

    def fetch_dvol_index_history(self, timeframe_days: int = 30, resolution: str = "1D") -> List[IVPoint]:
        """Fetch DVOL history via Deribit volatility index endpoint."""
        end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ts = end_ts - int(timeframe_days) * 24 * 3600 * 1000
        result = self._get(
            "public/get_volatility_index_data",
            currency=self.currency,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            resolution=resolution,
        )

        rows = result.get("data", []) if isinstance(result, dict) else []
        points: List[IVPoint] = []
        for row in rows:
            ts = None
            close_value = None
            if isinstance(row, dict):
                ts = row.get("timestamp") or row.get("time")
                close_value = row.get("close")
            elif isinstance(row, list) and len(row) >= 5:
                ts = row[0]
                close_value = row[4]
            if ts is None or close_value is None:
                continue
            points.append(IVPoint(timestamp_ms=int(ts), iv=float(close_value)))
        return points

    def _filter_timeframe(self, points: Iterable[IVPoint], timeframe_days: int) -> List[IVPoint]:
        ordered = sorted(points, key=lambda x: x.timestamp_ms)
        if not ordered:
            return []
        end_ts = ordered[-1].timestamp_ms
        start_ts = end_ts - int(timeframe_days) * 24 * 3600 * 1000
        return [x for x in ordered if x.timestamp_ms >= start_ts]

    @staticmethod
    def calculate_percentile(points: List[IVPoint], current_value: Optional[float] = None) -> float:
        if not points:
            raise ValueError("Cannot calculate percentile from empty IV history")
        values = [x.iv for x in points]
        target = values[-1] if current_value is None else float(current_value)
        return 100.0 * sum(1 for x in values if x <= target) / len(values)

    def store_history(
        self,
        points: List[IVPoint],
        source: str,
        timeframe_days: int,
        file_format: str = "json",
    ) -> Path:
        if file_format not in {"json", "csv"}:
            raise ValueError("file_format must be 'json' or 'csv'")

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = (
            f"{self.currency.lower()}_{source.lower()}_{int(timeframe_days)}d_{stamp}.{file_format}"
        )
        out_path = self.storage_dir / filename

        if file_format == "json":
            payload = [
                {
                    "timestamp_ms": x.timestamp_ms,
                    "timestamp_iso": datetime.fromtimestamp(x.timestamp_ms / 1000, tz=timezone.utc).isoformat(),
                    "iv": x.iv,
                }
                for x in points
            ]
            out_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        else:
            with out_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.writer(fp)
                writer.writerow(["timestamp_ms", "timestamp_iso", "iv"])
                for x in points:
                    writer.writerow(
                        [
                            x.timestamp_ms,
                            datetime.fromtimestamp(x.timestamp_ms / 1000, tz=timezone.utc).isoformat(),
                            x.iv,
                        ]
                    )

        return out_path
