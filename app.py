"""
IFVG Strategy Scanner + Backtest  (Streamlit)
Rules: "Learn & Trade With Naresh" - Inversion Fair Value Gap 4-step checklist
  1. Trend            -> trade only with the trend (EMA fast vs slow)
  2. Liquidity sweep  -> swing high/low swept (wick sweep, close back) near the FVG
  3. Confirmation gap -> after the FVG inverts, a NEW FVG forms inside/next to the IFVG
  4. Rejection        -> first retest of the IFVG must show a wick rejection (no full-body close inside)
Extras: speed of breakout, first (manipulation) gap after the sweep, only the FIRST retest is traded.

Data: Binance Vision public market-data API (data-api.binance.vision), Spot USDT pairs.
Silver: Binance spot has no silver pair, so Yahoo Finance SI=F (silver futures) is used.
"""
import os
import time
import threading
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

st.set_page_config(page_title="IFVG Strategy Scanner", page_icon="🎯", layout="wide")

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
ALL_TFS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"]
DEFAULT_TFS = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]
TF_ORDER = {t: i for i, t in enumerate(ALL_TFS)}
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "EUR", "EURI", "AEUR",
           "GBP", "TRY", "BRL", "ARS", "UST", "USTC", "PAXG", "XAUT", "WBTC", "WBETH", "BFUSD", "RLUSD"}
FALLBACK_TOP = ["BTC", "ETH", "XRP", "BNB", "SOL", "DOGE", "ADA", "TRX", "LINK", "AVAX",
                "XLM", "SUI", "BCH", "HBAR", "LTC", "DOT", "UNI", "AAVE", "NEAR", "APT",
                "ETC", "ICP", "ATOM", "FIL", "ARB", "OP", "INJ", "TAO", "SHIB", "PEPE",
                "TON", "ENA", "WLD", "SEI", "RENDER"]
SILVER = "SILVER(SI=F)"

DEFAULT_P = dict(
    ema_fast=50, ema_slow=200,
    use_trend=True, use_sweep=True, use_confirm=True,
    first_gap_only=False, use_speed=False,
    pivot_k=5, sweep_lookback=100, sweep_pre=5, sweep_reclaim=True,
    max_fvg_age=60, min_gap_atr=0.1,
    speed_bars=10, break_body=0.5,
    conf_window=8, conf_dist=1.0,
    retest_window=30, min_wick=0.30,
    sl_buf=0.10, max_risk_atr=4.0, max_hold=100,
    rrs=[1.0, 1.5, 2.0, 3.0], fee=0.001,
)

_lock_yf = threading.Lock()


@st.cache_resource
def _store():
    return {}


STORE = _store()

# ----------------------------------------------------------------------------
# Data layer
# ----------------------------------------------------------------------------

