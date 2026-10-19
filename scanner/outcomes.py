#!/usr/bin/env python3
"""Outcome tracking for PASS tokens (+ random KILL control group).
Lightweight: DexScreener + GeckoTerminal only (no Helius). Writes data/outcomes.json."""
import json, os, random, time, statistics
from datetime import datetime, timezone
import requests

_HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.environ.get("FD_REPO") or os.path.dirname(_HERE)
D = os.environ.get("FD_DATA_DIR") or os.path.join(REPO, "data")
LOG = os.path.join(D, "checks_log.jsonl")
OUT = os.path.join(D, "outcomes.json")

TRACK_DAYS = 7
HORIZONS_H = [1, 6, 24, 72]
CONTROL_PER_SCAN = 5          # random KILLs per scan (scan = same UTC hour bucket)
RUG_LIQ_USD = 1000
RUG_MULT, DEAD_MULT, WIN_PEAK = 0.10, 0.30, 2.0
GECKO_DELAY = 3.0
USER_NOTES = {
    "FhTRVAkQeiaLRJHYc13DBNhgw9N8ZPacvonaKfXxpump": "user bought ~17:05 MST Oct 18",
    "BxaCEL9DM7SgaNHYaTy7o812mBCvE1QHr4QZbU2cpump": "user got rugged on this",
}

def ts(s): return datetime.fromisoformat(s).timestamp()

def load_candidates():
    passes, kills_by_scan = {}, {}
    for line in open(LOG):
        try: e = json.loads(line)
        except Exception: continue
        v, m = e.get("verdict"), e.get("mint")
        if v == "PASS" and m not in passes:
            loose = "lpLockedPct" in e and "metrics" not in e   # v1 filter format
            passes[m] = (e, "pass_v1_loose" if loose else "pass")
        elif v == "KILL":
            kills_by_scan.setdefault(e["timestamp"][:13], {}).setdefault(m, e)
    return passes, kills_by_scan

def dex_batch(mints):
    out = {}
    for i in range(0, len(mints), 30):
        try:
            r = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i+30]), timeout=15)
            for p in r.json() or []:
                m = (p.get("baseToken") or {}).get("address")
                if m not in mints: continue
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if m not in out or liq > ((out[m].get("liquidity") or {}).get("usd") or 0): out[m] = p
        except Exception as ex: print("dex error", ex)
        time.sleep(0.3)
    return out

_last = [0.0]
def gecko_ohlcv(pool, since):
    for t in range(4):
        w = GECKO_DELAY - (time.time() - _last[0])
        if w > 0: time.sleep(w)
        try:
            r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour?limit=200", timeout=20)
            _last[0] = time.time()
            if r.status_code == 429: time.sleep(6 * 2 ** t); continue
            rows = sorted(r.json()["data"]["attributes"]["ohlcv_list"])
            return [x for x in rows if x[0] + 3600 > since]
        except Exception:
            _last[0] = time.time(); time.sleep(2)
    return None

