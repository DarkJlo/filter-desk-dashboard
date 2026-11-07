"""r10.7 wallet intel (ANNOTATION ONLY, never changes a verdict or BUY/SKIP):
  1. Smart money: wallets that were early buyers / profit-taking top traders in >= 2 past winners and in no
     past rug (data/smart_wallets.json, built by scanner/build_smart_wallets.py). On a PASS we count how many
     of them appear among the token's top traders and early buyers.
  2. Dev history: the creator wallet (fee payer of the mint's first tx = pump.fun create), how many other
     pump.fun tokens it launched (from its last/first 100 txs) and how they ended (now < $30K FDV / no pair =
     died), plus whether the dev still holds its launch buy.

Sources: Helius getTransactionsForAddress (full, <= 100 tx = 10 credits/call), one getMultipleAccounts (1 credit),
DexScreener tokens (keyless). pump.fun's frontend API is Cloudflare-blocked from servers, so it is not used.
Budget per PASS: ~31 Helius credits (hard cap WI_MAX_CREDITS). Per-mint results cached in data/wallet_intel_cache.json.
Best-effort: every failure returns an "error" field; callers never change a verdict because of this module.
The Helius key comes from env HELIUS_API_KEY and is never printed or logged.
"""
import json, os, time

import requests

try:
    import top_traders as _tt
    from top_traders import ata, find_pda, b58decode, TOKEN_PROGRAM, TOKEN_2022, PROGRAM_ADDRS
except Exception:  # pragma: no cover
    _tt = None
    PROGRAM_ADDRS = set()

try:
    from cex_wallets import CEX_WALLETS
except Exception:  # pragma: no cover
    CEX_WALLETS = {}

HELIUS_URL = "https://mainnet.helius-rpc.com/"
DS_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{mints}"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FD_DATA_DIR") or os.path.join(os.path.dirname(_HERE), "data")
CACHE_PATH = os.path.join(DATA_DIR, "wallet_intel_cache.json")   # gitignored (data/*cache*.json), Actions cache
SMART_PATH = os.path.join(DATA_DIR, "smart_wallets.json")

EARLY_SKIP_SECS = 60       # skip the launch-second sniper/bundle txs: "early" = first 100 txs from create + 60s
EARLY_LIMIT = 100          # 100 full txs = 10 credits
DEV_TX_LIMIT = 100         # creator history: newest 100 + oldest 100 = 20 credits
DIED_FDV = 30_000          # prior launch now below this (or no pair) counts as died
WI_MAX_CREDITS = int(os.environ.get("WI_MAX_CREDITS") or 60)
GTFA_CREDITS, RPC_CREDITS = 10, 1
SERIAL_MIN, SERIAL_DIED_SHARE = 3, 0.75   # serial launcher: >= 3 earlier launches, >= 75% now dead
# r10.7.1 dev tier from each prior launch's PEAK FDV (GeckoTerminal OHLCV, keyless, no Helius credits)
TIER_MIN = 3               # need >= 3 prior launches with a known peak to call good/bad
RAN_FDV = 300_000          # a prior launch "ran" if it ever peaked >= $300K
FLOP_FDV = 60_000          # a prior launch "flopped" if it never peaked above $60K (most never left the curve)
GOOD_SHARE, BAD_SHARE = 0.5, 0.75
PEAK_MAX_MINTS = 10        # newest prior launches looked up per dev (2 GeckoTerminal calls each)
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
GOOD_DEVS_PATH = os.path.join(DATA_DIR, "good_devs.json")

# ---------------- Helius (credit-counted, key never printed) ----------------
class Budget:
    def __init__(self, cap=None): self.credits, self.cap = 0, cap or WI_MAX_CREDITS
    def can(self, cost): return self.credits + cost <= self.cap

