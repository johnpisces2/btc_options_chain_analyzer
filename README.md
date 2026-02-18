# BTC Options Chain Analyzer

Desktop options-chain analyzer built with Python + PySide6.

It supports two modes:
- Deribit real chain (public API)
- Binance modeled chain (spot + 30D historical volatility proxy)

## Features
- Deribit-style options chain layout: `Calls | Strike | Puts`
- ATM-centered strike display with configurable wings
- Per-strike hook annualized yields in center column: `P ... | C ...`
- Side highlight interaction:
  - click left (Calls) -> highlight only left side of that row
  - click right (Puts) -> highlight only right side of that row
- Auto refresh with configurable interval
- Background data loading (non-blocking UI)

## Data Modes

### 1) Deribit Chain
- Uses real options data from Deribit public endpoints
- Select expiry from dropdown (`Expiry`)
- `ATM Wings` controls number of strikes above/below ATM
- `Reload Exp` refreshes available expiries (with cache bypass)
- `Spot` is displayed in `USD`

### 2) Binance Model
- Uses Binance spot data via `ccxt`
- Uses 30-day historical volatility (HV) as IV proxy
- Strike grid is fixed at:
  - step = `1000`
  - range = `ATM +/- ATM Wings`
- `Model Days` affects modeled premium, delta, probability, and annualized metrics
- `Spot` currency follows symbol quote (for example, `BTC/USDT` -> `USDT`)

## Column Semantics
- `Open Int` (Deribit mode): open interest
- `ITM Prob` (Binance model mode): modeled in-the-money probability
- `Delta`: option delta
- `Annual`: annualized return based on mark price
- `Bid/Mark/Ask`: bid/mark/ask price
- `Strike / Hook Annual`: strike + hook annualized yields (`P` for sell put, `C` for sell call)

## Install
```bash
pip install -r requirements.txt
```

## Run
```bash
python main.py
```

## Notes
- This tool is for analysis only, not investment advice.
- Binance mode is model-based and not equal to market implied volatility.
- Deribit mode depends on network/API availability.
