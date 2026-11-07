"""r10.7: build data/smart_wallets.json from our own outcomes (one-off / weekly; ~20 Helius credits per new mint, cached).

Winners = tracked tokens whose price hit >= 2x the check price before -50% (outcomes.json snapshots).
Rugs    = tracked tokens that hit -50% first and ended RUGGED / DEAD.
Per token: early buyers (first 100 txs from create + 60s, wallet_intel.mint_early) + stored top traders from the
check (metrics.top_traders_detail): on winners only the profit takers (sold more than bought in the window) count.
Smart wallet = early / profitable in >= MIN_WINS winners, in fewer than 1 rug per 2 wins (default: zero rugs when
it has 2-3 wins), not a bot (seen early in >= BOT_SHARE of all sampled tokens), not CEX / program / pool / creator /
any funder flagged as a cluster funder.

  python scanner/build_smart_wallets.py [--max-new N] [--dry]
"""
import json, os, sys, time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wallet_intel as wi

MIN_WINS = 2
BOT_SHARE = 0.25
LINK_MIN = 15           # two tokens sharing >= 15 non-bot wallets are one operation (e.g. MUNK/BLEP: 36): count once
RUG_SAMPLE = 70          # most recent kill-control rugs sampled (all PASS rugs always included)

def first_hit(t):
    e = t.get("entry_price")
    if not e: return None
    for s in t.get("snapshots") or []:
        p = s[1] if len(s) > 1 else None
        if not p: continue
        if p >= 2 * e: return "WIN"
        if p <= 0.5 * e: return "LOSS"
    return "OPEN"

def label(t):
    h = first_hit(t)
    if h == "WIN": return "win"
    if h == "LOSS" and t.get("status") in ("RUGGED", "DEAD"): return "rug"
    return None

def pick_tokens(outcomes):
    T = outcomes.get("tokens") or {}
    wins = {m: t for m, t in T.items() if label(t) == "win"}
    rugs_pass = {m: t for m, t in T.items() if label(t) == "rug" and t.get("group") != "kill_control"}
    rugs_kc = sorted(((m, t) for m, t in T.items() if label(t) == "rug" and t.get("group") == "kill_control"),
                     key=lambda x: x[1].get("t0") or "", reverse=True)[:RUG_SAMPLE]
    rugs = dict(rugs_pass); rugs.update(dict(rugs_kc))
    return wins, rugs

def top_detail_by_mint(log_path):
    out = {}
    for l in open(log_path):
        try: r = json.loads(l)
        except Exception: continue
        d = (r.get("metrics") or {}).get("top_traders_detail")
        if d and r["mint"] not in out: out[r["mint"]] = d
    return out

def link_groups(sets, bots=(), link_min=LINK_MIN):
    """Pure. {mint: group id}: tokens sharing >= link_min non-bot wallets are merged (same operation)."""
    ms = list(sets); parent = {m: m for m in ms}
    def find(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    clean = {m: set(sets[m]) - set(bots) for m in ms}
    for i, a in enumerate(ms):
        for b in ms[i + 1:]:
            if len(clean[a] & clean[b]) >= link_min: parent[find(a)] = find(b)
    return {m: find(m) for m in ms}

def score(sets, labels, bots=(), groups=None):
    """Pure. sets: {mint: set(wallets)}; labels: {mint: 'win'|'rug'}. -> {wallet: {'wins': [...], 'rugs': [...], 'win_groups': n}}"""
    S = defaultdict(lambda: {"wins": [], "rugs": [], "_g": set()})
    for m, ws in sets.items():
        lb = labels.get(m)
        if lb not in ("win", "rug"): continue
        for w in ws:
            if w in bots: continue
            S[w]["wins" if lb == "win" else "rugs"].append(m)
            if lb == "win": S[w]["_g"].add((groups or {}).get(m, m))
    out = {}
    for w, s in S.items():
        out[w] = {"wins": s["wins"], "rugs": s["rugs"], "win_groups": len(s["_g"])}
    return out

def is_smart(s, min_wins=MIN_WINS):
    w, r = s.get("win_groups", len(s["wins"])), len(s["rugs"])
    return w >= min_wins and (r == 0 if w < 4 else r * 2 < w)

def bot_set(sets, share=BOT_SHARE):
    cnt = defaultdict(int)
    for ws in sets.values():
        for w in ws: cnt[w] += 1
    n = max(1, len(sets))
    return {w for w, c in cnt.items() if c >= max(4, share * n)}

def main():
    dry = "--dry" in sys.argv
    max_new = int(sys.argv[sys.argv.index("--max-new") + 1]) if "--max-new" in sys.argv else 400
    O = json.load(open(os.path.join(wi.DATA_DIR, "outcomes.json")))
    wins, rugs = pick_tokens(O)
    labels = {**{m: "rug" for m in rugs}, **{m: "win" for m in wins}}
    syms = {m: (O["tokens"][m].get("symbol") or m[:6]) for m in labels}
    detail = top_detail_by_mint(os.path.join(wi.DATA_DIR, "checks_log.jsonl"))
    cache = wi._load_cache(); B = wi.Budget(cap=10 ** 9); sets, creators, new, failed = {}, set(), 0, 0
    for m in labels:
        if "mint:" + m not in cache:
            if new >= max_new: continue
            new += 1
        me = wi.mint_early(m, B, cache)
        if me.get("error"): failed += 1
        cr = (me.get("launch") or {}).get("creator")
        if cr: creators.add(cr)
        ws = set(me.get("early") or [])
        for r in detail.get(m) or []:
            # winners: profit takers (net buy-sell < 0); rugs: every top trader counts against it
            if labels[m] == "rug" or (r.get("net") or 0) < 0: ws.add(r["w"])
        sets[m] = ws
        if new and new % 20 == 0: wi._save_cache(cache)
    wi._save_cache(cache)
    bots = bot_set(sets) | creators | set(wi.CEX_WALLETS)
    groups = link_groups(sets, bots)
    S = score(sets, labels, bots, groups)
    smart = {w: s for w, s in S.items() if is_smart(s)}
    print(f"tokens: {len(wins)} winners, {len(rugs)} rugs; sampled {len(sets)} (new {new}, failed {failed}); "
          f"Helius ~{B.credits} credits; bots excluded {len(bots)}; smart wallets {len(smart)}")
    out = {"built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rules": "r10.7",
           "definition": f">= {MIN_WINS} independent winners (2x before -50%) as early buyer (first 100 txs from create+60s) or "
                         "profit-taking top trader; 0 rugs (or rugs < wins/2 with 4+ wins); bots/CEX/creators excluded",
           "winners": len(wins), "rugs": len(rugs), "sampled": len(sets),
           "wallets": sorted(({"wallet": w, "wins": [syms[m] for m in s["wins"]], "win_mints": s["wins"],
                               "rugs": [syms[m] for m in s["rugs"]], "independent_wins": s["win_groups"]} for w, s in smart.items()),
                             key=lambda x: (-x["independent_wins"], -len(x["wins"]), len(x["rugs"]), x["wallet"]))}
    if not dry:
        json.dump(out, open(wi.SMART_PATH, "w"), indent=1)
        json.dump({"sets": {m: sorted(v) for m, v in sets.items()}, "labels": labels, "bots": sorted(bots)},
                  open(os.path.join(wi.DATA_DIR, "smart_build_cache.json"), "w"))
    for x in out["wallets"][:15]: print(f"  {x['wallet']}  wins {x['wins']}  rugs {x['rugs']}")
    return out

if __name__ == "__main__":
    main()
