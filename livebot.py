"""Live PAPER trading bot for an always-on server. Yours to run: you hold the keys, you start it, you stop it.

SAFETY (all in this file, read it):
  * PAPER ONLY. It talks to paper-api.alpaca.markets and nothing else. There is no live-money endpoint in
    this file. Switching to real money means editing this code yourself, on purpose, later.
  * DRY RUN by default: it logs the order it WOULD send. Add --send to place paper orders.
  * Keys come from environment variables ALPACA_API_KEY / ALPACA_SECRET_KEY (paper keys start with PK).
  * Same rules as the shadow engine: stocks under $150, one position at a time, max 3 trades a day,
    Time Machine skips, dynamic size (1% of balance, half after 2 losses in a row or once the day is
    up 0.75%), stop for the day at -1.5% or +2%, never size up to win back a loss.
  * ALWAYS FLAT: at 15:50 New York it cancels open orders and closes every position, then checks.
  * Kill switch: create the file trader/STOP and the bot stops opening trades and flattens.
  * Every decision and order goes to trader/live-log.md.

    python trader/livebot.py                       # dry run, loops all day, all week
    python trader/livebot.py --send                # sends PAPER bracket orders
    python trader/livebot.py --replay "2026-11-13 10:15"   # one decision at a past time, no Alpaca, no orders
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
PY = sys.executable
TMP = ROOT / "trader" / "live-tmp"
TMP.mkdir(parents=True, exist_ok=True)
STATE_PATH = ROOT / "trader" / "live-state.json"
LOG_PATH = ROOT / "trader" / "live-log.md"
STOP_PATH = ROOT / "trader" / "STOP"
PAPER_BASE = "https://paper-api.alpaca.markets"  # the ONLY broker endpoint in this file
MAX_TRADES, MAX_PRICE = 3, 150.0
RISK_PCT, LOSS_STOP_PCT, WIN_STOP_PCT, PROTECT_PCT = 0.01, 0.015, 0.02, 0.0075
FLATTEN_AT = "15:50"
LAST_ENTRY = "15:30"
SEND = "--send" in sys.argv


def log(msg):
    line = f"- {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}Z {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(args):
    r = subprocess.run([PY] + args, capture_output=True, text=True, encoding="utf-8")
    return r.stdout + r.stderr


# ---------------- Alpaca (paper only) ----------------
def keys():
    k, s = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if not k.startswith("PK") or not s:
        sys.exit("Refusing to run: ALPACA_API_KEY must be a PAPER key (starts with PK) and ALPACA_SECRET_KEY must be set.")
    return k, s


def alp(method, path, body=None):
    k, s = keys()
    req = urllib.request.Request(PAPER_BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            t = r.read().decode()
            return json.loads(t) if t else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Alpaca {method} {path}: {e.code} {e.read().decode()[:300]}")


def parse_ts(s):
    s = re.sub(r"(\.\d+)", "", s)  # drop fractional seconds
    return dt.datetime.fromisoformat(s)


# ---------------- state ----------------
def load_state(day, bal0):
    if STATE_PATH.exists():
        st = json.load(open(STATE_PATH))
        if st.get("day") == day:
            return st
    return {"day": day, "bal0": bal0, "realized": 0.0, "consec": 0, "trades": 0, "open": None,
            "tried": [], "flattened": False, "list_ok": False}


def save_state(st):
    json.dump(st, open(STATE_PATH, "w"), indent=1)


# ---------------- decisions (same pipeline as the shadow engine) ----------------
def hhmm(s):
    return dt.datetime.strptime(s.strip(), "%I:%M %p").strftime("%H:%M")


def decide(st, day, T, replay):
    """Returns (order_dict, note). Looks for a fresh breakout at time T and applies every rule."""
    bal0, realized = st["bal0"], st["realized"]
    if realized <= -LOSS_STOP_PCT * bal0:
        return None, f"stopped for the day: loss limit ({realized:+.2f})"
    if realized >= WIN_STOP_PCT * bal0:
        return None, f"stopped for the day: banked the day ({realized:+.2f})"
    if st["trades"] >= MAX_TRADES:
        return None, "max trades reached"
    if T > LAST_ENTRY:
        return None, "no new entries after 15:30"
    now_args = ["--now", f"{day} {T}"] if replay else []
    wl = f"trader/watchlist-premarket-{day}.json"
    uni = Path("trader/universe.txt")
    if not uni.exists() or time.time() - uni.stat().st_mtime > 7 * 86400:
        run(["trader-scanner/scan.py", "universe"])  # refresh the stock list about once a week
    if not Path(wl).exists():
        if replay:
            run(["trader-scanner/scan.py", "daily", "--now", f"{day} 08:00"])
        else:
            run(["trader-scanner/scan.py", "daily"])
        if Path("trader/watchlist.json").exists():
            Path(wl).write_bytes(Path("trader/watchlist.json").read_bytes())
        if not Path(wl).exists():
            return None, "pre-market list not ready yet"
    daily = {x["symbol"]: x for x in json.load(open(wl))["hits"]}
    out = run(["trader-scanner/scan.py", "orb", "--watchlist", wl, "--max-confirm", "0", "--json", "--no-write"] + now_args)
    if "{" not in out:
        return None, f"no opening-range scan: {out.strip()[:100]}"
    hits = json.loads(out[out.index("{"):])["hits"]
    cur = dt.datetime.strptime(T, "%H:%M")
    fresh = []
    for h in hits:
        if h["price"] >= MAX_PRICE or h["symbol"] not in daily or h["symbol"] in st["tried"]:
            continue
        trig = dt.datetime.strptime(hhmm(h["at"]), "%H:%M")
        if 0 <= (cur - trig).total_seconds() <= 5 * 60:  # triggered on the bar that just closed
            d = daily[h["symbol"]]
            fresh.append((-d["price"] * d["volume"], h["symbol"]))
    fresh.sort()
    if not fresh:
        return None, "no fresh breakout this bar"
    m, why = 1.0, "normal size"
    if st["consec"] >= 2:
        m, why = 0.5, f"half size after {st['consec']} losses in a row"
    if realized >= PROTECT_PCT * bal0:
        m, why = 0.5, "half size to protect the day's gain"
    bal_now = bal0 + realized
    notes = []
    for _, sym in fresh[:8]:
        st["tried"].append(sym)
        e = run(["trader-entry/entry.py", "check", sym, "--setup", "orb", "--write"] + now_args)
        if "TRIGGERED" not in e:
            notes.append(f"{sym}: entry check not triggered"); continue
        run(["trader-exit/exit.py", "plan", "--from", "trader/entry.json", "--write"] + now_args)
        tm = run(["trader-timemachine/timemachine.py", "run", "--plan", "trader/plan.json", "--seed", "13", "--write"] + now_args)
        mm = re.search(r"^(Strongly toward the (stop|target)|Leans toward the (stop|target)|Close to break-even)", tm, re.M)
        verdict = mm.group(1) if mm else "no verdict"
        if verdict == "no verdict" or verdict.startswith(("Strongly toward the stop", "Leans toward the stop")):
            notes.append(f"{sym}: skipped, Time Machine {verdict}"); continue
        cfg = json.load(open("trader/config.json"))
        cfg.update({"max_loss_per_trade_usd": round(RISK_PCT * bal_now * m, 2), "max_daily_loss_usd": round(LOSS_STOP_PCT * bal0 * 2, 2),
                    "budget": int(bal_now), "max_order_usd": round(0.9 * bal_now), "paper": True})
        json.dump(cfg, open(TMP / "config.json", "w"))
        json.dump({"cash": round(bal_now, 2), "positions": {}}, open(TMP / "pf.json", "w"))
        sz = run(["trader-budget/size.py", "--config", str(TMP / "config.json"), "--portfolio", str(TMP / "pf.json"),
                  "--budget", str(int(bal_now)), "--write"] + now_args)
        ms = re.search(r"^Size: (\d+) shares", sz, re.M)
        if not ms:
            notes.append(f"{sym}: no order fits the budget"); continue
        qty = int(ms.group(1))
        rk = run(["trader-risk/check.py", "--config", str(TMP / "config.json"), "--portfolio", str(TMP / "pf.json"),
                  "--today", day, "--out", str(TMP / "risk.json")] + now_args)
        if "PASS" not in rk.split("\n")[0] or f"buy {sym}" not in rk:
            notes.append(f"{sym}: risk check did not pass"); continue
        plan = json.load(open("trader/plan.json"))
        order = {"symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day",
                 "order_class": "bracket",
                 "take_profit": {"limit_price": f"{float(plan['target']):.2f}"},
                 "stop_loss": {"stop_price": f"{float(plan['stop']):.2f}"}}
        info = {"sym": sym, "entry": float(plan["entry"]), "stop": float(plan["stop"]), "tgt": float(plan["target"]),
                "qty": qty, "verdict": verdict, "size_reason": why, "T": T}
        return (order, info), "; ".join(notes + [f"{sym}: all rules passed ({verdict}, {why})"])
    return None, "; ".join(notes) or "nothing passed"


# ---------------- order management ----------------
def flatten(st, reason):
    log(f"FLATTEN ({reason}): canceling orders and closing all positions" + ("" if SEND else " [dry run: nothing sent]"))
    if SEND:
        alp("DELETE", "/v2/orders")
        time.sleep(2)
        alp("DELETE", "/v2/positions?cancel_orders=true")
        time.sleep(5)
        left = alp("GET", "/v2/positions")
        if left:
            log("WARNING: positions still open after flatten: " + ", ".join(p["symbol"] for p in left))
        else:
            log("Flat: no open positions.")
    st["open"] = None
    st["flattened"] = True


def sync_open(st):
    """If our bracket closed on its own (stop or target), book the P&L."""
    o = st["open"]
    if not o or o.get("dry"):
        return
    parent = alp("GET", f"/v2/orders/{o['id']}?nested=true")
    fills = [l for l in parent.get("legs", []) if l.get("status") == "filled"]
    if parent.get("status") == "filled" and fills:
        exit_px = float(fills[0]["filled_avg_price"])
        entry_px = float(parent["filled_avg_price"])
        pnl = round((exit_px - entry_px) * o["qty"], 2)
        st["realized"] = round(st["realized"] + pnl, 2)
        st["consec"] = st["consec"] + 1 if pnl < 0 else 0
        log(f"CLOSED {o['sym']}: in {entry_px:.2f}, out {exit_px:.2f}, {pnl:+.2f}. Day {st['realized']:+.2f}, losses in a row {st['consec']}")
        st["open"] = None
    elif parent.get("status") in ("canceled", "expired", "rejected"):
        log(f"order for {o['sym']} ended as {parent['status']} without a fill")
        st["open"] = None


def tick(st, day, T, replay=False):
    if replay:
        order, note = decide(st, day, T, True)
        log(f"REPLAY {day} {T}: {note}")
        if order:
            log("REPLAY would send: " + json.dumps(order[0]))
        return
    if STOP_PATH.exists():
        if not st["flattened"]:
            flatten(st, "STOP file present")
        return
    if T >= FLATTEN_AT:
        if not st["flattened"]:
            if SEND:
                sync_open(st)
            flatten(st, f"{FLATTEN_AT} close-out")
        return
    if SEND:
        sync_open(st)
        pos = alp("GET", "/v2/positions")
        if pos and not st["open"]:
            log("UNEXPECTED position without a bot order: " + ", ".join(p["symbol"] for p in pos))
            flatten(st, "unexpected position")
            return
    if st["open"]:
        return
    order, note = decide(st, day, T, False)
    if not order:
        log(f"{T} no trade: {note}")
        return
    body, info = order
    log(f"{T} SIGNAL {info['sym']}: {info['qty']} shares, stop {info['stop']}, target {info['tgt']} ({info['size_reason']}). {note}")
    st["trades"] += 1
    if SEND:
        r = alp("POST", "/v2/orders", body)
        info["id"] = r["id"]
        st["open"] = info
        log(f"PAPER ORDER sent: {json.dumps(body)} -> id {r['id']} status {r.get('status')}")
    else:
        st["open"] = dict(info, dry=True)
        log("DRY RUN: would send " + json.dumps(body))


def main():
    if "--replay" in sys.argv:
        stamp = sys.argv[sys.argv.index("--replay") + 1]
        day, T = stamp.split()
        st = {"day": day, "bal0": 1000.0, "realized": 0.0, "consec": 0, "trades": 0, "open": None, "tried": [], "flattened": False}
        tick(st, day, T, replay=True)
        return
    keys()
    log(f"bot started. mode: {'PAPER ORDERS ENABLED (--send)' if SEND else 'DRY RUN (no orders)'}")
    errors = 0
    while True:
        try:
            c = alp("GET", "/v2/clock")
            now = parse_ts(c["timestamp"])
            day, T = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
            if not c["is_open"]:
                nxt = parse_ts(c["next_open"])
                wait = min(600, max(30, (nxt - now).total_seconds() - 90))
                time.sleep(wait)
                continue
            acct = alp("GET", "/v2/account")
            st = load_state(day, float(acct["last_equity"]))
            tick(st, day, T)
            save_state(st)
            errors = 0
        except Exception as e:  # keep running, but say so
            errors += 1
            log(f"ERROR {errors}: {e}")
            if errors >= 5 and SEND:
                try:
                    flatten({"open": None}, "5 errors in a row: closing out to be safe")
                except Exception as e2:
                    log(f"flatten failed: {e2}")
        # next 5-minute bar boundary + 20 s
        s = dt.datetime.utcnow()
        time.sleep(300 - ((s.minute % 5) * 60 + s.second) + 20)


if __name__ == "__main__":
    main()