def _rpc(method, params, cost, B):
    key = os.environ.get("HELIUS_API_KEY")
    if not key or not B.can(cost): return None
    for t in range(4):
        try:
            r = requests.post(HELIUS_URL, params={"api-key": key}, timeout=30,
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if r.status_code == 429:
                time.sleep(1.0 + t); continue
            j = r.json()
            if "result" in j:
                B.credits += cost; return j["result"]
            return None   # RPC error (failed responses are free)
        except Exception:
            pass  # never print the exception: it contains the URL with the key
        time.sleep(0.5 * (t + 1))
    return None

def gtfa(addr, B, order="asc", limit=100, gte=None):
    opts = {"sortOrder": order, "limit": limit, "transactionDetails": "full", "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1}
    if gte: opts["filters"] = {"blockTime": {"gte": int(gte)}}
    cost = GTFA_CREDITS * max(1, -(-limit // 100))
    r = _rpc("getTransactionsForAddress", [addr, opts], cost, B)
    return None if r is None else (r.get("data") or [])

# ---------------- pure parsers (unit-tested) ----------------
def _keys(tx):
    msg = ((tx.get("transaction") or {}).get("message")) or {}
    return [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]

def _amt(b):
    try: return float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    except Exception: return 0.0

def token_deltas(tx, mint):
    """{owner: post - pre} UI token amount change of `mint` per owner in one tx."""
    meta = tx.get("meta") or {}
    d = {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("mint") == mint and b.get("owner"): d[b["owner"]] = d.get(b["owner"], 0.0) - _amt(b)
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") == mint and b.get("owner"): d[b["owner"]] = d.get(b["owner"], 0.0) + _amt(b)
    return d

def is_pump_create(tx):
    logs = (tx.get("meta") or {}).get("logMessages") or []
    return any(l in ("Program log: Instruction: Create", "Program log: Instruction: CreateV2") for l in logs) and \
        PUMP_PROGRAM in _keys(tx)

def created_mint(tx):
    """Mint created by a pump.fun create tx (first account of the pump create instruction), else None."""
    if not is_pump_create(tx) or (tx.get("meta") or {}).get("err"): return None
    msg = ((tx.get("transaction") or {}).get("message")) or {}
    for ins in msg.get("instructions") or []:
        if isinstance(ins, dict) and ins.get("programId") == PUMP_PROGRAM and ins.get("accounts"):
            a0 = ins["accounts"][0]
            if a0 and a0 not in PROGRAM_ADDRS: return a0
    return None

def launch_info(txs, mint):
    """From the mint's oldest txs: creator (create tx fee payer), create time, dev launch buy, token program."""
    for tx in txs or []:
        if (tx.get("meta") or {}).get("err"): continue
        keys = _keys(tx)
        if not keys: continue
        payer = keys[0]
        prog = None
        for b in (tx.get("meta") or {}).get("postTokenBalances") or []:
            if b.get("mint") == mint: prog = b.get("programId") or prog
        dev_buy = max(0.0, token_deltas(tx, mint).get(payer, 0.0))
        return {"creator": payer, "create_bt": tx.get("blockTime"), "dev_buy": dev_buy,
                "pump": is_pump_create(tx), "token_program": prog}
    return None

def early_buyers(txs, mint, exclude=()):
    """Ordered unique wallets whose balance of `mint` went up in successful txs (buys), minus excluded addresses."""
    ex = set(exclude) | PROGRAM_ADDRS | {mint}
    out, seen = [], set()
    for tx in txs or []:
        if (tx.get("meta") or {}).get("err"): continue
        for o, dv in token_deltas(tx, mint).items():
            if dv > 0 and o not in ex and o not in seen and o not in CEX_WALLETS:
                seen.add(o); out.append(o)
    return out

def dev_launch_mints(txs, creator, skip_mint=None):
    """[[mint, blockTime]] this creator launched on pump.fun (it paid the create tx), pages merged, unique."""
    out, seen = [], set()
    for tx in txs or []:
        ks = _keys(tx)
        if not ks or ks[0] != creator: continue
        m = created_mint(tx)
        if m and m != skip_mint and m not in seen: seen.add(m); out.append([m, tx.get("blockTime") or 0])
    return out

# ---------------- smart wallets ----------------
def load_smart(path=None):
    try:
        j = json.load(open(path or SMART_PATH))
        return {w["wallet"]: w for w in j.get("wallets") or []}
    except Exception:
        return {}

def smart_money(top_wallets, early, smart):
    """Pure. top_wallets: [{'w' or 'wallet', 'hold'/'holding'}...]; early: [wallet]; smart: {wallet: info}.
    Returns {'count', 'in_top', 'in_early', 'holding', 'wallets', 'line'}."""
    top = {}
    for r in top_wallets or []:
        w = r.get("w") or r.get("wallet")
        if w: top[w] = r.get("hold", r.get("holding"))
    in_top = [w for w in top if w in smart]
    in_early = [w for w in (early or []) if w in smart]
    allw = list(dict.fromkeys(in_top + in_early))
    holding = sum(1 for w in in_top if top.get(w))
    res = {"count": len(allw), "in_top": len(in_top), "in_early": len(in_early), "holding": holding,
           "wallets": [{"w": w, "wins": (smart[w].get("wins") or [])[:6]} for w in allw[:10]]}
    res["line"] = smart_line(res, n_smart=len(smart))
    return res

def smart_line(r, n_smart=None):
    if not r: return ""
    if r.get("error") and not r.get("count"): return f"Smart money: n/a ({r['error']})"
    k = r.get("count") or 0
    if not k: return "Smart money: none of our known winners' wallets" + (f" ({n_smart} tracked)" if n_smart else "")
    parts = []
    if r.get("in_early"): parts.append(f"{r['in_early']} early")
    if r.get("in_top"): parts.append(f"{r['in_top']} in top traders" + (f", {r['holding']} still holding" if r.get("in_top") else ""))
    return f"Smart money: {k} known winner wallet{'s' if k != 1 else ''} ({'; '.join(parts)})"

# ---------------- dev history ----------------
def dev_tier(peaks):
    """Pure. peaks: [peak_fdv or None] for prior launches. 'good' when >= TIER_MIN known and >= 50% ran to $300K+;
    'bad' when >= TIER_MIN known and >= 75% never topped $60K (backtest: 5/5 such PASSes rugged, max prior peak $85K);
    'neutral' for first launches / mixed; 'unknown' when peaks could not be read."""
    if peaks is None: return "unknown"
    known = [p for p in peaks if p is not None]
    if not peaks: return "neutral"
    if len(known) < min(TIER_MIN, len(peaks)) or (len(known) < len(peaks) / 2): return "unknown"
    if len(known) < TIER_MIN: return "neutral"
    ran = sum(1 for p in known if p >= RAN_FDV); flop = sum(1 for p in known if p < FLOP_FDV)
    if ran / len(known) >= GOOD_SHARE: return "good"
    if flop / len(known) >= BAD_SHARE: return "bad"
    return "neutral"

def _median(xs):
    xs = sorted(xs); n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if xs else 0

def _k(v): return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K"

def dev_verdict(d):
    """Pure. 'serial' when the dev launched >= SERIAL_MIN tokens before this one and >= SERIAL_DIED_SHARE of them died
    (backtest: 5/5 such PASSes rugged); 'unknown' without history; else 'ok'. Dev selling its launch buy is NOT
    used: ~80% of devs sell 100% (winners WEALTH, inu, DESSA, RAMBO all did)."""
    if not d or d.get("prior") is None: return "unknown"
    prior, died = d.get("prior") or 0, d.get("died") or 0
    if prior >= SERIAL_MIN and died / prior >= SERIAL_DIED_SHARE: return "serial"
    return "ok"

def dev_line(d):
    if not d: return ""
    if d.get("creator") is None: return "Dev: n/a" + (f" ({d['error']})" if d.get("error") else "")
    if d.get("non_pump") and not d.get("prior"): s = "Dev: not a pump.fun launch (launch history n/a)"
    elif d.get("prior") is None: s = "Dev: launch history n/a"
    elif d["prior"] == 0: s = "Dev: first pump.fun launch seen"
    else:
        s = f"Dev: {d['prior']} prior launch{'es' if d['prior'] != 1 else ''}"
        pk = [p for p in (d.get("peaks") or []) if p is not None]
        if pk:
            ran = sum(1 for p in pk if p >= RAN_FDV)
            s += f" ({len(pk)} checked: {ran} ran to $300K+, median peak {_k(_median(pk))}, best {_k(max(pk))}"
            if d.get("peak_hours_med") is not None: s += f", runners peaked ~{d['peak_hours_med']:.0f}h after launch"
            s += ")"
        else:
            s += f", {d.get('died', 0)} died <$30K"
            if d.get("survived"): s += f", {d['survived']} still alive ({d.get('best_sym') or 'best'} ${d.get('best_fdv', 0) / 1e3:,.0f}K)"
        if d.get("capped"): s += " (100+ txs, partial)"
    if d.get("non_pump"): pass
    elif not d.get("dev_buy"): s += ", no dev buy at launch"
    elif d.get("sold_pct") is not None:
        sp = d["sold_pct"]
        s += ", dev sold 100% of launch buy" if sp >= 99.5 else (", dev still holds its launch buy" if sp <= 0.5 else f", dev sold {sp:.0f}% of launch buy")
    t = d.get("tier")
    if t == "good": s = "\u2705 " + s + " (GOOD DEV: past launches ran; still use the exit plan)"
    elif t == "bad": s = "\u26d4 " + s + " (BAD DEV: past launches never got going; 5/5 past PASSes like this rugged)"
    elif t is None and dev_verdict(d) == "serial": s = "\u26a0\ufe0f " + s + " (serial launcher: 5/5 past PASSes like this rugged)"
    return s

def gt_peak(mint, cache=None):
    """(peak_fdv, hours_from_pool_create_to_peak) from GeckoTerminal 4h candles of the mint's top pool.
    (0, None) when it has no pool (never graduated = flop); (None, None) on errors. Cached per mint."""
    ck = "peak:" + mint
    if cache is not None and ck in cache: return tuple(cache[ck])
    res = (None, None)
    for t in range(3):
        try:
            r = requests.get(f"{GT}/tokens/{mint}/pools?page=1", timeout=20)
            if r.status_code == 429: time.sleep(15 + 5 * t); continue
            if r.status_code == 404: res = (0, None); break
            pools = (r.json() or {}).get("data") or []
            if not pools: res = (0, None); break
            a = pools[0]["attributes"]; time.sleep(2.1)
            r2 = requests.get(f"{GT}/pools/{a['address']}/ohlcv/hour?aggregate=4&limit=1000&currency=usd&token=base", timeout=20)
            if r2.status_code == 429: time.sleep(15 + 5 * t); continue
            ol = ((r2.json() or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
            if not ol: res = (0, None); break
            hi = max(ol, key=lambda c: c[2]); hrs = None
            try:
                from datetime import datetime
                c0 = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp()
                hrs = max(0.0, (hi[0] - c0) / 3600)
            except Exception: pass
            res = (float(hi[2]) * 1e9, hrs); break   # pump.fun supply = 1B
        except Exception:
            time.sleep(2)
    if cache is not None and res[0] is not None: cache[ck] = list(res)
    time.sleep(2.1)
    return res

def record_good_dev(d, mint, path=None):
    """Append a GOOD-tier creator to data/good_devs.json (deduped). Best-effort."""
    path = path or GOOD_DEVS_PATH
    try:
        try: j = json.load(open(path))
        except Exception: j = {"note": "r10.7.1 creators whose prior launches mostly ran to $300K+", "devs": {}}
        e = j["devs"].setdefault(d["creator"], {"seen_on": []})
        e.update({"prior": d.get("prior"), "peaks": [round(p) for p in d.get("peaks") or [] if p is not None],
                  "updated": int(time.time())})
        if mint not in e["seen_on"]: e["seen_on"].append(mint)
        json.dump(j, open(path, "w"), indent=1)
    except Exception:
        pass

# ---------------- network (cached) ----------------
def _load_cache():
    try: return json.load(open(CACHE_PATH))
    except Exception: return {}

def _save_cache(c):
    try:
        os.makedirs(DATA_DIR, exist_ok=True); json.dump(c, open(CACHE_PATH, "w"))
    except Exception:
        pass

def mint_early(mint, B, cache=None):
    """{'launch': launch_info, 'early': [wallets]} from the mint's oldest tx + 100 txs from create+60s. Cached."""
    cache = cache if cache is not None else _load_cache()
    ck = "mint:" + mint
    if ck in cache: return cache[ck]
    first = gtfa(mint, B, "asc", 1)
    # limit 1 still bills the 10-credit minimum; fine.
    if not first: return {"error": "mint history unavailable"}
    li = launch_info(first, mint) or {}
    early = []
    if li.get("create_bt"):
        txs = gtfa(mint, B, "asc", EARLY_LIMIT, gte=li["create_bt"] + EARLY_SKIP_SECS)
        if txs is not None: early = early_buyers(txs, mint, exclude={li.get("creator")})
    res = {"launch": li, "early": early}
    if early: cache[ck] = res
    return res

def ds_fdv(mints):
    """{mint: (fdv, liquidity, symbol)} best-liquidity Solana pair per mint (30 per request)."""
    out = {}
    for i in range(0, len(mints), 30):
        try:
            r = requests.get(DS_TOKENS.format(mints=",".join(mints[i:i + 30])), timeout=20)
            for p in (r.json() or {}).get("pairs") or []:
                if p.get("chainId") != "solana": continue
                m = (p.get("baseToken") or {}).get("address")
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if m and (m not in out or liq > out[m][1]):
                    out[m] = (p.get("fdv") or p.get("marketCap") or 0, liq, (p.get("baseToken") or {}).get("symbol"))
        except Exception:
            pass
        time.sleep(0.3)
    return out

def dev_history(mint, B, launch=None, cache=None):
    cache = cache if cache is not None else _load_cache()
    d = {"creator": None, "prior": None, "died": None, "survived": None, "dev_buy": None, "sold_pct": None}
    if launch is None:
        first = gtfa(mint, B, "asc", 1)
        launch = launch_info(first, mint) if first else None
    if not launch or not launch.get("creator"):
        d["error"] = "creator unknown"; d["verdict"] = "unknown"; d["tier"] = "unknown"; d["line"] = dev_line(d); return d
    cr = d["creator"] = launch["creator"]; d["dev_buy"] = launch.get("dev_buy") or 0
    ck = "dev:" + cr
    mints = (cache.get(ck) or {}).get("mints")
    if mints is None:
        new = gtfa(cr, B, "desc", DEV_TX_LIMIT)
        old = gtfa(cr, B, "asc", DEV_TX_LIMIT) if new is not None and len(new) >= DEV_TX_LIMIT else []
        if new is None: d["error"] = "creator history unavailable"
        else:
            mints = dev_launch_mints(list(old or []) + list(new), cr, skip_mint=mint)
            d["capped"] = len(new) >= DEV_TX_LIMIT
            cache[ck] = {"mints": mints, "capped": d["capped"], "t": int(time.time())}
    else:
        d["capped"] = cache[ck].get("capped")
    if mints is not None:
        cbt = launch.get("create_bt") or 0
        mints = [m for m, bt in mints if m != mint and (not cbt or not bt or bt < cbt)]   # launched BEFORE this one
        fd = ds_fdv(mints) if mints else {}
        alive = [(m, fd[m]) for m in mints if m in fd and (fd[m][0] or 0) >= DIED_FDV]
        d["prior"], d["survived"] = len(mints), len(alive)
        d["died"] = d["prior"] - d["survived"]
        if alive:
            bm, (bf, _, bs) = max(alive, key=lambda x: x[1][0])
            d["best_fdv"], d["best_sym"] = bf, bs
        d["prior_mints"] = mints[:20]
        if os.environ.get("FD_DEV_PEAKS", "1") != "0":
            recent = mints[:PEAK_MAX_MINTS]
            pk = [gt_peak(m, cache) for m in recent]
            d["peaks"] = [p for p, _ in pk]
            hrs = [h for p, h in pk if p and p >= RAN_FDV and h is not None]
            d["peak_hours_med"] = _median(hrs) if hrs else None
            d["tier"] = dev_tier(d["peaks"])
            if d["tier"] == "good": record_good_dev(d, mint)
    # dev still holding? one batched balance call on its ATAs (Token + Token-2022)
    if not launch.get("pump"): d["dev_buy"] = None; d["non_pump"] = True   # non-pump launch: creator minted supply
    if d["dev_buy"] and _tt is not None:
        res = _rpc("getMultipleAccounts", [[ata(cr, mint, TOKEN_PROGRAM), ata(cr, mint, TOKEN_2022)],
                                           {"encoding": "jsonParsed"}], RPC_CREDITS, B)
        if res is not None:
            bal = 0.0
            for v in res.get("value") or []:
                try: bal += float(v["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                except Exception: pass
            d["dev_balance"] = bal
            d["sold_pct"] = round(max(0.0, min(100.0, 100 * (1 - bal / d["dev_buy"]))), 1)
    if d.get("prior") == 0: d["tier"] = "neutral"
    elif d.get("prior") is None: d.setdefault("tier", "unknown")
    d["verdict"] = dev_verdict(d)
    d["line"] = dev_line(d)
    return d

def check(mint, top_detail=None):
    """Both annotations for one PASS. Returns (smart_money dict, dev_history dict). Never raises."""
    B = Budget(); cache = _load_cache()
    sm, dv = {"count": 0}, {"creator": None}
    try:
        me = mint_early(mint, B, cache)
        smart = load_smart()
        sm = smart_money(top_detail, me.get("early"), smart) if smart else {"count": 0, "error": "no smart_wallets.json"}
        if me.get("error"): sm["error"] = me["error"]
        sm["early_seen"] = len(me.get("early") or [])
        sm["line"] = smart_line(sm, len(smart))
        dv = dev_history(mint, B, launch=me.get("launch") or {}, cache=cache)
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:100]}".replace(os.environ.get("HELIUS_API_KEY") or "\x00", "***")
        sm.setdefault("error", err); sm["line"] = smart_line(sm); dv.setdefault("error", err); dv["line"] = dev_line(dv)
    _save_cache(cache)
    sm["helius_credits"] = dv["helius_credits"] = B.credits
    return sm, dv
