#!/usr/bin/env python3
"""Backtest the opening-range breakout rule the paper trader uses, on the
stocks a $100 account can actually trade, over Yahoo's last 60 days of
5-minute bars. Standard library only; reuses trader-scanner's fetch code.

Rule (same as trader-entry / trader-exit):
  opening range = first 15 minutes (three 5-minute bars, 9:30-9:45)
  entry  = close of the first 5-minute bar that closes above the range high,
           if that bar closes at or before the entry cutoff
  stop   = range midpoint (or range low)
  target = entry + k x risk
  exit   = stop or target, whichever a later bar touches first (a bar that
           touches both counts as the stop: conservative), else the 15:55
           bar's close
Sizing = the $100 account: whole shares, min(75% cap, $90 cash, $5 max loss).

Usage: python3 tools/orb_backtest.py [--top 150] [--min-price 3] [--max-price 60]
Writes trader/backtest/results.json and prints the comparison table.
"""
import argparse
import datetime as dt
import gzip
import importlib.util
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "trader", "backtest")
CACHE = os.path.join(OUT, "cache")

spec = importlib.util.spec_from_file_location(
    "scan", os.path.join(ROOT, ".claude", "skills", "trader-scanner", "scan.py"))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

BUDGET, CAP_PCT, BUFFER_PCT, MAX_LOSS = 100.0, 75.0, 10.0, 5.0


SCREENS = ("most_actives", "day_gainers", "day_losers", "small_cap_gainers", "aggressive_small_caps",
           "growth_technology_stocks", "undervalued_growth_stocks", "most_shorted_stocks")


def pick_symbols(top, lo, hi, pause):
    """Yahoo's predefined lists, merged; ranked by 3-month average dollar volume.
    Biased toward what is active lately, which is also what the live scanner sees."""
    seen = {}
    for scr in SCREENS:
        for start in (0, 100, 200):
            try:
                d = json.loads(scan.yahoo_get("/v1/finance/screener/predefined/saved?scrIds=%s&count=100&start=%d"
                                              % (scr, start)))
                quotes = d["finance"]["result"][0]["quotes"]
            except Exception:
                break
            time.sleep(pause)
            for q in quotes:
                s, p, v = q.get("symbol"), q.get("regularMarketPrice"), q.get("averageDailyVolume3Month")
                if s and p and v and q.get("quoteType", "EQUITY") == "EQUITY" and lo <= p <= hi and v >= 1e6:
                    seen[s] = (p * v, s, p)
            if len(quotes) < 100:
                break
    rows = sorted(seen.values(), reverse=True)[:top]
    return [(s, p, dv) for dv, s, p in rows]


