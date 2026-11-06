"""
IFVG Sniper - Backtest + Live Scanner (Top-100 Binance USDT pairs by volume)
Created & maintained by Kishore

Run:  pip install -r requirements.txt
      streamlit run app.py
"""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

import ifvg_engine as E

st.set_page_config(page_title="IFVG Sniper", layout="wide")
st.title("IFVG Sniper - Backtest & Live Scanner")
st.caption("Top volume Binance USDT pairs | timeframes 1m 5m 15m 30m 1h 4h | win rate and PnL on 1,00,000 capital")


# ───────────────────────── cached data ─────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def cached_symbols(n):
    return E.get_top_symbols(n)


@st.cache_data(ttl=600, show_spinner=False)
def cached_klines(symbol, tf, bars):
    return E.fetch_klines(symbol, tf, bars)


# ───────────────────────── sidebar ─────────────────────────
with st.sidebar:
    st.header("Strategy")
    flt = st.selectbox("IFVG filter", ["Balanced", "Loose", "Strict", "Off"], index=0)
    entry_mode = st.selectbox("Entry price", ["Confirmation Close", "IFVG Line"], index=0,
                              help="IFVG Line is the Pine default but is optimistic: price has already closed beyond the line "
                                   "when the signal fires. Confirmation Close is the realistic fill.")
    sl_mult = st.number_input("SL = ATR x", 0.1, 10.0, 1.5, 0.1)
    atr_len = st.number_input("ATR length", 1, 100, 14)

    st.header("Capital / costs")
    capital = st.number_input("Capital per coin (1 lakh = 100000)", 1000.0, 100_000_000.0, 100_000.0, 1000.0)
    risk_pct = st.number_input("Risk per trade (% of capital)", 0.1, 10.0, 1.0, 0.1)
    fee_side = st.number_input("Fee per side (%)", 0.0, 1.0, 0.10, 0.01,
                               help="Binance spot taker = 0.10% per side. Charged on entry and exit.")
    max_lev = st.number_input("Max leverage (position cap)", 1.0, 50.0, 5.0, 1.0)

params = dict(E.DEFAULT_PARAMS, filter=flt, entry_mode=entry_mode, sl_mult=sl_mult, atr_len=int(atr_len),
              capital=capital, risk_pct=risk_pct, fee_side_pct=fee_side, max_leverage=max_lev)

tab_bt, tab_live = st.tabs(["Backtest", "Live scanner"])


def fmt_money(x):
    return f"{x:,.0f}"


# ───────────────────────── BACKTEST ─────────────────────────
with tab_bt:
    c1, c2, c3 = st.columns(3)
    n_coins = c1.slider("Top coins by volume", 5, 100, 100, 5)
    tfs = c2.multiselect("Timeframes", E.TFS, default=E.TFS)
    rrs = c3.multiselect("Take profit RR", [1, 2, 3, 4, 5], default=[1, 2, 3], format_func=lambda x: f"{x}R")
    bars = st.slider("Candles per coin per timeframe", 1000, 20000, 5000, 1000,
                     help="1m: 5000 candles is about 3.5 days. More candles = more trades but slower.")
    st.caption("Each coin is tested separately starting from the capital above. Fixed risk per trade, no compounding.")

    if st.button("Run backtest", type="primary"):
        if not tfs or not rrs:
            st.error("Select at least one timeframe and one RR.")
            st.stop()
        try:
            with st.spinner("Loading top volume pairs from Binance..."):
                symbols = cached_symbols(n_coins)
        except E.DataError as e:
            st.error(f"Could not load pairs. {e}")
            st.info("Binance blocks some regions/servers. This app uses data-api.binance.vision first; if this still "
                    "fails, run the app from India or use the GitHub Actions backtest in this repo.")
            st.stop()

        tasks = [(s, tf) for s in symbols for tf in tfs]
        rows, fails = [], []
        prog = st.progress(0.0, text="Starting...")

        def work(task):
            sym, tf = task
            try:
                df = cached_klines(sym, tf, bars)
                if df is None or len(df) < 100:
                    return task, None, "not enough candles"
                res, nsig, filt = E.backtest_df(df, rrs, params)
                return task, (res, len(df), nsig), None
            except Exception as e:  # keep going, show failures at the end
                return task, None, f"{type(e).__name__}: {e}"

        done = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            for task, out, err in pool.map(work, tasks):
                done += 1
                prog.progress(done / len(tasks), text=f"{done}/{len(tasks)}  {task[0]} {task[1]}")
                if err:
                    fails.append(f"{task[0]} {task[1]}: {err}")
                    continue
                res, nbars, nsig = out
                for rr, (st_, _tr) in res.items():
                    rows.append(dict(coin=task[0][:-4], timeframe=task[1], rr=rr, bars=nbars, signals=nsig, **st_))
        prog.empty()
        st.session_state["bt_rows"] = rows
        st.session_state["bt_fails"] = fails
        st.session_state["bt_cap"] = capital

    rows = st.session_state.get("bt_rows")
    if rows:
        cap = st.session_state.get("bt_cap", capital)
        agg = E.aggregate(rows)
        st.subheader(f"Per timeframe summary (each coin starts with {fmt_money(cap)})")
        st.dataframe(agg.rename(columns={
            "timeframe": "TF", "rr": "RR", "coins": "Coins", "trades": "Trades", "win_rate": "Win %",
            "avg_pnl_per_coin": "Avg net PnL / coin", "median_pnl_per_coin": "Median PnL / coin",
            "total_pnl_all_coins": "Total net PnL (all coins)", "gross_pnl": "Gross PnL", "fees": "Fees paid",
            "profitable_coins": "Profitable coins"}), width="stretch", hide_index=True)
        best = agg.sort_values("avg_pnl_per_coin", ascending=False).iloc[0]
        st.caption(f"Best average PnL per coin: {best['timeframe']} @ {best['rr']} -> {fmt_money(best['avg_pnl_per_coin'])} "
                   f"(win rate {best['win_rate']}%, {best['trades']} trades). Fees column shows how much cost eats into the edge.")

        st.subheader("Per coin")
        c1, c2 = st.columns(2)
        tf_pick = c1.selectbox("Timeframe", sorted({r["timeframe"] for r in rows}, key=E.TFS.index))
        rr_pick = c2.selectbox("RR", sorted({r["rr"] for r in rows}), format_func=lambda x: f"{x}R")
        coin_df = pd.DataFrame([r for r in rows if r["timeframe"] == tf_pick and r["rr"] == rr_pick])
        coin_df = coin_df.sort_values("net_pnl", ascending=False)[
            ["coin", "trades", "wins", "win_rate", "gross_pnl", "fees", "net_pnl", "ret_pct", "profit_factor", "max_dd", "bars"]]
        st.dataframe(coin_df, width="stretch", hide_index=True)
        st.download_button("Download all results (CSV)", pd.DataFrame(rows).to_csv(index=False),
                           file_name="ifvg_backtest_results.csv", mime="text/csv")
        if st.session_state.get("bt_fails"):
            with st.expander(f"{len(st.session_state['bt_fails'])} symbols/timeframes failed"):
                st.write("\n".join(st.session_state["bt_fails"][:200]))
    else:
        st.info("Press **Run backtest**. 100 coins x 6 timeframes takes a few minutes.")