def _get(path, params):
    last = None
    for base in BASES:
        try:
            r = requests.get(base + path, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            last = f"{base} -> HTTP {r.status_code}"
        except Exception as e:  # noqa
            last = f"{base} -> {e}"
    raise RuntimeError(last)


def fetch_binance(symbol, tf, n_candles):
    out, end = [], None
    while len(out) < n_candles:
        lim = min(1000, n_candles - len(out))
        p = {"symbol": symbol, "interval": tf, "limit": lim}
        if end:
            p["endTime"] = end
        data = _get("/api/v3/klines", p)
        if not data:
            break
        out = data + out
        end = data[0][0] - 1
        if len(data) < lim:
            break
    if not out:
        raise RuntimeError("no data")
    df = pd.DataFrame(out).iloc[:, :7]
    df.columns = ["t", "open", "high", "low", "close", "volume", "ct"]
    df = df.drop_duplicates("t").sort_values("t")
    now_ms = int(time.time() * 1000)
    df = df[df["ct"] <= now_ms]  # closed candles only
    df.index = pd.to_datetime(df["t"], unit="ms")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_silver(tf, n_candles):
    import yfinance as yf
    cfg = {"5m": ("5m", "59d", None), "15m": ("15m", "59d", None), "30m": ("30m", "59d", None),
           "1h": ("1h", "700d", None), "2h": ("1h", "700d", "2h"), "4h": ("1h", "700d", "4h"),
           "6h": ("1h", "700d", "6h"), "8h": ("1h", "700d", "8h"), "12h": ("1h", "700d", "12h"),
           "1d": ("1d", "10y", None), "3d": ("1d", "10y", "3D"), "1w": ("1wk", "10y", None)}
    if tf not in cfg:
        raise RuntimeError(f"silver: timeframe {tf} not available on Yahoo")
    interval, period, rs = cfg[tf]
    with _lock_yf:
        raw = yf.Ticker("SI=F").history(period=period, interval=interval, auto_adjust=False)
    if raw is None or raw.empty:
        raise RuntimeError("silver: no data")
    raw = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    idx = raw.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    raw.index = idx
    if rs:
        raw = raw.resample(rs).agg({"open": "first", "high": "max", "low": "min",
                                    "close": "last", "volume": "sum"}).dropna()
    if interval != "1d" and interval != "1wk":
        raw = raw.iloc[:-1]  # drop the forming candle
    return raw.tail(n_candles).astype(float)


def get_data(symbol, tf, n_candles):
    key = (symbol, tf, n_candles)
    hit = STORE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    if os.environ.get("IFVG_SYNTH"):
        df = synth(symbol, tf, n_candles)
    elif symbol == SILVER:
        df = fetch_silver(tf, n_candles)
    else:
        df = fetch_binance(symbol, tf, n_candles)
    STORE[key] = (time.time(), df)
    return df


def synth(symbol, tf, n):
    """Synthetic random-walk candles (only used for offline testing: IFVG_SYNTH=1)."""
    rng = np.random.default_rng(abs(hash((symbol, tf))) % (2 ** 32))
    vol = 0.004 * np.exp(0.5 * np.cumsum(rng.normal(0, 0.05, n)) / 5)
    drift = np.repeat(rng.normal(0, 0.0006, n // 150 + 1), 150)[:n]
    ret = drift + vol * rng.normal(0, 1, n)
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.0005, n))
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol)))
    idx = pd.date_range(end=pd.Timestamp.utcnow().tz_localize(None).floor("h"), periods=n, freq="1h")
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": 1.0}, index=idx)


@st.cache_data(ttl=3600, show_spinner=False)
def top_symbols(n, mode):
    if mode.startswith("Top by"):
        try:
            data = _get("/api/v3/ticker/24hr", {})
            rows = []
            for d in data:
                s = d["symbol"]
                if not s.endswith("USDT"):
                    continue
                base = s[:-4]
                if base in STABLES or any(base.endswith(x) for x in ("UP", "DOWN", "BULL", "BEAR")):
                    continue
                rows.append((float(d["quoteVolume"]), s))
            rows.sort(reverse=True)
            syms = [s for _, s in rows][:n]
            if syms:
                return syms
        except Exception:
            pass
    return [b + "USDT" for b in FALLBACK_TOP][:n]


# ----------------------------------------------------------------------------
# Indicators
# ----------------------------------------------------------------------------

def ema(a, n):
    return pd.Series(a).ewm(span=n, adjust=False).mean().values


def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().values


def find_fvgs(h, l, at, min_gap):
    bull, bear = [], []  # (index, bottom, top)
    for i in range(2, len(h)):
        if l[i] > h[i - 2]:
            if (l[i] - h[i - 2]) >= min_gap * at[i]:
                bull.append((i, h[i - 2], l[i]))
        elif h[i] < l[i - 2]:
            if (l[i - 2] - h[i]) >= min_gap * at[i]:
                bear.append((i, h[i], l[i - 2]))
    return bull, bear


def find_sweeps_low(l, c, P):
    """Bar s sweeps a confirmed swing low (wick below it) and closes back above it."""
    n, k = len(l), P["pivot_k"]
    out = np.zeros(n, bool)
    piv = []
    for s in range(n):
        p = s - k
        if p - k >= 0 and l[p] <= l[p - k:p + k + 1].min():
            piv.append((p, l[p]))
        if piv:
            keep = []
            for pp, pv in piv:
                if l[s] < pv:
                    if c[s] >= pv or not P["sweep_reclaim"]:
                        out[s] = True
                elif s - pp <= P["sweep_lookback"]:
                    keep.append((pp, pv))
            piv = keep
    return out


