"""Reusable rule backtester (read-only, keyless, 0 Helius credits).
Groups every mint in data/checks_log.jsonl by its FIRST check matching a filter, then scores what happened after:
  winner = peak >= 2x first-check FDV or now >= 1.5x;  rug = now <= 0.3x or no pair;  else flat.
Peaks come from data/outcomes.json snapshots (free) and, for mints without snapshots, GeckoTerminal hourly highs
(capped by --gecko N). Current FDVs come from DexScreener (30 mints/request). Everything network is cached in
data/backtest_cache.json (gitignored): current FDVs for --max-age-h hours, peaks forever (extended on refresh).
Examples:
  python scanner/backtest_rules.py --reason "insider" --verdict KILL            # the Nov 7 insider-KILL study
  python scanner/backtest_rules.py --reason "lopsided" --verdict KILL --only "lopsided"
  python scanner/backtest_rules.py --verdict PASS --tag bundled_launch          # PASS with a warn tag
  python scanner/backtest_rules.py --reason insider --verdict KILL --where "fdv>=1e6 and age_d>=3 and liq>=1e5"
--where takes a Python expression over: fdv, liq, age_d, peak_x, now_x (no other names). Output: markdown table.
"""
import argparse, datetime, json, os, re, sys, time
import requests

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(os.path.dirname(HERE), "data")
CACHE = os.path.join(DATA, "backtest_cache.json")
DS = "https://api.dexscreener.com/latest/dex/tokens/"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{}/ohlcv/hour?aggregate=1&limit=1000&currency=usd&token=base"
CALLS = {"dex": 0, "gecko": 0}

def ts(s): return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
def load(p, d):
    try: return json.load(open(p))
    except Exception: return d

def classify(fdv, peak, now):
    if not fdv: return None
    if peak >= 2 * fdv or now >= 1.5 * fdv: return "win"
    if now <= 0.3 * fdv: return "rug"
    return "flat"

def first_checks(rows, verdict, reason, only, tag):
    """{mint: first matching row}. reason/only are regexes on the reasons list; tag matches metrics.warn_tags."""
    out = {}
    for r in rows:
        m = r.get("mint")
        if not m or m in out: continue
        if verdict and r.get("verdict") != verdict: continue
        rs = r.get("reasons") or []
        if reason and not any(re.search(reason, s, re.I) for s in rs): continue
        if only and not all(re.search(only, s, re.I) for s in rs): continue
        if tag and tag not in ((r.get("metrics") or {}).get("warn_tags") or []): continue
        out[m] = r
    return out

def dex_now(mints, cache, max_age):
    now_t = time.time(); need = [m for m in mints if now_t - (cache["now"].get(m) or {}).get("t", 0) > max_age * 3600]
    for i in range(0, len(need), 30):
        CALLS["dex"] += 1; got = {}
        try:
            for q in (requests.get(DS + ",".join(need[i:i + 30]), timeout=30).json() or {}).get("pairs") or []:
                if q.get("chainId") != "solana": continue
                m = (q.get("baseToken") or {}).get("address"); l = (q.get("liquidity") or {}).get("usd") or 0
                if m and (m not in got or l > got[m]["liq"]):
                    got[m] = {"fdv": q.get("fdv") or q.get("marketCap") or 0, "liq": l, "pair": q.get("pairAddress"),
                              "price": float(q.get("priceUsd") or 0), "created": (q.get("pairCreatedAt") or 0) / 1000,
                              "sym": (q.get("baseToken") or {}).get("symbol")}
        except Exception: continue
        for m in need[i:i + 30]: cache["now"][m] = dict(got.get(m) or {"fdv": 0, "pair": None}, t=now_t)
        time.sleep(0.25)