# ───────────────────────── LIVE SCANNER ─────────────────────────
with tab_live:
    c1, c2, c3 = st.columns(3)
    l_coins = c1.slider("Top coins by volume ", 5, 100, 100, 5, key="lc")
    l_tfs = c2.multiselect("Timeframes ", E.TFS, default=["5m", "15m", "1h", "4h"], key="lt")
    lookback = c3.slider("Signal in last N closed candles", 1, 10, 2)
    l_rr = st.selectbox("RR for TP level", [1, 2, 3, 4, 5], index=2, format_func=lambda x: f"{x}R")

    if st.button("Scan now", type="primary"):
        try:
            with st.spinner("Loading top volume pairs..."):
                symbols = E.get_top_symbols(l_coins)
        except E.DataError as e:
            st.error(f"Could not load pairs. {e}")
            st.stop()
        tasks = [(s, tf) for s in symbols for tf in l_tfs]
        found, errs = [], 0
        prog = st.progress(0.0)

        def scan(task):
            sym, tf = task
            try:
                df = E.fetch_klines(sym, tf, 400)
                if df is None or len(df) < 100:
                    return []
                out = []
                for sig, a, t in E.latest_signals(df, params, lookback):
                    bar, d, top, bot, cl = sig
                    entry = E.entry_price(sig, params)
                    risk = a * params["sl_mult"]
                    out.append(dict(
                        coin=sym[:-4], tf=tf, side="BUY (IFVG+)" if d == 1 else "SELL (IFVG-)",
                        candles_ago=len(df) - 1 - bar, candle_time=t, entry=entry,
                        sl=entry - d * risk, tp=entry + d * risk * l_rr, close=df["c"].iloc[-1]))
                return out
            except Exception:
                return None

        done = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            for out in pool.map(scan, tasks):
                done += 1
                prog.progress(done / len(tasks))
                if out is None:
                    errs += 1
                else:
                    found.extend(out)
        prog.empty()
        st.session_state["live"] = (found, errs, len(tasks))

    live = st.session_state.get("live")
    if live:
        found, errs, total = live
        st.caption(f"Scanned {total - errs}/{total} coin-timeframes. {len(found)} signals.")
        if found:
            ldf = pd.DataFrame(found).sort_values(["candles_ago", "tf"])
            st.dataframe(ldf, width="stretch", hide_index=True)
        else:
            st.info("No fresh IFVG signals right now. Increase the lookback or add more timeframes.")
    else:
        st.info("Press **Scan now** to look for fresh IFVG signals.")