# ----------------------------------------------------------------------------
# Strategy engine (written for LONG; SHORT = same code on mirrored prices)
# ----------------------------------------------------------------------------

def scan_dir(o, h, l, c, at, ef, es, P):
    n = len(c)
    bull, bear = find_fvgs(h, l, at, P["min_gap_atr"])
    bear_idx = [x[0] for x in bear]
    bull_idx = [x[0] for x in bull]
    sw_pos = np.flatnonzero(find_sweeps_low(l, c, P)) if P["use_sweep"] else np.array([], int)
    warm = max(P["ema_slow"], 50)
    setups, live, watch, used = [], [], [], set()

    for f, bot, top in bear:                       # bearish FVG -> inverted upward = bullish IFVG
        b = None
        for j in range(f + 1, min(n, f + 1 + P["max_fvg_age"])):
            if c[j] > top:
                b = j
                break
        if b is None or b < warm:
            continue
        rng_b = h[b] - l[b]
        if P["use_speed"]:
            if b - f > P["speed_bars"]:
                continue
            if rng_b <= 0 or (c[b] - o[b]) / rng_b < P["break_body"]:
                continue
        if P["use_trend"] and not ef[b] > es[b]:                       # 1. trend
            continue
        s = None
        if P["use_sweep"]:                                              # 2. liquidity sweep
            m = sw_pos[(sw_pos >= f - P["sweep_pre"]) & (sw_pos <= b)]
            if len(m) == 0:
                continue
            s = int(m[0])
            if P["first_gap_only"]:
                lo_i = bisect_left(bear_idx, s - P["sweep_pre"])
                hi_i = bisect_left(bear_idx, f)
                if hi_i - lo_i > 0:
                    continue
        cidx = b
        if P["use_confirm"]:                                            # 3. confirmation FVG
            cidx = None
            for gi in range(bisect_left(bull_idx, b), len(bull)):
                g, cb, ct = bull[gi]
                if g > b + P["conf_window"]:
                    break
                d = P["conf_dist"] * at[g]
                if cb <= top + d and ct >= bot - d:
                    cidx = g
                    break
            if cidx is None:
                if b + P["conf_window"] >= n - 1:
                    watch.append(dict(status="IFVG formed - waiting for confirmation FVG",
                                      bot=bot, top=top, b=b))
                continue
        start = cidx + 1                                                # 4. rejection on 1st retest
        end = min(n, start + P["retest_window"])
        decided = False
        for r in range(start, end):
            if c[r] < bot:                    # zone failed
                decided = True
                break
            if l[r] <= top:                   # first touch = only chance
                decided = True
                rng = h[r] - l[r]
                wick = (min(o[r], c[r]) - l[r]) / rng if rng > 0 else 0.0
                if c[r] > top and wick >= P["min_wick"] and r not in used:
                    used.add(r)
                    sl = min(bot, l[r]) - P["sl_buf"] * at[r]
                    e = r + 1
                    if e >= n:
                        risk = c[r] - sl
                        if 0 < risk <= P["max_risk_atr"] * at[r]:
                            live.append(dict(bot=bot, top=top, entry=c[r], sl=sl, r=r, b=b))
                    else:
                        entry = o[e]
                        risk = entry - sl
                        if 0 < risk <= P["max_risk_atr"] * at[r]:
                            setups.append(dict(f=f, b=b, c=cidx, r=r, e=e, bot=bot, top=top,
                                               entry=entry, sl=sl, risk=risk))
                break
        if not decided and end == n and start + P["retest_window"] >= n and start <= n:
            watch.append(dict(status="IFVG confirmed - waiting for retest/rejection",
                              bot=bot, top=top, b=b))
    return setups, live, watch


def simulate(h, l, c, e, entry, sl, rr, max_hold):
    risk = entry - sl
    tp = entry + rr * risk
    last = min(len(c) - 1, e + max_hold)
    for j in range(e, last + 1):
        if l[j] <= sl:                    # SL first if both hit in the same bar (conservative)
            return j, sl, -1.0, "LOSS"
        if h[j] >= tp:
            return j, tp, rr, "WIN"
    if last - e < max_hold:               # data ended, trade still running
        return last, c[last], (c[last] - entry) / risk, "OPEN"
    return last, c[last], (c[last] - entry) / risk, "TIMEOUT"


