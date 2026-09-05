"""Order-flow analytics: volume dots, cumulative delta and the next-move read.

A candle tells you where price went. It does not tell you who moved it, whether
the move was paid for, or whether the other side quietly took the whole thing.
Delta — aggressive buying minus aggressive selling — does.

Every dot on the chart is one bar of the consolidated cross-exchange tape:

* where it sits   — the volume-weighted price of that bar
* how big it is   — total volume against the rest of the window
* what colour     — green when buyers were the aggressors, red when sellers
* how solid       — how lopsided that was

The interesting dots are the ones that disagree with their candle. A red dot on
a green bar means sellers hit every bid and price went up anyway: someone with
size absorbed them. That is the signature that leads reversals, and it is what
:func:`read_flow` is looking for.
"""

from __future__ import annotations

import math
from typing import Any

#: dots are graded against the window, not an absolute size
SIZE_STEPS = (0.35, 0.6, 0.8, 0.93)
#: a bar whose body is this small a share of its range went nowhere
STALL_BODY = 0.34
#: |delta| share of volume above which flow counts as one-sided
ONE_SIDED = 0.28


def _slope(values: list[float]) -> float:
    """Least-squares slope, normalised so it can be compared across scales."""
    n = len(values)
    if n < 3:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if not den:
        return 0.0
    slope = num / den
    scale = max(abs(mean_y), 1e-9)
    return slope / scale


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[idx]


