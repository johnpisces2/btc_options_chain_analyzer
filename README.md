# BTC Options Chain Analyzer

`BTC Options Chain Analyzer` 是一個用 Python + PySide6 製作的桌面工具，整合：
- Binance 模型選擇權鏈（理論定價）
- Deribit 真實選擇權鏈（即時報價）
- 4 腿自訂策略模擬倉（可開倉 / 平倉 / 刪除 / 損益追蹤）

## 功能總覽
- 雙模式報價：`Binance Model` / `Deribit Chain`
- T 字報價表：`Calls | Strike | Puts`
- 以 ATM 為中心顯示上下翼（`ATM Wings`）
- 可調整自動刷新（`Refresh Sec`）
- 4 腿策略組合器：
  - 左側 `Calls`、右側 `Puts`
  - 每腿可選 `BUY/SELL` 與 `Strike`
  - 可留空，支援 1~4 腿策略
- 模擬倉紀錄：
  - 欄位包含 `Puts`, `Calls`, `Entry Net`, `PnL`
  - 支援多選（Ctrl/Shift）後批次 `Close Selected` / `Delete Selected`
- 損益統計：
  - `Unrealized PnL`
  - `Realized PnL`
  - `Total`

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