def scan(df, P):
    t = df.index
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    n = len(c)
    cols = ["signal_time", "entry_time", "side", "rr", "entry", "sl", "tp", "exit_time", "exit",
            "outcome", "r", "ifvg_time", "zone_bot", "zone_top", "bars_held"]
    if n < P["ema_slow"] + 60:
        return pd.DataFrame(columns=cols), [], []
    at = atr(h, l, c)
    ef, es = ema(c, P["ema_fast"]), ema(c, P["ema_slow"])
    rows, live_all, watch_all = [], [], []
    for side, sg in (("LONG", 1), ("SHORT", -1)):
        if sg == 1:
            o2, h2, l2, c2, ef2, es2 = o, h, l, c, ef, es
        else:
            o2, h2, l2, c2, ef2, es2 = -o, -l, -h, -c, -ef, -es
        setups, live, watch = scan_dir(o2, h2, l2, c2, at, ef2, es2, P)
        for s in setups:
            for rr in P["rrs"]:
                j, px, r, out = simulate(h2, l2, c2, s["e"], s["entry"], s["sl"], rr, P["max_hold"])
                cost = 2 * P["fee"] * abs(s["entry"]) / s["risk"]
                zb, zt = sorted((sg * s["bot"], sg * s["top"]))
                rows.append(dict(signal_time=t[s["r"]], entry_time=t[s["e"]], side=side, rr=rr,
                                 entry=sg * s["entry"], sl=sg * s["sl"],
                                 tp=sg * (s["entry"] + rr * s["risk"]), exit_time=t[j],
                                 exit=sg * px, outcome=out, r=r - cost, ifvg_time=t[s["b"]],
                                 zone_bot=zb, zone_top=zt, bars_held=j - s["e"]))
        for lv in live:
            risk = lv["entry"] - lv["sl"]
            zb, zt = sorted((sg * lv["bot"], sg * lv["top"]))
            live_all.append(dict(time=t[lv["r"]], side=side, entry=sg * lv["entry"], sl=sg * lv["sl"],
                                 tp_1R=sg * (lv["entry"] + risk), tp_2R=sg * (lv["entry"] + 2 * risk),
                                 tp_3R=sg * (lv["entry"] + 3 * risk), zone_bot=zb, zone_top=zt))
        for w in watch:
            zb, zt = sorted((sg * w["bot"], sg * w["top"]))
            watch_all.append(dict(side=side, status=w["status"], zone_bot=zb, zone_top=zt,
                                  ifvg_time=t[w["b"]], last_close=c[-1]))
    tr = pd.DataFrame(rows, columns=cols)
    return tr, live_all, watch_all


# ----------------------------------------------------------------------------
# Backtest runner + statistics
# ----------------------------------------------------------------------------

def stats(g):
    n = len(g)
    if n == 0:
        return {"trades": 0, "win%": np.nan, "avgR": np.nan, "PF": np.nan, "totalR": 0.0, "maxDD_R": 0.0}
    r = g.sort_values("entry_time")["r"]
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    eq = r.cumsum()
    return {"trades": n, "win%": 100 * (r > 0).mean(), "avgR": r.mean(),
            "PF": (gp / gl) if gl > 0 else np.inf, "totalR": r.sum(),
            "maxDD_R": (eq.cummax() - eq).max()}


def summarize(df, keys):
    rows = []
    for k, g in df.groupby(keys):
        k = k if isinstance(k, tuple) else (k,)
        row = dict(zip(keys, k))
        row.update(stats(g))
        oo = stats(g[g["oos"]])
        row.update({"OOS_trades": oo["trades"], "OOS_win%": oo["win%"], "OOS_avgR": oo["avgR"]})
        rows.append(row)
    out = pd.DataFrame(rows)
    if not out.empty and "tf" in out:
        out["_o"] = out["tf"].map(TF_ORDER)
        out = out.sort_values(["_o"] + [k for k in keys if k != "tf"]).drop(columns="_o")
    return out