def build_dots(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn tape bars into plottable dots plus a cumulative delta line."""
    if not bars:
        return {"dots": [], "cvd": [], "bars": [], "levels": [], "poc": None}

    volumes = [b["volume"] for b in bars if b["volume"] > 0]
    ranges = [b["high"] - b["low"] for b in bars if b["high"] > b["low"]]
    avg_range = sum(ranges) / len(ranges) if ranges else 0.0

    cvd = 0.0
    cvd_series: list[dict[str, Any]] = []
    dots: list[dict[str, Any]] = []
    levels: dict[float, float] = {}

    for i, bar in enumerate(bars):
        vol = bar["volume"]
        delta = bar["delta"]
        cvd += delta
        cvd_series.append({"ts": bar["ts"], "value": round(cvd, 6)})

        for lv in bar.get("levels") or []:
            levels[lv["price"]] = levels.get(lv["price"], 0.0) + lv["buy"] + lv["sell"]

        if vol <= 0:
            continue

        share = sum(1 for v in volumes if v <= vol) / len(volumes) if volumes else 0.0
        size = 1 + sum(1 for step in SIZE_STEPS if share >= step)
        dpct = bar["delta_pct"]
        body = bar["close"] - bar["open"]
        span = bar["high"] - bar["low"]
        body_share = abs(body) / span if span else 0.0

        kind, note = _classify(bar, dpct, body, body_share, span, avg_range)
        dots.append(
            {
                "ts": bar["ts"],
                "price": bar["vwap"] or bar["close"],
                "size": size,
                "volume": vol,
                "delta": delta,
                "delta_pct": round(dpct, 4),
                "buy_vol": bar["buy_vol"],
                "sell_vol": bar["sell_vol"],
                "trades": bar["trades"],
                "kind": kind,
                "note": note,
                "estimated": bar.get("estimated", False),
                "venues": bar.get("venues") or {},
            }
        )

    poc = max(levels.items(), key=lambda kv: kv[1])[0] if levels else None
    return {
        "dots": dots,
        "cvd": cvd_series,
        "levels": sorted(({"price": p, "volume": v} for p, v in levels.items()), key=lambda r: -r["volume"])[:40],
        "poc": poc,
        "cvd_total": round(cvd, 6),
    }


def _classify(bar: dict, dpct: float, body: float, body_share: float, span: float, avg_range: float) -> tuple[str, str]:
    """Name what a bar's flow actually did."""
    one_sided = abs(dpct) >= ONE_SIDED
    big_bar = span > avg_range * 1.1 if avg_range else False

    if one_sided and body_share <= STALL_BODY:
        side = "buying" if dpct > 0 else "selling"
        other = "sellers" if dpct > 0 else "buyers"
        return "absorption", f"heavy {side} went nowhere — {other} absorbed it"
    if one_sided and body and (dpct > 0) != (body > 0):
        side = "buyers" if dpct > 0 else "sellers"
        return "divergence", f"{side} were the aggressors but price went the other way"
    if one_sided and body and (dpct > 0) == (body > 0) and big_bar:
        side = "buyers" if dpct > 0 else "sellers"
        return "initiative", f"{side} paid up and price followed"
    if one_sided:
        return "pressure", ("buy pressure" if dpct > 0 else "sell pressure")
    if body_share >= 0.75 and abs(dpct) < 0.1:
        return "thin", "price moved on almost no net flow — easy to reverse"
    return "balanced", "two-way trade"


def venue_split(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who was buying and who was selling, per exchange, over the window."""
    agg: dict[str, list[float]] = {}
    for bar in bars:
        for venue, side in (bar.get("venues") or {}).items():
            slot = agg.setdefault(venue, [0.0, 0.0])
            slot[0] += side.get("buy", 0.0)
            slot[1] += side.get("sell", 0.0)
    rows = []
    total = sum(b + s for b, s in agg.values()) or 1.0
    for venue, (buy, sell) in agg.items():
        vol = buy + sell
        rows.append(
            {
                "venue": venue,
                "buy": round(buy, 6),
                "sell": round(sell, 6),
                "volume": round(vol, 6),
                "delta": round(buy - sell, 6),
                "delta_pct": round((buy - sell) / vol, 4) if vol else 0.0,
                "share": round(vol / total, 4),
            }
        )
    rows.sort(key=lambda r: -r["volume"])
    return rows


def read_flow(
    bars: list[dict[str, Any]],
    dots: list[dict[str, Any]],
    cvd: list[dict[str, Any]],
    venues: list[dict[str, Any]],
    prints: list[dict[str, Any]] | None = None,
    lookback: int = 24,
) -> dict[str, Any]:
    """Score the order flow and say what it implies about the next move."""
    if len(bars) < 6:
        return {
            "ok": False,
            "direction": "flat",
            "score": 0,
            "confidence": 0,
            "headline": "not enough tape yet",
            "reasons": [],
        }

    recent = bars[-lookback:]
    closes = [b["close"] for b in recent]
    cvals = [c["value"] for c in cvd[-lookback:]]
    price_slope = _slope(closes)
    cvd_slope = _slope(cvals) if len(cvals) >= 3 else 0.0

    reasons: list[dict[str, Any]] = []
    score = 0.0

    def add(weight: float, text: str, detail: str = "") -> None:
        nonlocal score
        score += weight
        reasons.append({"weight": round(weight, 1), "text": text, "detail": detail, "bull": weight > 0})

    # 1. delta trend --------------------------------------------------------
    vols = [b["volume"] for b in recent]
    deltas = [b["delta"] for b in recent]
    net = sum(deltas)
    gross = sum(vols) or 1.0
    tilt = net / gross
    if abs(tilt) > 0.04:
        add(
            max(-26.0, min(26.0, tilt * 130)),
            f"net {'buying' if tilt > 0 else 'selling'} over the last {len(recent)} bars",
            f"{tilt * 100:+.1f}% of all volume traded was aggressive {'buying' if tilt > 0 else 'selling'}",
        )

    # 2. divergence: the single most useful thing in this whole panel -------
    if abs(price_slope) > 1e-5 and abs(cvd_slope) > 1e-5 and (price_slope > 0) != (cvd_slope > 0):
        bull = cvd_slope > 0
        add(
            22.0 if bull else -22.0,
            "price and cumulative delta disagree",
            (
                "price is grinding lower while buyers keep lifting offers — sellers are running out"
                if bull
                else "price is grinding higher while sellers keep hitting bids — the rally is not being paid for"
            ),
        )
    elif abs(price_slope) > 1e-5 and (price_slope > 0) == (cvd_slope > 0) and abs(cvd_slope) > 1e-5:
        add(
            9.0 if price_slope > 0 else -9.0,
            "flow confirms the trend",
            "cumulative delta is moving with price, so the move is being paid for",
        )

    # 3. absorption clusters -------------------------------------------------
    tail = dots[-lookback:]
    absorbed_buy = [d for d in tail if d["kind"] == "absorption" and d["delta"] > 0]
    absorbed_sell = [d for d in tail if d["kind"] == "absorption" and d["delta"] < 0]
    if len(absorbed_buy) >= 2:
        add(-14.0, f"{len(absorbed_buy)} bars of buying absorbed", "someone is selling into every bid lift")
    if len(absorbed_sell) >= 2:
        add(14.0, f"{len(absorbed_sell)} bars of selling absorbed", "someone is buying everything offered")

    # 4. initiative ----------------------------------------------------------
    init = [d for d in tail if d["kind"] == "initiative"]
    if init:
        bull = sum(1 for d in init if d["delta"] > 0)
        bear = len(init) - bull
        if bull != bear:
            add(
                8.0 if bull > bear else -8.0,
                f"{max(bull, bear)} initiative {'buy' if bull > bear else 'sell'} bars",
                "aggressors paid the spread and price followed",
            )

    # 5. cross-exchange agreement -------------------------------------------
    voting = [v for v in venues if v["volume"] > 0]
    if len(voting) >= 2:
        up = sum(1 for v in voting if v["delta"] > 0)
        down = len(voting) - up
        lead = max(voting, key=lambda v: v["volume"])
        if up and not down:
            add(12.0, f"all {len(voting)} exchanges show net buying", "no venue is fading the move")
        elif down and not up:
            add(-12.0, f"all {len(voting)} exchanges show net selling", "no venue is fading the move")
        else:
            add(
                6.0 if lead["delta"] > 0 else -6.0,
                f"exchanges disagree — {lead['venue']} leads",
                f"{up} buying vs {down} selling; the biggest book ({lead['venue']}, "
                f"{lead['share'] * 100:.0f}% of volume) is net "
                f"{'long' if lead['delta'] > 0 else 'short'}",
            )

    # 6. size prints ---------------------------------------------------------
    prints = prints or []
    if prints:
        buy_n = sum(p["notional"] for p in prints if p["side"] == "buy")
        sell_n = sum(p["notional"] for p in prints if p["side"] == "sell")
        tot = buy_n + sell_n
        if tot > 0:
            bias = (buy_n - sell_n) / tot
            if abs(bias) > 0.2:
                add(
                    max(-11.0, min(11.0, bias * 22)),
                    f"large prints lean {'buy' if bias > 0 else 'sell'}",
                    f"{len(prints)} outsized trades, {abs(bias) * 100:.0f}% skewed",
                )

    # 7. where price sits in the volume profile ------------------------------
    poc_levels = [lv for b in recent for lv in (b.get("levels") or [])]
    if poc_levels:
        totals: dict[float, float] = {}
        for lv in poc_levels:
            totals[lv["price"]] = totals.get(lv["price"], 0.0) + lv["buy"] + lv["sell"]
        poc = max(totals.items(), key=lambda kv: kv[1])[0]
        last = closes[-1]
        if poc and abs(last - poc) / poc > 0.0008:
            above = last > poc
            add(
                5.0 if above else -5.0,
                f"price is {'above' if above else 'below'} the volume point of control",
                f"most of the window's volume traded at {poc:g}; acceptance {'above' if above else 'below'} it "
                f"is {'constructive' if above else 'heavy'}",
            )

    # 8. exhaustion ----------------------------------------------------------
    if len(recent) >= 4:
        last_bar = recent[-1]
        prior = recent[-4:-1]
        prior_max = max((abs(b["delta"]) for b in prior), default=0.0)
        if prior_max and abs(last_bar["delta"]) > prior_max * 2 and abs(last_bar["delta_pct"]) > 0.4:
            body = abs(last_bar["close"] - last_bar["open"])
            span = last_bar["high"] - last_bar["low"]
            if span and body / span < 0.4:
                add(
                    -10.0 if last_bar["delta"] > 0 else 10.0,
                    "climax bar with no follow-through",
                    "the biggest delta of the window produced almost no candle — that is usually the last of it",
                )

    score = max(-100.0, min(100.0, score))
    direction = "up" if score >= 12 else "down" if score <= -12 else "flat"
    confidence = min(95, int(abs(score) * 0.85 + (10 if len(reasons) >= 4 else 0)))
    estimated = any(b.get("estimated") for b in recent)

    headline = {
        "up": "buyers are in control",
        "down": "sellers are in control",
        "flat": "two-sided — no edge in the flow right now",
    }[direction]
    if direction != "flat":
        strongest = max(reasons, key=lambda r: abs(r["weight"]))
        headline = f"{headline} — {strongest['text']}"

    reasons.sort(key=lambda r: -abs(r["weight"]))
    return {
        "ok": True,
        "direction": direction,
        "score": round(score, 1),
        "confidence": confidence,
        "headline": headline,
        "reasons": reasons,
        "price_slope": round(price_slope * 1000, 4),
        "cvd_slope": round(cvd_slope * 1000, 4),
        "net_delta": round(sum(deltas), 6),
        "delta_tilt": round(tilt, 4),
        "bars_read": len(recent),
        "estimated": estimated,
    }


def summarise(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline numbers for the KPI strip."""
    if not bars:
        return {}
    vol = sum(b["volume"] for b in bars)
    buy = sum(b["buy_vol"] for b in bars)
    sell = sum(b["sell_vol"] for b in bars)
    last = bars[-1]
    upbars = sum(1 for b in bars if b["delta"] > 0)
    return {
        "bars": len(bars),
        "volume": round(vol, 6),
        "buy_vol": round(buy, 6),
        "sell_vol": round(sell, 6),
        "delta": round(buy - sell, 6),
        "delta_pct": round((buy - sell) / vol, 4) if vol else 0.0,
        "buy_bars": upbars,
        "sell_bars": len(bars) - upbars,
        "trades": sum(b["trades"] for b in bars),
        "last_delta": round(last["delta"], 6),
        "last_delta_pct": round(last["delta_pct"], 4),
        "notional": round(sum(b["volume"] * b["vwap"] for b in bars), 2),
        "estimated": all(b.get("estimated") for b in bars),
    }


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x if not math.isnan(x) else 0.0))
