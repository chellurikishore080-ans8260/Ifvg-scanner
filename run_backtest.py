#!/usr/bin/env python3
"""
Headless IFVG backtest: top-N Binance USDT pairs by volume x 1m/5m/15m/30m/1h/4h x 1R/2R/3R.
Created & maintained by Kishore

Writes results/backtest_results.csv (per coin) and results/summary.md (per timeframe).
Used by .github/workflows/backtest.yml so it can be run from a phone, no PC needed.
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pandas as pd

import ifvg_engine as E

BARS = {"1m": 10000, "5m": 10000, "15m": 10000, "30m": 10000, "1h": 10000, "4h": 5000}


def work(args):
    sym, tf, bars, rrs, params = args
    try:
        df = E.fetch_klines(sym, tf, bars)
        if df is None or len(df) < 100:
            return sym, tf, [], "not enough candles"
        res, nsig, _ = E.backtest_df(df, rrs, params)
        rows = [dict(coin=sym[:-4], timeframe=tf, rr=rr, bars=len(df), signals=nsig, **st) for rr, (st, _t) in res.items()]
        return sym, tf, rows, None
    except Exception as e:
        return sym, tf, [], f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=100)
    ap.add_argument("--timeframes", default=",".join(E.TFS))
    ap.add_argument("--rr", default="1,2,3")
    ap.add_argument("--filter", default="Balanced", choices=["Balanced", "Loose", "Strict", "Off"])
    ap.add_argument("--entry-mode", default="Confirmation Close", choices=["Confirmation Close", "IFVG Line"])
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--fee-side-pct", type=float, default=0.01, help="fee per side in %%, charged on entry and exit")
    ap.add_argument("--max-leverage", type=float, default=5.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="results")
    a = ap.parse_args()

    tfs = [t.strip() for t in a.timeframes.split(",") if t.strip()]
    rrs = [int(x) for x in a.rr.split(",")]
    params = dict(E.DEFAULT_PARAMS, filter=a.filter, entry_mode=a.entry_mode, capital=a.capital,
                  risk_pct=a.risk_pct, fee_side_pct=a.fee_side_pct, max_leverage=a.max_leverage)
    os.makedirs(a.out, exist_ok=True)

    symbols = E.get_top_symbols(a.coins)
    print(f"Top {len(symbols)} by volume: {', '.join(s[:-4] for s in symbols)}", flush=True)

    tasks = [(s, tf, BARS[tf], rrs, params) for s in symbols for tf in tfs]
    rows, fails = [], []
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        for k, (sym, tf, r, err) in enumerate(pool.map(work, tasks), 1):
            if err:
                fails.append(f"{sym} {tf}: {err}")
                print(f"[fail] {sym} {tf}: {err}", flush=True)
            rows.extend(r)
            if k % 25 == 0 or k == len(tasks):
                print(f"[{k}/{len(tasks)}] done", flush=True)

    if not rows:
        sys.exit("No results produced. " + (fails[0] if fails else ""))
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(a.out, "backtest_results.csv"), index=False)
    agg = E.aggregate(rows)
    agg.to_csv(os.path.join(a.out, "summary_by_timeframe.csv"), index=False)

    lines = [
        "# IFVG Sniper - Backtest Summary",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | top {len(symbols)} Binance USDT pairs by 24h volume | "
        f"filter {a.filter} | entry {a.entry_mode}",
        f"Each coin starts with {a.capital:,.0f}. Risk {a.risk_pct}% per trade (fixed, no compounding), fee {a.fee_side_pct}% per side, "
        f"max leverage {a.max_leverage}x, SL = 1.5 x ATR(14).",
        f"Candles per coin: " + ", ".join(f"{t} {BARS[t]}" for t in tfs),
        "",
        "| TF | RR | Coins | Trades | Win % | Avg net PnL / coin | Median PnL / coin | Total net PnL | Gross PnL | Fees | Profitable coins |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in agg.iterrows():
        lines.append(f"| {r.timeframe} | {r.rr} | {r.coins} | {r.trades} | {r.win_rate} | {r.avg_pnl_per_coin:,.0f} | "
                     f"{r.median_pnl_per_coin:,.0f} | {r.total_pnl_all_coins:,.0f} | {r.gross_pnl:,.0f} | {r.fees:,.0f} | {r.profitable_coins} |")
    if fails:
        lines += ["", f"{len(fails)} coin/timeframe jobs failed (first 10):", ""] + [f"- {f}" for f in fails[:10]]
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