def run_backtest(symbols, tfs, n_candles, P, progress):
    jobs = [(s, tf) for s in symbols for tf in tfs]
    data, errors = {}, []
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(get_data, s, tf, n_candles): (s, tf) for s, tf in jobs}
        for fut in as_completed(futs):
            s, tf = futs[fut]
            try:
                data[(s, tf)] = fut.result()
            except Exception as e:  # noqa
                errors.append(f"{s} {tf}: {e}")
            done += 1
            progress.progress(done / (2 * len(jobs)), text=f"Downloading {done}/{len(jobs)}")
    all_tr, live, watch, cover = [], [], [], []
    for i, ((s, tf), df) in enumerate(data.items()):
        tr, lv, wt = scan(df, P)
        split = df.index[0] + 0.6 * (df.index[-1] - df.index[0])
        tr["symbol"], tr["tf"] = s, tf
        tr["oos"] = tr["entry_time"] >= split
        all_tr.append(tr)
        for x in lv:
            live.append(dict(symbol=s, tf=tf, **x))
        for x in wt:
            watch.append(dict(symbol=s, tf=tf, **x))
        cover.append(dict(symbol=s, tf=tf, candles=len(df), start=df.index[0], end=df.index[-1]))
        progress.progress(0.5 + (i + 1) / (2 * max(1, len(data))), text=f"Scanning {i + 1}/{len(data)}")
    trades = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    return dict(trades=trades, live=pd.DataFrame(live), watch=pd.DataFrame(watch),
                cover=pd.DataFrame(cover), errors=errors, n_candles=n_candles, P=P)


def to_ist(df, cols):
    df = df.copy()
    for c in cols:
        if c in df:
            df[c] = (pd.to_datetime(df[c]).dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
                     .dt.strftime("%d-%b %H:%M"))
    return df


def show(df, **kw):
    st.dataframe(df, hide_index=True, **kw)


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.title("🎯 IFVG Strategy Scanner + Backtest")
st.caption("Learn & Trade With Naresh - 4-step IFVG checklist | Binance Vision Spot USDT | times in IST")

with st.expander("⚙️ Market & backtest settings", expanded=True):
    a, b = st.columns(2)
    universe = a.radio("Coin list", ["Top by 24h volume (live)", "Fixed market-cap list"])
    n_coins = a.slider("Number of coins", 5, 50, 30)
    add_gold = b.checkbox("XAUTUSDT (gold token)", True)
    add_silver = b.checkbox("Silver (Yahoo SI=F, no Binance pair)", True)
    tfs = st.multiselect("Timeframes", ALL_TFS, default=DEFAULT_TFS)
    n_candles = st.slider("Candles per timeframe", 1000, 10000, 3000, 500)
    rrs = st.multiselect("Reward:Risk targets (each is backtested)", [1.0, 1.5, 2.0, 3.0, 4.0],
                         default=[1.0, 1.5, 2.0, 3.0])
    fee = st.number_input("Fee + slippage per side (%)", 0.0, 0.5, 0.10, 0.01)

with st.expander("🧩 Strategy rules (4-step checklist)"):
    r1, r2 = st.columns(2)
    P = dict(DEFAULT_P)
    P["use_trend"] = r1.checkbox("1. Trend filter (EMA)", True)
    P["use_sweep"] = r1.checkbox("2. Liquidity sweep required", True)
    P["use_confirm"] = r1.checkbox("3. Confirmation FVG required", True)
    P["first_gap_only"] = r2.checkbox("Only first (manipulation) gap after sweep", False)
    P["use_speed"] = r2.checkbox("Speed of breakout filter (strong, quick break)", False)
    P["ema_fast"] = r1.number_input("EMA fast", 5, 200, 50)
    P["ema_slow"] = r2.number_input("EMA slow", 20, 500, 200)
    P["min_wick"] = r1.slider("Min rejection wick (% of candle range)", 10, 80, 30) / 100
    P["retest_window"] = r2.slider("Max bars to wait for retest", 5, 100, 30)
    P["conf_window"] = r1.slider("Max bars to wait for confirmation FVG", 2, 30, 8)
    P["max_hold"] = r2.slider("Max bars to hold a trade", 20, 300, 100)
    P["rrs"] = sorted(rrs) if rrs else [2.0]
    P["fee"] = fee / 100