def bars_5m(sym, pause):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s.json.gz" % sym)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        with gzip.open(path, "rt") as fh:
            return json.load(fh)
    doc = json.loads(scan.yahoo_get(scan.chart_path(sym, "60d", "5m")))
    time.sleep(pause)
    res = doc["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        t = scan.ts_to_eastern(ts)
        out.append([t.strftime("%Y-%m-%d"), t.hour * 60 + t.minute, o, h, l, c])
    with gzip.open(path, "wt") as fh:
        json.dump(out, fh)
    return out


def sessions(bars, today):
    days = {}
    for d, m, o, h, l, c in bars:
        if 570 <= m < 960 and d != today:      # regular hours, completed sessions only
            days.setdefault(d, []).append((m, o, h, l, c))
    return {d: sorted(b) for d, b in days.items() if len(b) >= 70}


def trade(day, stop_rule, target_r, cutoff_min):
    rng = [b for b in day if b[0] < 585]
    if len(rng) < 3:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    stop = (hi + lo) / 2 if stop_rule == "mid" else lo
    stop = math.floor(stop * 100) / 100
    for i, (m, o, h, l, c) in enumerate(day):
        if m < 585:
            continue
        if m + 5 > cutoff_min:
            return None
        if c > hi:
            entry = c
            risk = entry - stop
            if risk < max(0.02, entry * 0.001):
                return None
            target = round(entry + target_r * risk, 2)
            qty = min(int(BUDGET * CAP_PCT / 100 // entry), int(BUDGET * (1 - BUFFER_PCT / 100) // entry),
                      int(MAX_LOSS // risk))
            if qty < 1:
                return None
            for (m2, o2, h2, l2, c2) in day[i + 1:]:
                if m2 >= 955:
                    return (entry, c2, qty, risk, "close")
                if l2 <= stop:
                    return (entry, min(stop, o2), qty, risk, "stop")   # gap below the stop fills at the open
                if h2 >= target:
                    return (entry, target, qty, risk, "target")
            return (entry, day[-1][4], qty, risk, "close")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=150)
    ap.add_argument("--min-price", type=float, default=3.0)
    ap.add_argument("--max-price", type=float, default=60.0)
    ap.add_argument("--pause", type=float, default=0.3)
    a = ap.parse_args()
    today = scan.to_eastern(dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)).strftime("%Y-%m-%d")
    print("picking the %d most traded stocks priced $%g-$%g..." % (a.top, a.min_price, a.max_price), flush=True)
    picks = pick_symbols(a.top, a.min_price, a.max_price, a.pause)
    print("got %d. Fetching 60 days of 5-minute bars..." % len(picks), flush=True)
    data = {}
    for k, (s, p, dv) in enumerate(picks):
        try:
            data[s] = sessions(bars_5m(s, a.pause), today)
        except Exception as e:  # one bad symbol must not stop the run
            print("  %s: %s" % (s, e), flush=True)
        if (k + 1) % 25 == 0:
            print("  %d/%d" % (k + 1, len(picks)), flush=True)
    variants = []
    for stop_rule in ("mid", "low"):
        for target_r in (1.0, 1.5, 2.0, 3.0):
            for cutoff in ("10:30", "11:30", "15:30"):
                hh, mm = map(int, cutoff.split(":"))
                trades = []
                for s, days in data.items():
                    for d, day in days.items():
                        t = trade(day, stop_rule, target_r, hh * 60 + mm)
                        if t:
                            e, x, q, r, why = t
                            trades.append({"sym": s, "date": d, "R": (x - e) / r, "usd": (x - e) * q, "why": why})
                n = len(trades)
                if not n:
                    continue
                wins = sum(1 for t in trades if t["usd"] > 0)
                avg_r = sum(t["R"] for t in trades) / n
                usd = sum(t["usd"] for t in trades)
                # one trade a day, like the paper trader: take that day's first signal by time is not
                # recoverable here, so also report a per-day-capped estimate: mean $ per trade x days
                ndays = len({t["date"] for t in trades})
                variants.append({"stop": stop_rule, "target_R": target_r, "cutoff": cutoff, "trades": n,
                                 "win_rate": wins / n, "avg_R": avg_r, "avg_usd": usd / n,
                                 "one_a_day_usd": usd / n * ndays, "days": ndays,
                                 "stops": sum(t["why"] == "stop" for t in trades) / n,
                                 "targets": sum(t["why"] == "target" for t in trades) / n})
    variants.sort(key=lambda v: -v["avg_R"])
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "results.json"), "w") as fh:
        json.dump({"run_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z", "symbols": [p[0] for p in picks],
                   "sessions_per_symbol": max((len(v) for v in data.values()), default=0),
                   "variants": variants}, fh, indent=1)
    print("\n%-4s %-4s %-6s %7s %6s %7s %8s %7s %7s %10s" % ("stop", "tgt", "until", "trades", "win%",
                                                           "avgR", "$/trade", "stop%", "tgt%", "1/day $"))
    for v in variants:
        print("%-4s %-4s %-6s %7d %5.0f%% %+7.3f %+8.3f %6.0f%% %6.0f%% %+10.2f" % (
            v["stop"], "%gR" % v["target_R"], v["cutoff"], v["trades"], 100 * v["win_rate"], v["avg_R"],
            v["avg_usd"], 100 * v["stops"], 100 * v["targets"], v["one_a_day_usd"]))


if __name__ == "__main__":
    sys.exit(main())
