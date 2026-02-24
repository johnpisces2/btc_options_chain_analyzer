# BTC Options Chain Analyzer

`BTC Options Chain Analyzer` 是一個用 Python + PySide6 製作的桌面工具，整合：
- Binance 模型選擇權鏈（理論定價）
- Deribit 真實選擇權鏈（即時報價）
- 4 腿自訂策略模擬倉（可開倉 / 平倉 / 刪除 / 損益追蹤）

## 功能總覽
- 雙模式報價：`Binance Model` / `Deribit Chain`
- T 字報價表：`Calls | Strike | Puts`
- 以 ATM 為中心顯示上下翼（`ATM Wings`）
- Deribit DVOL 歷史資料擷取（預設使用近 1 年資料計算百分位）
- IV 百分位計算與摘要顯示（例如 `DVOL L:45.20/ CUR:51.93 / H:62.10 (85.0%, 1Y)`）
- 可調整自動刷新（`Refresh Sec`）
- 4 腿策略組合器：
  - 左側 `Calls`、右側 `Puts`
  - 每腿可選 `BUY/SELL` 與 `Strike`
  - 可留空，支援 1~4 腿策略
- 週結算回測統計（`Backtest Stats (IC ±2σ)`）：
  - 輸入回測區間後，使用 `週結算(週變動比例)` 方式計算
  - 以每週五 `08:00 UTC` 為結算錨點，取該時點（或之後第一根）`1h close`
  - 輸出 `Samples`, `Mean`, `Std`, `±2σ Band`, `base`
  - 依 `±2σ` 價格帶映射目前鏈上履約價，提供 `IC Ref`（`SELL PUT` / `SELL CALL`）參考
- 模擬倉紀錄：
  - 欄位包含 `Puts`, `Calls`, `Entry Net`, `PnL`
  - 支援多選（Ctrl/Shift）後批次 `Close Selected` / `Delete Selected`
- 損益統計：
  - `Unrealized PnL`
  - `Realized PnL`
  - `Total`

## GUI 欄位說明
- `Time`：最近一次刷新時間（本地時間）
- `Spot`：標的現價
- `IV`：
  - Deribit 模式：`DVOL L:/ CUR:/ H: (percentile, 1Y)`，其中 `L/H` 為近 30 天低/高值
  - Binance 模式：`30D HV as IV proxy`
- `ATM Annual (Bid)`：ATM 年化 Bid（依目前模式與鏈上資料計算）
- `Source`：目前資料來源與到期日資訊（Deribit 模式）
- `Backtest Stats (IC ±2σ)`：
  - `Samples`：週變動比例樣本數（括號內顯示使用的週結算點數）
  - `Mean` / `Std`：週變動比例的平均與標準差
  - `±2σ Band`：以 `base` 套用 `mean ± 2*std` 後換算的下週參考價格帶
  - `IC Ref`：以價格帶對應目前鏈上最接近的 `SELL PUT` / `SELL CALL` 履約價與 Delta/Bid

## 策略資料持久化
- 交易紀錄儲存在：`strategy_positions.json`
- 程式啟動會自動載入，狀態變動會自動寫回
- 若刪除全部紀錄，檔案會自動刪除，ID 下次從 1 開始

## 安裝
```bash
pip install -r requirements.txt
```

## 執行
```bash
python main.py
```

## 注意事項
- 本工具僅供分析與模擬，不構成投資建議。
- Binance 模式為模型估算，非市場真實 IV。
- Deribit 模式依賴外部 API 與網路狀態。
- `Backtest Stats` 的 `base` 為回測區間內最後一個週結算採樣價，不是實際手動開倉價。

## IV 歷史資料 API（程式內）
- 檔案：`iv_history.py`
- 類別：`DeribitIVHistory`
- 端點：
  - `public/get_historical_volatility`
  - `public/get_volatility_index_data`（DVOL）
- 主要方法：
  - `fetch_historical_volatility(timeframe_days=30)`：取得歷史波動率資料
  - `fetch_dvol_index_history(timeframe_days=30, resolution="1D")`：取得 DVOL 歷史資料
  - `calculate_percentile(points, current_value=None)`：計算目前值在樣本中的百分位