if st.button("🚀 Run backtest + live scan", type="primary", width="stretch"):
    if not tfs:
        st.warning("Select at least one timeframe.")
    else:
        syms = top_symbols(n_coins, universe)
        if add_gold and "XAUTUSDT" not in syms:
            syms = syms + ["XAUTUSDT"]
        if add_silver:
            syms = syms + [SILVER]
        prog = st.progress(0.0, text="Starting...")
        st.session_state["res"] = run_backtest(syms, tfs, n_candles, P, prog)
        prog.empty()

if st.button("🧹 Clear data cache"):
    STORE.clear()
    st.success("Cache cleared - next run downloads fresh candles.")

res = st.session_state.get("res")
tab1, tab2, tab3, tab4 = st.tabs(["🏆 Best timeframes", "📡 Live signals", "📈 Chart", "📘 Rules & notes"])

with tab1:
    if not res:
        st.info("Run the backtest first (button above).")
    else:
        tr = res["trades"]
        if res["errors"]:
            with st.expander(f"⚠️ {len(res['errors'])} downloads failed"):
                st.write("\n".join(res["errors"][:60]))
        if tr.empty:
            st.warning("No trades found with these rules. Loosen a rule or add more candles.")
        else:
            closed = tr[tr["outcome"] != "OPEN"]
            st.subheader("Timeframe × RR (all coins pooled)")
            pool = summarize(closed, ["tf", "rr"])
            c1, c2 = st.columns(2)
            c1.caption("Win rate %")
            c1.dataframe(pool.pivot(index="tf", columns="rr", values="win%").reindex(
                [t for t in ALL_TFS if t in set(pool["tf"])]).round(1))
            c2.caption("Average R per trade (after fees)")
            c2.dataframe(pool.pivot(index="tf", columns="rr", values="avgR").reindex(
                [t for t in ALL_TFS if t in set(pool["tf"])]).round(3))
            with st.expander("Full pooled table"):
                show(pool.round(2))

            st.subheader("High-probability picks (symbol × timeframe × RR)")
            f1, f2, f3, f4 = st.columns(4)
            min_tr = f1.number_input("Min trades", 5, 200, 20)
            min_win = f2.number_input("Min win %", 0, 95, 55)
            min_pf = f3.number_input("Min profit factor", 0.0, 10.0, 1.3, 0.1)
            need_oos = f4.checkbox("OOS must be profitable", True)
            combo = summarize(closed, ["symbol", "tf", "rr"])
            sel = combo[(combo["trades"] >= min_tr) & (combo["win%"] >= min_win) & (combo["PF"] >= min_pf)]
            if need_oos:
                sel = sel[(sel["OOS_trades"] >= 5) & (sel["OOS_avgR"] > 0)]
            sort_by = st.selectbox("Sort by", ["avgR", "win%", "PF", "totalR", "OOS_avgR", "trades"])
            sel = sel.sort_values(sort_by, ascending=False)
            st.caption(f"{len(sel)} combos pass. OOS = last 40% of each data range (not used to judge in-sample).")
            show(sel.round(2).head(100))
            st.download_button("⬇️ All trades CSV", closed.to_csv(index=False).encode(), "ifvg_trades.csv")
            with st.expander("Data coverage (how far back each timeframe goes)"):
                show(to_ist(res["cover"], ["start", "end"]))

with tab2:
    if not res:
        st.info("Run the scan first.")
    else:
        st.subheader("🔔 Rejection candle just closed - entry on next candle open")
        if res["live"].empty:
            st.write("None right now.")
        else:
            show(to_ist(res["live"], ["time"]).round(6))
        st.subheader("👀 Watchlist - IFVG zones waiting")
        if res["watch"].empty:
            st.write("None right now.")
        else:
            show(to_ist(res["watch"], ["ifvg_time"]).round(6))
        st.caption("Live signals use the same 4-step rules. Check the pair's backtest numbers in the first tab "
                   "before acting - not every timeframe is worth trading.")

