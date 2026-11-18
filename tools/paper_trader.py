#!/usr/bin/env python3
"""Autonomous PAPER trader built on the installed trader-* skills.

No broker, no real money. It runs the skills' own pipeline on live public
data and records simulated fills at the live quote:

  scanner (orb) -> entry check -> exit plan (from the live price) ->
  time machine (edge gate) -> budget -> risk (must PASS) -> paper fill

One position at a time. While a position is open it polls the quote and
exits at the stop, at the target, or at 15:55 New York. Every fill goes
through trader-broker's `ticket.py fill`, and every entry is logged in
trader/state/broker.json as an "auto-paper" record (never as a user "yes")
so trader-risk's daily loss limit counts it.

Usage (from the repo root):
    python3 tools/paper_trader.py run           # trade until 15:58 New York
    python3 tools/paper_trader.py summary       # print the running results

Files (all under trader/paper/): state.json, trades.csv, events.log,
journal.md, lock. Paper only: settlement, PDT and slippage beyond the
quote are not modelled.
"""
import csv
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SK = os.path.join(ROOT, ".claude", "skills")
T = os.path.join(ROOT, "trader")
PD = os.path.join(T, "paper")
NY = ZoneInfo("America/New_York")
PY = [sys.executable, "-I"]

SCAN_EVERY_MIN = 8        # minutes between market scans while flat
FRESH_MIN = 12            # a breakout bar must have closed within this many minutes
MAX_PRICE = 60.0          # $100 account, 75% position cap: leave room for >= 1 share
MIN_PRICE = 2.0
MAX_CANDIDATES = 6        # candidates tried per scan
POLL_SEC = 20             # quote polling while a position is open
GOAL = 500000.0

_stop = False
_critical = False


def now_ny():
    return dt.datetime.now(NY)


def hm(d):
    return d.hour * 60 + d.minute


