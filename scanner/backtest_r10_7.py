"""r10.7 backtest: do smart money (>0 known winner wallets) and bad dev history separate winners from rugs?
Smart money is scored leave-one-out (the token itself is removed before building the smart set), so it is not
in-sample. Dev history uses only launches made BEFORE the token; dev 'sold' is today's balance (look-ahead, shown
for reference only). Needs data/smart_build_cache.json from build_smart_wallets.py.
  python scanner/backtest_r10_7.py [--dev]     # --dev also fetches dev history for tracked PASS tokens (~21 credits each)
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wallet_intel as wi
import build_smart_wallets as bs

def loo_counts(sets, labels, bots):
    groups = bs.link_groups(sets, bots)
    full = bs.score(sets, labels, bots, groups)
    out = {}
    for m in sets:
        cnt = 0
        for w in sets[m]:
            s = full.get(w)
            if not s: continue
            s2 = {"wins": [x for x in s["wins"] if x != m], "rugs": [x for x in s["rugs"] if x != m]}
            s2["win_groups"] = len({groups[x] for x in s2["wins"]})
            if bs.is_smart(s2): cnt += 1
        out[m] = cnt
    return out

def row(name, xs, labels):
    w = sum(1 for m in xs if labels[m] == "win"); r = sum(1 for m in xs if labels[m] == "rug")
    return f"| {name} | {len(xs)} | {w} | {r} | {100 * w / len(xs):.0f}% |" if xs else f"| {name} | 0 | 0 | 0 | - |"

def main():
    C = json.load(open(os.path.join(wi.DATA_DIR, "smart_build_cache.json")))
    O = json.load(open(os.path.join(wi.DATA_DIR, "outcomes.json")))["tokens"]
    sets = {m: set(v) for m, v in C["sets"].items()}; labels = C["labels"]; bots = set(C["bots"])
    sm = loo_counts(sets, labels, bots)
    def r10(m): return str(O[m].get("rules_version") or "").startswith("r10") and O[m].get("group") == "pass"
    print("| Bucket | n | winners | rugs | win rate |\n|---|---|---|---|---|")
    for scope, ms in (("all tracked", list(sets)), ("r10+ PASS", [m for m in sets if r10(m)])):
        print(row(f"{scope}: all", ms, labels))
        print(row(f"{scope}: smart money > 0", [m for m in ms if sm[m] > 0], labels))
        print(row(f"{scope}: smart money = 0", [m for m in ms if sm[m] == 0], labels))
        print(row(f"{scope}: smart money >= 2", [m for m in ms if sm[m] >= 2], labels))
    if "--dev" in sys.argv:
        cache = wi._load_cache(); B = wi.Budget(cap=10 ** 9); D = {}
        pm = [m for m in sets if O[m].get("group") in ("pass", "pass_v1_loose")]
        for m in pm:
            me = wi.mint_early(m, B, cache)
            D[m] = wi.dev_history(m, B, launch=me.get("launch"), cache=cache)
        wi._save_cache(cache)
        print(f"\nDev history on {len(pm)} tracked PASS tokens (Helius ~{B.credits} credits)")
        print("| Bucket | n | winners | rugs | win rate |\n|---|---|---|---|---|")
        for scope, ms in (("PASS all", pm), ("r10+ PASS", [m for m in pm if r10(m)])):
            print(row(f"{scope}: all", ms, labels))
            print(row(f"{scope}: dev bad (>=3 prior, >=75% died)", [m for m in ms if (D[m].get('prior') or 0) >= 3 and D[m]['died'] / D[m]['prior'] >= .75], labels))
            print(row(f"{scope}: dev has >=1 prior launch", [m for m in ms if (D[m].get('prior') or 0) >= 1], labels))
            print(row(f"{scope}: dev first launch", [m for m in ms if D[m].get('prior') == 0], labels))
            print(row(f"{scope}: not pump.fun", [m for m in ms if D[m].get('non_pump')], labels))
            print(row(f"{scope}: dev sold 100% (today, look-ahead)", [m for m in ms if (D[m].get('sold_pct') or 0) >= 99.5], labels))
        for m in pm:
            print(f"  {O[m]['symbol'][:10]:<10} {labels[m]:<4} sm={sm[m]} {D[m].get('line')}")
        json.dump({m: {k: v for k, v in D[m].items()} for m in pm}, open("/tmp/r10_7_dev_backtest.json", "w"), default=str)

if __name__ == "__main__":
    main()