def gecko_peak(m, since, cache):
    c = cache["peak"].get(m)
    if c is not None: return c
    n = cache["now"].get(m) or {}
    if not n.get("pair") or not n.get("price") or not n.get("fdv"): return None
    CALLS["gecko"] += 1
    try:
        ol = requests.get(GT.format(n["pair"]), timeout=30).json()["data"]["attributes"]["ohlcv_list"]
        hi = max([x[2] for x in ol if x[0] >= since] or [0])
        cache["peak"][m] = v = hi * n["fdv"] / n["price"]
    except Exception: v = None
    time.sleep(2.1)
    return v

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdict", default="KILL"); ap.add_argument("--reason"); ap.add_argument("--only"); ap.add_argument("--tag")
    ap.add_argument("--where"); ap.add_argument("--gecko", type=int, default=40); ap.add_argument("--max-age-h", type=float, default=6)
    ap.add_argument("--examples", type=int, default=8)
    a = ap.parse_args(argv)
    rows = sorted((json.loads(x) for x in open(os.path.join(DATA, "checks_log.jsonl")) if x.strip()), key=lambda r: r["timestamp"])
    outc = load(os.path.join(DATA, "outcomes.json"), {}).get("tokens", {})
    cache = load(CACHE, {}); cache.setdefault("now", {}); cache.setdefault("peak", {})
    first = first_checks(rows, a.verdict, a.reason, a.only, a.tag)
    dex_now(list(first), cache, a.max_age_h)
    recs, gecko_left = [], a.gecko
    for m, r in first.items():
        met = r.get("metrics") or {}; fdv = met.get("fdv") or 0
        if fdv <= 0: continue
        t = ts(r["timestamp"]); n = cache["now"].get(m) or {}
        snaps = [s[2] for s in (outc.get(m) or {}).get("snapshots") or [] if s[0] >= t and s[2]]
        peak = max(snaps + [fdv, n.get("fdv") or 0])
        if not snaps:
            gp = cache["peak"].get(m)
            if gp is None and gecko_left > 0 and n.get("pair"): gecko_left -= 1; gp = gecko_peak(m, t, cache)
            if gp: peak = max(peak, gp)
        age_min = met.get("pair_age_min")
        age_d = age_min / 1440 if age_min is not None else ((t - n["created"]) / 86400 if n.get("created") else None)
        rec = {"mint": m, "sym": n.get("sym") or (outc.get(m) or {}).get("symbol") or m[:6], "fdv": fdv,
               "liq": met.get("liquidity_usd") or 0, "age_d": age_d, "peak_x": peak / fdv, "now_x": (n.get("fdv") or 0) / fdv,
               "peak_known": bool(snaps) or cache["peak"].get(m) is not None}
        rec["cls"] = classify(fdv, peak, n.get("fdv") or 0); recs.append(rec)
    json.dump(cache, open(CACHE, "w"))
    def row(name, xs):
        if not xs: return f"| {name} | 0 | - | - | - | - |"
        c = {k: sum(1 for x in xs if x["cls"] == k) for k in ("win", "rug", "flat")}
        return f"| {name} | {len(xs)} | {c['win']} | {c['rug']} | {c['flat']} | {100 * c['win'] / len(xs):.0f}% |"
    print(f"Filter: verdict={a.verdict} reason={a.reason} only={a.only} tag={a.tag} where={a.where}")
    print("| Bucket | n | winners | rugs | flat | win rate |\n|---|---|---|---|---|---|")
    print(row("all", recs)); print(row("peak known", [x for x in recs if x["peak_known"]]))
    print(row("FDV < $1M", [x for x in recs if x["fdv"] < 1e6])); print(row("FDV >= $1M", [x for x in recs if x["fdv"] >= 1e6]))
    print(row("age < 3d", [x for x in recs if (x["age_d"] or 0) < 3])); print(row("age >= 3d", [x for x in recs if (x["age_d"] or 0) >= 3]))
    if a.where:
        f = lambda x: eval(a.where, {"__builtins__": {}}, {k: (x[k] or 0) for k in ("fdv", "liq", "age_d", "peak_x", "now_x")})
        sel = [x for x in recs if f(x)]; print(row(f"where {a.where}", sel))
        for x in sel: print(f"  {x['sym']:<10} {x['cls']:<4} first ${x['fdv']/1e3:,.0f}K peak {x['peak_x']:.1f}x now {x['now_x']:.2f}x")
    for x in sorted((x for x in recs if x["cls"] == "win"), key=lambda x: -x["peak_x"])[:a.examples]:
        print(f"  win: {x['sym']:<10} first ${x['fdv']/1e3:,.0f}K peak {x['peak_x']:.1f}x now {x['now_x']:.2f}x")
    print(f"API calls: DexScreener {CALLS['dex']}, GeckoTerminal {CALLS['gecko']} (cached in data/backtest_cache.json), Helius 0")
    return recs

if __name__ == "__main__":
    main()