def log(msg):
    os.makedirs(PD, exist_ok=True)
    line = "%s  %s" % (now_ny().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(os.path.join(PD, "events.log"), "a") as fh:
        fh.write(line + "\n")


def rj(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def tool(rel, *args, timeout=900):
    """Run a skill script from the repo root. Returns (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(PY + [os.path.join(SK, rel)] + list(args), cwd=ROOT,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def tool_json(rel, *args, timeout=900):
    rc, out, err = tool(rel, *(list(args) + ["--json"]), timeout=timeout)
    try:
        return rc, json.loads(out), err
    except ValueError:
        return rc, None, (err or out)[-400:]


def quote(sym):
    rc, out, err = tool("trader-alerts/alerts.py", "quote", sym, "--write", timeout=60)
    p = (rj(os.path.join(T, "prices.json"), {}) or {}).get("prices", {})
    return p.get(sym.upper())


# ---------------------------------------------------------------- state

def load_state():
    return rj(os.path.join(PD, "state.json"), None) or {"open": None, "tried": {}, "days": {}}


def save_state(st):
    wj(os.path.join(PD, "state.json"), st)


def cash():
    pf = rj(os.path.join(T, "portfolio.json"), None)
    if pf is None:
        cfg = rj(os.path.join(T, "config.json"), {}) or {}
        return float(cfg.get("budget", 100))
    return float(pf.get("cash", 0))


def record_trade(op, exit_price, reason):
    pnl = round((exit_price - op["entry"]) * op["qty"], 2)
    r = round((exit_price - op["entry"]) / op["risk_per_share"], 2) if op["risk_per_share"] else 0
    path = os.path.join(PD, "trades.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "symbol", "qty", "entry_time", "entry", "stop", "target",
                        "exit_time", "exit", "reason", "pnl_usd", "R", "cash_after"])
        w.writerow([op["date"], op["symbol"], op["qty"], op["entered_at"], op["entry"], op["stop"],
                    op["target"], now_ny().strftime("%H:%M:%S"), exit_price, reason, pnl, r,
                    round(cash(), 2)])
    return pnl, r


def log_entry_in_broker_state(op):
    """So trader-risk counts today's entries and their max loss. Marked auto-paper."""
    path = os.path.join(T, "state", "broker.json")
    st = rj(path, None) or {"orders": []}
    st.setdefault("orders", []).append({
        "at": now_ny().strftime("%Y-%m-%dT%H:%M:%S"), "date": op["date"], "symbol": op["symbol"],
        "side": "buy", "qty": str(op["qty"]), "kind": "entry", "max_loss": op["max_loss"],
        "stop": op["stop"], "status": "placed",
        "note": "auto-paper: simulated fill at the live quote, no broker, no user approval involved"})
    wj(path, st)


# ---------------------------------------------------------------- git

def secret_values():
    v = os.environ.get("SEC_USER_AGENT", "")
    return [x for x in v.split() if "@" in x]


def git_sync(msg):
    """Commit trader/ and push. Refuses if the SEC contact shows up in any file."""
    bad = secret_values()
    if bad:
        for dirpath, _, files in os.walk(T):
            for f in files:
                try:
                    with open(os.path.join(dirpath, f), errors="ignore") as fh:
                        txt = fh.read()
                except OSError:
                    continue
                if any(b in txt for b in bad):
                    log("git: NOT committing, %s contains the SEC contact" % os.path.join(dirpath, f))
                    return
    g = lambda *a: subprocess.run(["git"] + list(a), cwd=ROOT, capture_output=True, text=True, timeout=120)
    g("add", "trader")
    if g("diff", "--cached", "--quiet").returncode == 0:
        return
    g("commit", "-q", "-m", "paper trader: " + msg)
    branch = g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for i in range(4):
        if g("push", "-q", "-u", "origin", branch).returncode == 0:
            return
        g("pull", "-q", "--no-rebase", "--no-edit", "origin", branch)
        time.sleep(2 ** (i + 1))
    log("git: push failed after retries")


# ---------------------------------------------------------------- trading

def manage_open(st):
    op = st["open"]
    px = quote(op["symbol"])
    if px is None:
        return
    t = now_ny()
    reason = None
    exit_px = px
    if px <= op["stop"]:
        reason = "stop"            # a stop fills at the market: the quote, maybe worse than the stop
    elif px >= op["target"]:
        reason, exit_px = "target", op["target"]   # a limit fills at its price
    elif hm(t) >= 15 * 60 + 55:
        reason = "flat by 15:55"
    if not reason:
        return
    global _critical
    _critical = True
    rc, out, err = tool("trader-broker/ticket.py", "fill", "--symbol", op["symbol"], "--side", "sell",
                        "--qty", str(op["qty"]), "--price", "%.4f" % exit_px)
    if rc != 0:
        _critical = False
        log("ERROR recording the sell: %s" % (err or out).strip()[-300:])
        return
    pnl, r = record_trade(op, exit_px, reason)
    day = st["days"].setdefault(op["date"], {"trades": 0, "wins": 0, "pnl": 0.0})
    day["trades"] += 1
    day["wins"] += 1 if pnl > 0 else 0
    day["pnl"] = round(day["pnl"] + pnl, 2)
    log("EXIT %s %d @ %.2f (%s): P/L $%.2f, %.2fR. Cash $%.2f" % (op["symbol"], op["qty"], exit_px,
                                                                 reason, pnl, r, cash()))
    st["open"] = None
    save_state(st)
    _critical = False
    git_sync("exit %s %s %+.2f" % (op["symbol"], reason, pnl))


def try_enter(st, hits):
    today = now_ny().date().isoformat()
    tried = set(st["tried"].get(today, []))
    t = now_ny()
    fresh = []
    for h in hits:
        if h["symbol"] in tried or not (MIN_PRICE <= h.get("price", 0) <= MAX_PRICE):
            continue
        try:
            hh, mm = map(int, (h.get("bar_start") or "00:00").split(":"))
        except ValueError:
            continue
        close_min = hh * 60 + mm + 5
        if 0 <= hm(t) - close_min <= FRESH_MIN:
            fresh.append(h)
    log("scan: %d hits, %d fresh and affordable" % (len(hits), len(fresh)))
    for h in fresh[:MAX_CANDIDATES]:
        sym = h["symbol"]
        st["tried"].setdefault(today, []).append(sym)
        save_state(st)
        rc, e, err = tool_json("trader-entry/entry.py", "check", sym, "--setup", "orb", "--write")
        if not e or e.get("status") != "triggered":
            log("  %s: entry not triggered (%s)" % (sym, (e or {}).get("status") or err))
            continue
        px = quote(sym)
        if px is None:
            continue
        rc, plan, err = tool_json("trader-exit/exit.py", "plan", "--entry", "%.4f" % px, "--write")
        if not plan or rc != 0:
            log("  %s: no plan at %.2f (%s)" % (sym, px, str(err).strip()[-200:]))
            continue
        rc, tm, err = tool_json("trader-timemachine/timemachine.py", "run", "--write", timeout=300)
        if not tm:
            log("  %s: time machine failed (%s)" % (sym, str(err).strip()[-200:]))
            continue
        mc, hist = tm.get("mc", {}), tm.get("history", {})
        edge = (mc.get("avg_R", -1) > 0 and hist.get("avg_R", -1) > 0 and hist.get("triggered", 0) >= 10)
        log("  %s @ %.2f stop %.2f target %.2f | sim avgR %.3f (T %.0f%% / S %.0f%%) | history %s fired, avgR %.3f"
            % (sym, px, plan["stop"], plan["target"], mc.get("avg_R", 0), 100 * mc.get("p_target", 0),
               100 * mc.get("p_stop", 0), hist.get("triggered"), hist.get("avg_R", 0)))
        if not edge:
            log("  %s: skipped, no edge" % sym)
            continue
        rc, sz, err = tool_json("trader-budget/size.py", "--write", "--portfolio", "trader/portfolio.json")
        s = (sz or {}).get("sizing", {})
        if not sz or s.get("refusal") or not s.get("qty"):
            log("  %s: sizing refused (%s)" % (sym, s.get("refusal") or str(err).strip()[-200:]))
            continue
        rc, ck, err = tool_json("trader-risk/check.py", "--portfolio", "trader/portfolio.json")
        res = (ck or {}).get("results", [{}])[0]
        if res.get("verdict") != "PASS":
            log("  %s: risk BLOCK: %s" % (sym, "; ".join(res.get("reasons", [])) or err))
            continue
        fill_px = quote(sym) or px
        if fill_px <= plan["stop"] or fill_px >= plan["target"]:
            log("  %s: price moved to %.2f before the fill, skipped" % (sym, fill_px))
            continue
        qty = int(s["qty"])
        global _critical
        _critical = True
        rc, out, err = tool("trader-broker/ticket.py", "fill", "--symbol", sym, "--side", "buy",
                            "--qty", str(qty), "--price", "%.4f" % fill_px)
        if rc != 0:
            _critical = False
            log("  %s: fill not recorded (%s)" % (sym, (err or out).strip()[-200:]))
            continue
        op = {"symbol": sym, "qty": qty, "entry": round(fill_px, 4), "stop": plan["stop"],
              "target": plan["target"], "risk_per_share": round(fill_px - plan["stop"], 4),
              "max_loss": round(qty * (fill_px - plan["stop"]), 2), "date": today,
              "entered_at": now_ny().strftime("%H:%M:%S")}
        st["open"] = op
        save_state(st)
        log_entry_in_broker_state(op)
        _critical = False
        log("ENTER %s %d @ %.2f, stop %.2f, target %.2f, max loss $%.2f. Cash $%.2f"
            % (sym, qty, fill_px, op["stop"], op["target"], op["max_loss"], cash()))
        git_sync("enter %s %d @ %.2f" % (sym, qty, fill_px))
        return True
    return False


def daily_loss_hit(st, cfg):
    today = now_ny().date().isoformat()
    lost = -min(0.0, st["days"].get(today, {}).get("pnl", 0.0))
    return lost >= float(cfg.get("max_daily_loss_usd", 20))


def write_journal(st):
    lines = ["# Paper trading journal", "",
             "Simulated fills at live quotes. No broker, no real money. Goal: $%s." % format(int(GOAL), ","),
             "", "| date | trades | wins | P/L | cash at end |", "| --- | --- | --- | --- | --- |"]
    for d in sorted(st["days"]):
        x = st["days"][d]
        lines.append("| %s | %d | %d | %+.2f | %s |" % (d, x["trades"], x["wins"], x["pnl"],
                                                       "%.2f" % x["cash"] if "cash" in x else ""))
    tot_t = sum(x["trades"] for x in st["days"].values())
    tot_w = sum(x["wins"] for x in st["days"].values())
    c = cash()
    lines += ["", "Cash now: $%.2f. Trades: %d, wins: %d (%s). Progress to goal: %.4f%%."
              % (c, tot_t, tot_w, ("%.0f%%" % (100.0 * tot_w / tot_t)) if tot_t else "n/a", 100 * c / GOAL), ""]
    with open(os.path.join(PD, "journal.md"), "w") as fh:
        fh.write("\n".join(lines))


def market_open_today():
    """A trading day? Ask the NYSE calendar about noon today (handles holidays)."""
    noon = now_ny().strftime("%Y-%m-%d") + " 12:00"
    rc, out, err = tool("trader-alerts/alerts.py", "clock", "--now", noon, timeout=60)
    return "NYSE OPEN" in out


def take_lock():
    path = os.path.join(PD, "lock")
    old = rj(path, None)
    if old and old.get("pid") and old["pid"] != os.getpid():
        try:
            os.kill(old["pid"], signal.SIGTERM)
            log("took over from run pid %s" % old["pid"])
            for _ in range(30):
                os.kill(old["pid"], 0)
                time.sleep(1)
            os.kill(old["pid"], signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    wj(path, {"pid": os.getpid(), "started": now_ny().isoformat()})


def on_term(*_):
    """Stop now unless a fill is being recorded; then stop right after it."""
    global _stop
    _stop = True
    if not _critical:
        raise SystemExit(0)


def run():
    signal.signal(signal.SIGTERM, on_term)
    os.makedirs(PD, exist_ok=True)
    take_lock()
    subprocess.run(["git", "pull", "-q", "--no-rebase", "--no-edit"], cwd=ROOT,
                   capture_output=True, timeout=120)
    if not os.path.exists(os.path.join(T, "portfolio.json")):
        cfg = rj(os.path.join(T, "config.json"), {}) or {}
        wj(os.path.join(T, "portfolio.json"), {"cash": float(cfg.get("budget", 100)), "positions": []})
    if not os.path.exists(os.path.join(T, "universe.txt")):
        tool("trader-scanner/scan.py", "universe", timeout=600)
    t = now_ny()
    if t.weekday() >= 5 or not market_open_today():
        log("market closed now, nothing to do")
        return
    st = load_state()
    cfg = rj(os.path.join(T, "config.json"), {}) or {}
    log("run start. Cash $%.2f, open: %s" % (cash(), st["open"]["symbol"] if st["open"] else "none"))
    last_scan = 0.0
    while not _stop:
        t = now_ny()
        m = hm(t)
        if m >= 15 * 60 + 58 or m < 9 * 60 + 30:
            break
        if st["open"]:
            manage_open(st)
            time.sleep(POLL_SEC)
            continue
        entries_ok = (9 * 60 + 50 <= m < 15 * 60 + 30) and not daily_loss_hit(st, cfg)
        if entries_ok and time.time() - last_scan >= SCAN_EVERY_MIN * 60:
            last_scan = time.time()
            rc, scan, err = tool_json("trader-scanner/scan.py", "orb", "--quiet", "--no-write",
                                      "--min-price", str(MIN_PRICE), timeout=900)
            if not scan:
                log("scan failed: %s" % str(err).strip()[-200:])
            elif try_enter(st, scan.get("hits", [])):
                continue
        time.sleep(30)
    st = load_state() if _stop else st
    if hm(now_ny()) >= 15 * 60 + 56 and not st["open"]:
        today = now_ny().date().isoformat()
        st["days"].setdefault(today, {"trades": 0, "wins": 0, "pnl": 0.0})["cash"] = round(cash(), 2)
        save_state(st)
        write_journal(st)
        log("day done. Cash $%.2f" % cash())
    git_sync("run end %s" % now_ny().strftime("%Y-%m-%d %H:%M"))
    log("run end" + (" (handed over)" if _stop else ""))


def summary():
    st = load_state()
    write_journal(st)
    print(open(os.path.join(PD, "journal.md")).read())
    if st["open"]:
        print("Open: %s" % json.dumps(st["open"]))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": run, "summary": summary}[cmd]()
