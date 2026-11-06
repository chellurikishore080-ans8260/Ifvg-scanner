# IFVG Sniper - Backtest & Live Scanner

Created & maintained by **Kishore**

Python port of the "IFVG Sniper Entry Engine" Pine v6 indicator. Tests the top volume Binance USDT pairs on 1m, 5m, 15m, 30m, 1h and 4h at 1R / 2R / 3R, with win rate and PnL on 1,00,000 capital.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit app: Backtest tab and Live scanner tab |
| `ifvg_engine.py` | Strategy engine and Binance data loader |
| `run_backtest.py` | Headless backtest, writes `results/` |
| `.github/workflows/backtest.yml` | Run the backtest on GitHub from your phone |

## Streamlit

Main file: `app.py`. Requirements: `requirements.txt`.

## Backtest on GitHub (no PC)

Actions tab -> IFVG Backtest -> Run workflow. Results land in `results/summary.md` and `results/backtest_results.csv`.

## How PnL is calculated

- Each coin starts with 1,00,000. Risk per trade = 1% (1,000), fixed, no compounding.
- Position size = risk / (SL distance %), capped at 5x capital.
- Fee 0.10% per side on position size, charged on entry and exit.
- Gross = +RR x risk on a win, -1 x risk on a loss. Net = gross - fees.
- One trade at a time per coin. If TP and SL are hit in the same candle, SL counts first.

Low timeframes (1m, 5m) have a small SL distance, so fees take a large share of each trade's risk. Check the Fees column.