def update():
    try: O = json.load(open(OUT))
    except Exception: O = {"tokens": {}, "control_scans": []}
    T = O["tokens"]
    passes, kbs = load_candidates()
    for m, (e, grp) in passes.items():
        if m not in T or T[m]["group"].startswith("kill"):
            mt = e.get("metrics") or {}
            T[m] = {"group": grp, "symbol": mt.get("symbol"), "t0": e["timestamp"],
                    "entry_mcap": mt.get("fdv"), "entry_liq": mt.get("liquidity_usd"), "snapshots": []}
    rnd = random.Random(42)
    for scan, kills in sorted(kbs.items()):
        if scan in O["control_scans"]: continue
        pick = rnd.sample(sorted(kills), min(CONTROL_PER_SCAN, len(kills)))
        for m in pick:
            if m in T: continue
            mt = kills[m].get("metrics") or {}
            T[m] = {"group": "kill_control", "symbol": mt.get("symbol"), "t0": kills[m]["timestamp"],
                    "entry_mcap": mt.get("fdv"), "entry_liq": mt.get("liquidity_usd"),
                    "kill_reason": (kills[m].get("reasons") or [""])[0], "snapshots": []}
        O["control_scans"].append(scan)
    now = time.time()
    live = [m for m, t in T.items() if not t.get("final")]
    dex = dex_batch(live)
    for m in live:
        t = T[m]; p = dex.get(m); t0 = ts(t["t0"]); age_h = (now - t0) / 3600
        if p:
            price = float(p.get("priceUsd") or 0); mc = p.get("marketCap") or p.get("fdv")
            liq = (p.get("liquidity") or {}).get("usd")
            t["snapshots"].append([int(now), price, mc, liq])
            t["symbol"] = t.get("symbol") or (p.get("baseToken") or {}).get("symbol")
            t["pool"] = p.get("pairAddress"); t["cur_liq"] = liq; t["cur_mcap"] = mc; t["cur_price"] = price
        else:
            t["snapshots"].append([int(now), None, None, None]); t["cur_liq"] = 0
        # history from candles
        rows = gecko_ohlcv(t["pool"], t0) if t.get("pool") else None
        if rows:
            first = rows[0]
            if t.get("entry_price") is None:
                t["entry_price"] = first[4] if first[0] <= t0 else first[1]
                t["entry_approx"] = first[0] > t0 + 3600
            for h in HORIZONS_H:
                tgt = t0 + h * 3600
                if tgt <= now:
                    inside = [x for x in rows if x[0] <= tgt < x[0] + 3600]   # candle containing the target time
                    c = inside[0] if inside else min(rows, key=lambda x: abs(x[0] - tgt))
                    if abs(c[0] - tgt) <= 7200: t[f"p{h}h"] = c[4]
            after = [x for x in rows if x[0] >= t0 - 1800] or rows[-1:]   # skip candle mostly before pass time
            t["peak_price"] = max([x[2] for x in after] + [t.get("peak_price") or 0])
            t["min_price"] = min([x[3] for x in after] + ([t["min_price"]] if t.get("min_price") else []))
        elif t.get("entry_price") is None and t.get("cur_price"):
            t["entry_price"] = t["cur_price"]; t["entry_approx"] = True   # first observation
        # snapshot fallbacks for horizons
        for h in HORIZONS_H:
            if f"p{h}h" not in t and t0 + h * 3600 <= now:
                sn = [s for s in t["snapshots"] if s[1]]
                if sn:
                    c = min(sn, key=lambda s: abs(s[0] - (t0 + h * 3600)))
                    if abs(c[0] - (t0 + h * 3600)) < max(3600, h * 900): t[f"p{h}h"] = c[1]
        ep = t.get("entry_price")
        if ep:
            if t.get("entry_mcap") is None and t.get("cur_mcap") and t.get("cur_price"):
                t["entry_mcap"] = t["cur_mcap"] / t["cur_price"] * ep
            t["peak_price"] = max(t.get("peak_price") or 0, *(s[1] for s in t["snapshots"] if s[1])) if any(s[1] for s in t["snapshots"]) else t.get("peak_price")
            t["cur_mult"] = (t.get("cur_price") or 0) / ep
            t["peak_mult"] = (t.get("peak_price") or ep) / ep
            t["max_drawdown"] = ((t.get("min_price") or t.get("cur_price") or ep) / ep) - 1
            for h in HORIZONS_H:
                if t.get(f"p{h}h"): t[f"m{h}h"] = t[f"p{h}h"] / ep
        cm = t.get("cur_mult")
        if (t.get("cur_liq") or 0) < RUG_LIQ_USD or (cm is not None and cm <= RUG_MULT): t["status"] = "RUGGED"
        elif (t.get("peak_mult") or 0) >= WIN_PEAK: t["status"] = "WINNER"
        elif cm is not None and cm <= DEAD_MULT: t["status"] = "DEAD"
        else: t["status"] = "open"
        if age_h >= TRACK_DAYS * 24: t["final"] = True
        if m in USER_NOTES: t["note"] = USER_NOTES[m]
        t["snapshots"] = t["snapshots"][-400:]
    O["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    O["summary"] = summarize(T)
    json.dump(O, open(OUT, "w"), indent=0)
    return O["summary"]

def summarize(T):
    out = {}
    for g in ("pass", "pass_v1_loose", "kill_control"):
        xs = [t for t in T.values() if t["group"] == g and t.get("entry_price")]
        m24 = [t["m24h"] for t in xs if t.get("m24h")]
        n = len(xs) or 1
        out[g] = dict(tracked=len(xs), hit2x=round(100 * sum((t.get("peak_mult") or 0) >= 2 for t in xs) / n, 1),
                      rugged_or_dead=round(100 * sum(t.get("status") in ("RUGGED", "DEAD") for t in xs) / n, 1),
                      median_24h=round(statistics.median(m24), 2) if m24 else None,
                      median_cur=round(statistics.median([t["cur_mult"] for t in xs]), 2) if xs else None)
    return out

if __name__ == "__main__":
    print(json.dumps(update(), indent=1))
