# IFVG Sniper - Backtest & Live Scanner

Created & maintained by **Kishore**

Python port of the "IFVG Sniper Entry Engine" Pine v6 indicator. Tests the top volume Binance USDT pairs on 1m, 5m, 15m, 30m, 1h and 4h at 1R / 2R / 3R, with win rate and PnL on 1,00,000 capital.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit app: Naresh 4-step IFVG strategy (Best timeframes, Live signals, Chart, Rules tabs) |
| `ifvg_engine.py` | IFVG Sniper engine and Binance data loader (used by `run_backtest.py`, not by the new `app.py`) |
| `charts.py` | IFVG Sniper signal chart (not used by the new `app.py`) |
| `run_backtest.py` | Headless IFVG Sniper backtest, writes `results/` |
| `.github/workflows/backtest.yml` | Run the IFVG Sniper backtest on GitHub from your phone |

## Streamlit

Main file: `app.py`. Requirements: `requirements.txt`.

`app.py` is now the **Learn & Trade With Naresh IFVG 4-step checklist** strategy:

1. Trend (EMA 50 vs 200)
2. Liquidity sweep (swing high/low wicked and reclaimed)
3. Confirmation FVG after the IFVG forms
4. Wick rejection on the first retest only

Extras (toggles): first/manipulation gap, speed of breakout. It backtests the top 30 Binance Vision spot USDT coins by 24h volume, XAUTUSDT and silver (Yahoo SI=F) on all selected timeframes at 1R / 1.5R / 2R / 3R, ranks timeframes by win rate and average R, and keeps out-of-sample (last 40%) numbers separate. Money: each coin/timeframe/RR is a separate Rs 1,00,000 account and every SL hit loses 4% of the starting capital (Rs 4,000, plus fees), fixed with no compounding. Both numbers are editable in Settings. Results show PnL in rupees, ROI %, max drawdown % and the position size as a multiple of capital. The previous IFVG Sniper Streamlit app is still in git history (commit e58c83c).

## Live scanner (previous IFVG Sniper app, see commit e58c83c)

- **Auto-scan**: with the toggle on, the page checks every 15 seconds and scans again 3 seconds after a candle closes on the smallest selected timeframe. New OPEN signals pop up as a toast. It only runs while the page is open.
- **Signal chart**: pick a signal to see candles with ENTRY, SL and TP lines, the profit zone (green) and loss zone (red), and where the trade ended if TP or SL was already touched. Times are IST.
- **Status**: OPEN (SL and TP not touched yet), TP hit, SL hit, or BLOCKED (another trade of that coin/timeframe was still open, so the backtest would skip it). Same one-trade-at-a-time rule as the backtest.
- **Fee (R)** column and the line under the chart show the real result after fees, e.g. TP = +1.9R instead of +2R.

## Backtest on GitHub (no PC)

Actions tab -> IFVG Backtest -> Run workflow. Results land in `results/summary.md` and `results/backtest_results.csv`.

## How PnL is calculated

- Each coin starts with 1,00,000. Risk per trade = 1% (1,000), fixed, no compounding.
- Position size = risk / (SL distance %), capped at 5x capital.
- Fee 0.1% per side (= 0.001, the Binance spot fee) on position size, charged on entry and exit (0.2% round trip). Change it in the sidebar, or `--fee-side-pct` / the workflow input. Note the box is in percent: 0.1 means 0.001.
- Gross = +RR x risk on a win, -1 x risk on a loss. Net = gross - fees.
- One trade at a time per coin. If TP and SL are hit in the same candle, SL counts first.

Low timeframes (1m, 5m) have a small SL distance, so fees take a larger share of each trade's risk. Compare the Gross PnL and Net PnL columns to see what the fee costs.

## Compare with TradingView

The Pine indicator now keeps closed trades on the chart with a result label (TP hit / SL hit) and shows the win rate in its dashboard. The app has a **Trade log** (Backtest tab) listing every trade with time, levels and result. For the same trades, use the same entry mode on both sides: the Pine default is `IFVG Line`, the app default is `Confirmation Close`.
