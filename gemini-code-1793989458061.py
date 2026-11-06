"""
IFVG Sniper Entry Engine - Backtest + Scanner (Top 100 Binance USDT coins)
Faithful Python port of the Pine v6 logic (trade_w_samet).
Run:  pip install streamlit pandas numpy requests
      streamlit run ifvg_backtest_scanner.py
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="IFVG Sniper Backtest", layout="wide")

TFS = ["5m", "15m", "30m", "1h", "4h"]
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "EUR", "EURI", "AEUR", "XUSD", "USD1", "PAXG"}
BAD_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")

FILTERS = {  # gapAtr, bodyRatio, rangeAtr, breakBufferAtr
    "Off": (0.0, 0.0, 0.0, 0.0),
    "Loose": (0.15, 0.40, 0.40, 0.00),
    "Balanced": (0.25, 0.50, 0.60, 0.05),
    "Strict": (0.40, 0.60, 0.85, 0.10),
}


# ───────────────────────── DATA ─────────────────────────
def get_top_symbols(base, n):
    r = requests.get(f"{base}/api/v3/ticker/24hr", timeout=20)
    r.raise_for_status()
    rows = []
    for t in r.json():
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        b = s[:-4]
        if b in STABLES or b.endswith(BAD_SUFFIX):
            continue
        rows.append((s, float(t["quoteVolume"])))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def fetch_klines(base, symbol, interval, total):
    out, end = [], None
    while len(out) < total:
        p = {"symbol": symbol, "interval": interval, "limit": min(1000, total - len(out))}
        if end:
            p["endTime"] = end
        for attempt in range(3):
            try:
                r = requests.get(f"{base}/api/v3/klines", params=p, timeout=20)
                r.raise_for_status()
                break
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        else:
            break
        data = r.json()
        if not data:
            break
        out = data + out
        end = data[0][0] - 1
        if len(data) < p["limit"]:
            break
    if not out:
        return None
    df = pd.DataFrame(out).iloc[:, :6]
    df.columns = ["t", "o", "h", "l", "c", "v"]
    df = df.astype(float)
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    return df.iloc[:-1].reset_index(drop=True)  # drop the live (unconfirmed) candle


# ───────────────────────── ENGINE ─────────────────────────
def atr_rma(h, l, c, n):
    pc = np.roll(c, 1)
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    tr[0] = h[0] - l[0]
    out = np.full(len(c), np.nan)
    if len(c) < n:
        return out
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def run_ifvg(df, p):
    o, h, l, c = (df[k].values for k in ("o", "h", "l", "c"))
    n = len(df)
    atr = atr_rma(h, l, c, p["atr_len"])
    min_gap, min_body, min_rng, brk = (
        FILTERS[p["filter"]] if p["filter"] != "Custom" else p["custom"]
    )
    raw = []  # dict: top, bot, dir, age, gap, body, rng
    trade = None
    trades, signals = [], []
    blocked = filtered = 0

    for i in range(n):
        a = atr[i]
        safe = a if (not math.isnan(a) and a > 0) else 1e-12

        # age + expiry
        for r in raw:
            r["age"] += 1
        raw = [r for r in raw if r["age"] <= p["max_age"]]

        rng = max(h[i] - l[i], 1e-12)
        body_ratio = abs(c[i] - o[i]) / rng
        rng_atr = rng / safe

        if i >= 2:
            if l[i] > h[i - 2]:
                raw.append(dict(top=l[i], bot=h[i - 2], dir=1, age=0,
                                gap=(l[i] - h[i - 2]) / safe, body=body_ratio, rng=rng_atr))
            if h[i] < l[i - 2]:
                raw.append(dict(top=l[i - 2], bot=h[i], dir=-1, age=0,
                                gap=(l[i - 2] - h[i]) / safe, body=body_ratio, rng=rng_atr))
        while len(raw) > p["max_hidden"]:
            raw.pop(0)

        # inversion detection (newest -> oldest, first hit wins)
        new_dir = 0
        new_top = new_bot = None
        buf = safe * brk
        for idx in range(len(raw) - 1, -1, -1):
            r = raw[idx]
            bull = r["dir"] == -1 and c[i] > r["top"] + buf
            bear = r["dir"] == 1 and c[i] < r["bot"] - buf
            if bull or bear:
                ok = p["filter"] == "Off" or (
                    r["gap"] >= min_gap and r["body"] >= min_body and r["rng"] >= min_rng
                )
                if ok:
                    new_dir = 1 if bull else -1
                    new_top, new_bot = r["top"], r["bot"]
                else:
                    filtered += 1
                raw.pop(idx)
                break

        if new_dir != 0:
            signals.append((i, new_dir))
            if trade is None:
                # Broken Boundary line price (Pine default)
                if p["line_mode"] == "Confirmation Close":
                    line_price = c[i]
                elif p["line_mode"] == "Midpoint":
                    line_price = (new_top + new_bot) / 2
                else:
                    line_price = new_top if new_dir == 1 else new_bot
                entry = c[i] if p["entry_mode"] == "Confirmation Close" else line_price
                risk = a * p["sl_mult"] if not math.isnan(a) else None
                if risk and risk > 0:
                    sl = entry - risk if new_dir == 1 else entry + risk
                    tp = entry + risk * p["rr"] if new_dir == 1 else entry - risk * p["rr"]
                    trade = dict(bar=i, dir=new_dir, entry=entry, sl=sl, tp=tp, risk=risk)
            else:
                blocked += 1