"""
Chart + formatting helpers for the IFVG live scanner.
Created & maintained by Kishore
"""
import pandas as pd
import plotly.graph_objects as go

import ifvg_engine as E

UP, DOWN, BLUE = "#26a69a", "#ef5350", "#4da3ff"
IST_SHIFT = pd.Timedelta(hours=5, minutes=30)   # Binance candle times are UTC, the chart shows IST


def fmt_price(x):
    """Price with sensible decimals for BTC (98,765.43) down to SHIB (0.00001234)."""
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"
    return f"{x:.8f}"


def to_ist(ts_utc):
    return pd.Timestamp(ts_utc) + IST_SHIFT


def build_signal_chart(df, r, bars=90):
    """Candles with the signal marked and ENTRY / SL / TP drawn as lines plus green (profit) and red (loss) zones.

    df = closed candles (t,o,h,l,c,v) used by the scan, r = one signal row of the live scanner
    (bar, dir, entry, sl, tp, atr, status, exit_bar, coin, tf, side).
    """
    n = len(df)
    tfm = pd.Timedelta(minutes=E.TF_MINUTES[r["tf"]])
    start = max(0, min(n - bars, r["bar"] - 20))
    view = df.iloc[start:]
    t_view = view["t"] + IST_SHIFT
    t_sig = to_ist(df["t"].iloc[r["bar"]])
    x_end = to_ist(df["t"].iloc[-1]) + tfm * 20          # empty strip on the right: the price labels live here
    finished = r.get("exit_bar") is not None and r["status"] in ("TP hit", "SL hit")
    zone_end = to_ist(df["t"].iloc[r["exit_bar"]]) if finished else to_ist(df["t"].iloc[-1]) + tfm
    d, entry, sl, tp = r["dir"], r["entry"], r["sl"], r["tp"]

    fig = go.Figure(go.Candlestick(
        x=t_view, open=view["o"], high=view["h"], low=view["l"], close=view["c"], name="",
        increasing_line_color=UP, increasing_fillcolor=UP,
        decreasing_line_color=DOWN, decreasing_fillcolor=DOWN))

    # profit zone (entry -> TP) and loss zone (entry -> SL)
    fig.add_shape(type="rect", x0=t_sig, x1=zone_end, y0=entry, y1=tp, layer="below", line_width=0,
                  fillcolor="rgba(38,166,154,0.15)")
    fig.add_shape(type="rect", x0=t_sig, x1=zone_end, y0=entry, y1=sl, layer="below", line_width=0,
                  fillcolor="rgba(239,83,80,0.15)")
    for name, y, color, dash in (("TP", tp, UP, "solid"), ("ENTRY", entry, BLUE, "dash"), ("SL", sl, DOWN, "solid")):
        fig.add_shape(type="line", x0=t_sig, x1=x_end, y0=y, y1=y, line=dict(color=color, width=1.6, dash=dash))
        fig.add_annotation(x=x_end, y=y, text=f"<b>{name} {fmt_price(y)}</b>", showarrow=False,
                           xanchor="right", yanchor="bottom" if y >= entry else "top",
                           font=dict(color=color, size=11))

    # signal marker
    off = r["atr"] * 0.6
    if d == 1:
        y_m, sym, col, pos = df["l"].iloc[r["bar"]] - off, "triangle-up", UP, "bottom center"
    else:
        y_m, sym, col, pos = df["h"].iloc[r["bar"]] + off, "triangle-down", DOWN, "top center"
    fig.add_trace(go.Scatter(x=[t_sig], y=[y_m], mode="markers+text", hoverinfo="skip",
                             marker=dict(symbol=sym, size=14, color=col),
                             text=["BUY" if d == 1 else "SELL"], textposition=pos,
                             textfont=dict(color=col, size=11)))
    # where the trade ended (if it already did)
    if finished:
        hit_tp = r["status"] == "TP hit"
        fig.add_trace(go.Scatter(x=[to_ist(df["t"].iloc[r["exit_bar"]])], y=[tp if hit_tp else sl],
                                 mode="markers+text", hoverinfo="skip",
                                 marker=dict(symbol="x", size=12, color=UP if hit_tp else DOWN),
                                 text=[r["status"]], textposition="top center",
                                 textfont=dict(color=UP if hit_tp else DOWN, size=11)))

    lo = min(view["l"].min(), sl, tp, entry, y_m)
    hi = max(view["h"].max(), sl, tp, entry, y_m)
    pad = (hi - lo) * 0.05
    fig.update_yaxes(range=[lo - pad, hi + pad], side="right")
    fig.update_xaxes(range=[t_view.iloc[0], x_end + tfm], rangeslider_visible=False)
    fig.update_layout(height=480, margin=dict(l=0, r=0, t=38, b=0), showlegend=False, dragmode="pan",
                      title=dict(text=f"{r['coin']}/USDT {r['tf']} - {r['side']} - {r['status']}  (time = IST)",
                                 font=dict(size=14)))
    return fig
