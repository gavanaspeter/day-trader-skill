"""Shadow mode: replay the latest finished US trading day with the trader skills. NO ORDERS, ever.

Builds the pre-market list from the PREVIOUS close, then walks the day's opening-range breakouts in
time order using your rules (price under $150, one position at a time, max 3 trades, skip when the
Time Machine leans toward the stop or has no verdict, size and risk check from your config, flat 15:55).
Appends the result to trader/shadow-log.md. Run it after the US close (8:00 AM Sydney, Tue-Sat).

    python trader/nightly.py            # latest finished session
    python trader/nightly.py 2026-11-13 # a specific day (within ~5 trading days of today)
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
PY = sys.executable
TMP = ROOT / "trader" / "nightly-tmp"
TMP.mkdir(parents=True, exist_ok=True)
MAX_TRADES, DAILY_LOSS, MAX_EVAL, MAX_PRICE = 3, 150.0, 40, 150.0
UA = {"User-Agent": "Mozilla/5.0"}


def run(args):
    r = subprocess.run([PY] + args, capture_output=True, text=True, encoding="utf-8")
    return r.stdout + r.stderr


def fetch_chart(sym, rng="5d"):
    u = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&range={rng}"
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30))["chart"]["result"][0]


def latest_session():
    r = fetch_chart("SPY", "5d")
    off = r["meta"]["gmtoffset"]
    ts = [t for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None]
    last = dt.datetime.fromtimestamp(ts[-1] + off, dt.timezone.utc).replace(tzinfo=None)
    if last.time() < dt.time(15, 50):
        # the latest session is still open or just closing: use the one before
        days = sorted({dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date() for t in ts})
        return days[-2].isoformat()
    return last.date().isoformat()


def hhmm(s):
    return dt.datetime.strptime(s.strip(), "%I:%M %p").strftime("%H:%M")


def add_min(t, m):
    return (dt.datetime.strptime(t, "%H:%M") + dt.timedelta(minutes=m)).strftime("%H:%M")


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else latest_session()
    uni = Path("trader/universe.txt")
    if not uni.exists() or dt.datetime.now().timestamp() - uni.stat().st_mtime > 7 * 86400:
        run(["trader-scanner/scan.py", "universe"])  # refresh the stock list about once a week
    wl = f"trader/watchlist-premarket-{day}.json"
    run(["trader-scanner/scan.py", "daily", "--now", f"{day} 08:00"])
    shutil.copy("trader/watchlist.json", wl)
    asof = json.load(open(wl))["as_of"]
    daily = {x["symbol"]: x for x in json.load(open(wl))["hits"]}
    out = run(["trader-scanner/scan.py", "orb", "--watchlist", wl, "--now", f"{day} 15:25",
               "--max-confirm", "0", "--json", "--no-write"])
    if "{" not in out:
        print(f"{day}: no opening-range scan possible ({out.strip()[:120]})")
        return
    j = json.loads(out[out.index("{"):])
    cands = sorted((hhmm(h["at"]), -daily[h["symbol"]]["price"] * daily[h["symbol"]]["volume"], h["symbol"])
                   for h in j["hits"] if h["price"] < MAX_PRICE and h["symbol"] in daily)
    cache = {}

    def bars(sym):
        if sym not in cache:
            r = fetch_chart(sym)
            off = r["meta"]["gmtoffset"]
            q = r["indicators"]["quote"][0]
            L = []
            for t, h, l, c in zip(r["timestamp"], q["high"], q["low"], q["close"]):
                d = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).replace(tzinfo=None)
                if d.strftime("%Y-%m-%d") == day and h is not None:
                    L.append((d.strftime("%H:%M"), h, l, c))
            cache[sym] = L
        return cache[sym]

    cash, cursor, trades, skips, evals, realized = 1000.0, "09:50", [], [], 0, 0.0
    pf = str(TMP / "pf.json")
    for T, _, sym in cands:
        if len(trades) >= MAX_TRADES or evals >= MAX_EVAL or realized <= -DAILY_LOSS:
            break
        if T < cursor or T > "15:30":
            continue
        evals += 1
        now = f"{day} {T}"
        if "TRIGGERED" not in run(["trader-entry/entry.py", "check", sym, "--setup", "orb", "--now", now, "--write"]):
            skips.append(f"{sym} {T}: not triggered at that time"); continue
        run(["trader-exit/exit.py", "plan", "--from", "trader/entry.json", "--now", now, "--write"])
        tm = run(["trader-timemachine/timemachine.py", "run", "--plan", "trader/plan.json", "--now", now, "--seed", "13", "--write"])
        m = re.search(r"^(Strongly toward the (stop|target)|Leans toward the (stop|target)|Close to break-even)", tm, re.M)
        verdict = m.group(1) if m else "no verdict"
        p = re.search(r"target first\s+([\d.]+) %\s+stop first\s+([\d.]+) %\s+neither\s+([\d.]+) %", tm)
        odds = f"{p.group(1)}/{p.group(2)}/{p.group(3)}" if p else "?"
        if verdict == "no verdict" or verdict.startswith(("Strongly toward the stop", "Leans toward the stop")):
            skips.append(f"{sym} {T}: skipped, Time Machine {verdict} ({odds})"); continue
        json.dump({"cash": round(cash, 2), "positions": {}}, open(pf, "w"))
        sz = run(["trader-budget/size.py", "--portfolio", pf, "--now", now, "--budget", str(int(cash)), "--write"])
        ms = re.search(r"^Size: (\d+) shares", sz, re.M)
        if not ms:
            skips.append(f"{sym} {T}: no order fits the budget"); continue
        n = int(ms.group(1))
        rk = run(["trader-risk/check.py", "--portfolio", pf, "--now", now, "--today", day, "--out", str(TMP / "risk.json")])
        if "PASS" not in rk.split("\n")[0] or f"buy {sym}" not in rk:
            skips.append(f"{sym} {T}: risk check did not pass"); continue
        plan = json.load(open("trader/plan.json"))
        entry, stop, tgt = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
        res, last = None, None
        for bt, h, l, c in bars(sym):
            if bt < T:
                continue
            if bt > "15:50":
                break
            if l <= stop:
                res = ("stop", bt, stop); break
            if h >= tgt:
                res = ("target", bt, tgt); break
            last = c
        if res is None:
            res = ("flat 15:55", "15:50", last if last is not None else entry)
        pnl = round((res[2] - entry) * n, 2)
        cash += pnl; realized += pnl; cursor = add_min(res[1], 5)
        trades.append((sym, T, entry, stop, tgt, n, verdict, odds, res[0], pnl))
        os.remove("trader/orders.json") if os.path.exists("trader/orders.json") else None

    lines = [f"\n### {day} (list from {asof}; shadow mode, no orders)"]
    for t in trades:
        lines.append(f"- {t[0]} bought {t[1]} at {t[2]:.2f}, stop {t[3]:.2f}, target {t[4]:.2f}, {t[5]} shares, "
                     f"Time Machine {t[6]} ({t[7]}): {t[8]}, {t[9]:+.2f}")
    if not trades:
        lines.append("- no trade passed the rules today")
    for s in skips:
        lines.append(f"- skipped {s}")
    lines.append(f"- **Day total: {realized:+.2f}** on {len(trades)} trade(s). Checked {evals} breakouts.")
    text = "\n".join(lines)
    with open("trader/shadow-log.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