with tab3:
    if not res or res["trades"].empty:
        st.info("Run the backtest first.")
    else:
        tr = res["trades"]
        cc1, cc2, cc3 = st.columns(3)
        sym = cc1.selectbox("Symbol", sorted(tr["symbol"].unique()))
        tfc = cc2.selectbox("Timeframe", [t for t in ALL_TFS if t in set(tr[tr["symbol"] == sym]["tf"])])
        rr_sel = cc3.selectbox("RR", sorted(tr["rr"].unique()), index=min(2, len(tr["rr"].unique()) - 1))
        d = get_data(sym, tfc, res["n_candles"])
        t_ = tr[(tr["symbol"] == sym) & (tr["tf"] == tfc) & (tr["rr"] == rr_sel)]
        bars = st.slider("Bars to show", 100, min(1500, len(d)), min(400, len(d)))
        dd = d.tail(bars)
        fig = go.Figure(go.Candlestick(x=dd.index, open=dd["open"], high=dd["high"], low=dd["low"],
                                       close=dd["close"], name=sym))
        tt = t_[t_["entry_time"] >= dd.index[0]]
        for _, x in tt.iterrows():
            col = "rgba(0,160,90,0.18)" if x["side"] == "LONG" else "rgba(220,50,50,0.18)"
            fig.add_shape(type="rect", x0=x["ifvg_time"], x1=x["exit_time"], y0=x["zone_bot"],
                          y1=x["zone_top"], fillcolor=col, line_width=0)
        for side, sym_, colr in (("LONG", "triangle-up", "#00a05a"), ("SHORT", "triangle-down", "#dc3232")):
            z = tt[tt["side"] == side]
            fig.add_trace(go.Scatter(x=z["entry_time"], y=z["entry"], mode="markers", name=side,
                                     marker=dict(symbol=sym_, size=12, color=colr),
                                     text=z["outcome"] + "  R=" + z["r"].round(2).astype(str)))
        fig.update_layout(xaxis_rangeslider_visible=False, height=560, margin=dict(l=5, r=5, t=30, b=5))
        st.plotly_chart(fig, width="stretch")
        s_ = stats(t_[t_["outcome"] != "OPEN"])
        st.write(f"**{sym} {tfc} RR {rr_sel}:** {s_['trades']} trades, win {s_['win%']:.1f}%, "
                 f"avg R {s_['avgR']:.2f}, PF {s_['PF']:.2f}, total R {s_['totalR']:.1f}")
        show(to_ist(t_.drop(columns=["oos", "symbol", "tf"]), ["signal_time", "entry_time", "exit_time",
                                                               "ifvg_time"]).round(5).tail(50))

with tab4:
    st.markdown("""
**How each rule is coded**
1. **Trend** - long only if EMA50 > EMA200 at the break candle, short only if EMA50 < EMA200.
2. **Liquidity sweep** - a confirmed swing low (high for shorts) is wicked through and the candle closes back
   beyond it, within 5 bars before the FVG up to the break candle. Option: only the *first* gap after the sweep
   (manipulation gap).
3. **IFVG** - a normal FVG (≥ 0.1 ATR) is closed through. Bullish IFVG = bearish FVG closed above; bearish IFVG =
   bullish FVG closed below. Option: speed filter (break within 10 bars, strong body ≥ 50%).
4. **Confirmation gap** - a *new* FVG in the new direction must form within 8 bars, overlapping or within 1 ATR
   of the IFVG.
5. **Rejection at retest** - only the **first** touch of the IFVG counts. The candle must wick into the zone and
   close outside it with a wick ≥ 30% of its range. A body closing inside the zone, or a close beyond the zone,
   cancels the setup.
6. **Entry / exit** - entry at the next candle open. SL beyond the zone/wick low (+0.1 ATR). TP at the chosen RR.
   If SL and TP are both hit in one candle, SL is assumed first. Fees+slippage are subtracted in R.

**Honest notes**
- Backtests are not a promise. Prefer combos with many trades, win rate and profit factor that hold on the
  **OOS** (out-of-sample) columns, and that work on several coins at the same timeframe.
- Intraday history is limited by the candle count (e.g. 3000 × 5m ≈ 10 days). Raise it for lower timeframes.
- Silver comes from Yahoo futures data (SI=F): intraday history is short (≈2 months for ≤30m, ≈2 years for 1h+).
- The "POI / order block touch" alternative in rule 2 is not coded - only the liquidity sweep.
""")
