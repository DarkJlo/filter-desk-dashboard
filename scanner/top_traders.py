"""r10.2 top-traders check (WARN only, never changes a verdict).

Keyless GeckoTerminal trades + a small Helius budget (~151 credits for 15 wallets, hard cap TT_MAX_CREDITS).
  1. Main pool = highest-liquidity Solana pair on DexScreener (passed in, or fetched here).
  2. GeckoTerminal /pools/<pool>/trades (max 300 trades, last 24h) with trade_volume_in_usd_greater_than=500,
     falling back to 100, then 0, when there are too few trades / wallets.
  3. Aggregate by tx_from_address (buy USD, sell USD, net, trade count), drop program/router/pool addresses,
     keep the top 15 by total volume.
  4. Helius getTransactionsForAddress (oldest first, limit 1, full) per wallet = 10 credits each:
     wallet age + funder (source of the first SOL transfer into it). Cached forever per wallet
     (data/trader_cache.json), so a wallet seen before costs 0.
  5. One getMultipleAccounts call (1 credit): the wallets' associated token accounts for this mint
     (Token + Token-2022) and the funders' SOL balances.
  6. Funder clustering ignores CEX funders: Dune-listed CEX hot wallets (cex_wallets.py), any funder
     holding > 50 SOL, and any funder seen funding top traders of >= 3 different mints (likely an exchange).

Best-effort: every failure is caught and returned as summary["error"]; callers must never change a verdict
because of this module. The Helius key comes from env HELIUS_API_KEY and is never printed or logged.
"""
import hashlib, json, os, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    from cex_wallets import CEX_WALLETS
except Exception:  # pragma: no cover
    CEX_WALLETS = {}

HELIUS_URL = "https://mainnet.helius-rpc.com/"
GT_TRADES = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/trades"
DS_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FD_DATA_DIR") or os.path.join(os.path.dirname(_HERE), "data")
CACHE_PATH = os.path.join(DATA_DIR, "trader_cache.json")   # matches data/*cache*.json (gitignored, Actions cache)

TOP_N = 15
USD_THRESHOLDS = (500, 100, 0)   # GT trade_volume_in_usd_greater_than, tried in order
MIN_TRADES, MIN_WALLETS = 60, 15  # fall back to a lower threshold below this
FRESH_DAYS = 7
EXCHANGE_SOL = 50                 # unlabeled funder holding > 50 SOL = likely exchange / service
MULTI_TOKEN_FUNDER = 3            # funder seen behind top traders of >= 3 mints = likely exchange / service
CO_CREATED_MIN, CO_CREATED_SECS = 3, 120  # ... unless >= 3 of its wallets here were born within 2 min of each other
                                  # (bundled wallet creation, e.g. CHICO: 15 wallets in ONE dev tx) -> never "exchange"
DUST_USD = 1.0                    # remaining position worth < $1 counts as sold out
WARN_SHARED, WARN_FRESH = 5, 8    # trader_cluster WARN thresholds (of the top 15)
TT_MAX_CREDITS = int(os.environ.get("TT_MAX_CREDITS") or 170)
TT_MAX_SECS = int(os.environ.get("TT_MAX_SECS") or 150)
HELIUS_WORKERS = 3
FIRST_TX_CREDITS, RPC_CREDITS = 10, 1

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
PROGRAM_ADDRS = {
    "11111111111111111111111111111111", TOKEN_PROGRAM, TOKEN_2022, ATA_PROGRAM,
    "ComputeBudget111111111111111111111111111111",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # PumpSwap AMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun bonding curve
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM authority
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",   # Raydium LaunchLab
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora pools
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",   # Meteora DAMM v2
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",   # Meteora DBC
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma",  # OKX DEX router
}

# ---------------- base58 + associated-token-account derivation (no extra deps) ----------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58decode(s):
    n = 0
    for c in s: n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw

def b58encode(b):
    n = int.from_bytes(b, "big"); out = ""
    while n: n, r = divmod(n, 58); out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out

_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)

def _on_curve(b):
    """ed25519 point decompression check (same acceptance as curve25519-dalek decompress)."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    y %= _P
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0: return True
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P == 0: return True
    x = x * _I % _P
    return (x * x - x2) % _P == 0

def find_pda(seeds, program):
    pid = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not _on_curve(h): return b58encode(h)
    return None

def ata(owner, mint, token_program=TOKEN_PROGRAM):
    return find_pda([b58decode(owner), b58decode(token_program), b58decode(mint)], ATA_PROGRAM)

# ---------------- cache ----------------
def _load_cache():
    try:
        c = json.load(open(CACHE_PATH))
        c.setdefault("first", {}); c.setdefault("funder_mints", {}); return c
    except Exception:
        return {"first": {}, "funder_mints": {}}

def _save_cache(c):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(c, open(CACHE_PATH, "w"))
    except Exception:
        pass

# ---------------- Helius (credit-counted, key never printed) ----------------
class _Budget:
    def __init__(self): self.credits, self.calls, self.t0 = 0, 0, time.time()
    def can(self, cost): return self.credits + cost <= TT_MAX_CREDITS and time.time() - self.t0 < TT_MAX_SECS

def _rpc(method, params, cost, B):
    key = os.environ.get("HELIUS_API_KEY")
    if not key or not B.can(cost): return None
    for t in range(4):
        try:
            r = requests.post(HELIUS_URL, params={"api-key": key}, timeout=20,
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if r.status_code == 429:
                time.sleep(1.0 + t); continue
            B.credits += cost; B.calls += 1   # billed once the request is served (incl. RPC-level errors)
            j = r.json()
            if "result" in j: return j["result"]
            return None
        except Exception:
            pass  # never print the exception: it contains the URL with the key
        time.sleep(0.5 * (t + 1))
    return None

def _first_tx(wallet, B):
    """(first_block_time, funder) from the wallet's oldest transaction; funder None if not found."""
    r = _rpc("getTransactionsForAddress", [wallet, {"sortOrder": "asc", "limit": 1, "transactionDetails": "full",
             "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}], FIRST_TX_CREDITS, B)
    if not r or not r.get("data"): return None
    tx = r["data"][0]
    msg = ((tx.get("transaction") or {}).get("message")) or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    funder = None
    for ins in msg.get("instructions") or []:
        pi = ins.get("parsed") if isinstance(ins, dict) else None
        if isinstance(pi, dict) and pi.get("type") in ("transfer", "transferWithSeed", "createAccount"):
            info = pi.get("info") or {}
            if info.get("destination", info.get("newAccount")) == wallet and info.get("source"):
                funder = info["source"]; break
    if not funder and keys and keys[0] != wallet: funder = keys[0]   # someone else paid for its first tx
    sig = ((tx.get("transaction") or {}).get("signatures") or [None])[0]
    return {"bt": tx.get("blockTime"), "funder": funder, "slot": tx.get("slot"), "sig": sig}

# ---------------- GeckoTerminal / DexScreener (keyless) ----------------
def _get(url, params=None, tries=4):
    for t in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(6 * (t + 1)); continue
            if r.status_code == 200: return r.json()
        except Exception:
            pass
        time.sleep(2 * (t + 1))
    return None

def best_pair(mint):
    j = _get(DS_TOKEN.format(mint=mint)) or {}
    ps = [p for p in j.get("pairs") or [] if p.get("chainId") == "solana"]
    return max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0) if ps else None

def _ts(s):
    from datetime import datetime
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception: return None

def fetch_trades(pool):
    """Return (trades, usd_threshold_used, gt_calls)."""
    trades, used, calls = [], None, 0
    for thr in USD_THRESHOLDS:
        j = _get(GT_TRADES.format(pool=pool), {"trade_volume_in_usd_greater_than": thr}); calls += 1
        if j is None: continue
        T = [x.get("attributes") or {} for x in j.get("data") or []]
        trades, used = T, thr
        if len(T) >= MIN_TRADES and len({t.get("tx_from_address") for t in T}) >= MIN_WALLETS: break
        time.sleep(2.1)   # GT free tier ~30 req/min
    return trades, used, calls

def aggregate(trades, exclude):
    agg = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "n": 0})
    for t in trades:
        w = t.get("tx_from_address")
        if not w or w in exclude or t.get("is_anomalous"): continue
        try: usd = float(t.get("volume_in_usd") or 0)
        except Exception: usd = 0.0
        a = agg[w]; a["n"] += 1
        if t.get("kind") in ("buy", "sell"): a[t["kind"]] += usd
    rows = [{"wallet": w, "buy_usd": round(a["buy"], 2), "sell_usd": round(a["sell"], 2),
             "net_usd": round(a["buy"] - a["sell"], 2), "trades": a["n"]} for w, a in agg.items()]
    rows.sort(key=lambda r: -(r["buy_usd"] + r["sell_usd"]))
    return rows

# ---------------- main entry ----------------
def summary_line(s):
    if not s: return ""
    if not s.get("n"):
        return "Top traders: n/a" + (f" ({s['error']})" if s.get("error") else "")
    wh = s.get("window_hours")
    win = f", last ~{wh:.0f}h" if isinstance(wh, (int, float)) else ""
    line = (f"Top traders ({s['n']}{win}): {s.get('fresh_lt7d', '?')} fresh <7d, "
            f"{s.get('shared_funder_max', '?')} share a non-CEX funder, {s.get('cex_funded', '?')} CEX-funded, "
            f"{s.get('still_holding', '?')} still holding, {s.get('sold_out', '?')} sold out")
    if s.get("error"): line += f" [partial: {s['error']}]"
    return line

def is_cluster_warn(s):
    return bool(s) and ((s.get("shared_funder_max") or 0) >= WARN_SHARED or (s.get("fresh_lt7d") or 0) >= WARN_FRESH)

def warn_text(s):
    why = []
    if (s.get("shared_funder_max") or 0) >= WARN_SHARED: why.append(f"{s['shared_funder_max']} of top {s['n']} traders share a non-CEX funder")
    if (s.get("fresh_lt7d") or 0) >= WARN_FRESH: why.append(f"{s['fresh_lt7d']} of top {s['n']} traders are wallets <7d old")
    return "TRADER CLUSTER WARN: " + "; ".join(why)

def top_traders(mint, pair=None, record_funders=True):
    """Best-effort summary dict. Never raises."""
    B = _Budget()
    s = {"n": 0, "fresh_lt7d": None, "shared_funder_max": None, "cex_funded": None, "still_holding": None,
         "sold_out": None, "window_hours": None, "helius_credits_est": 0, "error": None}
    try:
        _run(mint, pair, s, B, record_funders)
    except Exception as e:
        s["error"] = f"{type(e).__name__}: {str(e)[:120]}".replace(os.environ.get("HELIUS_API_KEY") or "\x00", "***")
    s["helius_credits_est"] = B.credits
    s["secs"] = round(time.time() - B.t0, 1)
    return s

def _run(mint, pair, s, B, record_funders):
    errs = []
    if not pair or pair.get("chainId") not in (None, "solana"): pair = best_pair(mint)
    if not pair or not pair.get("pairAddress"):
        s["error"] = "no DexScreener pair"; return
    pool = pair["pairAddress"]; s["pool"] = pool
    try: price = float(pair.get("priceUsd") or 0)
    except Exception: price = 0.0
    trades, thr, _ = fetch_trades(pool)
    if not trades:
        s["error"] = "GeckoTerminal trades unavailable"; return
    s["usd_min"] = thr; s["trades_seen"] = len(trades)
    tss = [x for x in (_ts(t.get("block_timestamp") or "") for t in trades) if x]
    if tss: s["window_hours"] = round((max(tss) - min(tss)) / 3600, 1)
    exclude = PROGRAM_ADDRS | {pool, mint}
    top = aggregate(trades, exclude)[:TOP_N]
    s["n"] = len(top)
    if not top:
        s["error"] = "no trader wallets"; return
    if not os.environ.get("HELIUS_API_KEY"):
        s["wallets"] = top; s["error"] = "HELIUS_API_KEY missing (volume only)"; return

    cache = _load_cache(); C = cache["first"]
    need = [r["wallet"] for r in top if r["wallet"] not in C]
    with ThreadPoolExecutor(HELIUS_WORKERS) as ex:
        res = list(ex.map(lambda w: (w, _first_tx(w, B)), need))
    for w, ft in res:
        if ft and ft.get("bt"): C[w] = ft
    miss = [r["wallet"] for r in top if r["wallet"] not in C]
    if miss: errs.append(f"first tx unknown for {len(miss)}")

    now = time.time()
    for r in top:
        ft = C.get(r["wallet"]) or {}
        r["age_d"] = round((now - ft["bt"]) / 86400, 1) if ft.get("bt") else None
        r["funder"] = ft.get("funder")

    # one batched call: ATAs (Token + Token-2022) for the mint + funder SOL balances
    funders = sorted({r["funder"] for r in top if r.get("funder") and r["funder"] not in CEX_WALLETS})
    atas = []
    for r in top:
        atas += [ata(r["wallet"], mint, TOKEN_PROGRAM), ata(r["wallet"], mint, TOKEN_2022)]
    accts = atas + funders
    vals = None
    if accts:
        res = _rpc("getMultipleAccounts", [accts[:100], {"encoding": "jsonParsed"}], RPC_CREDITS, B)
        vals = (res or {}).get("value") if res else None
    lamports = {}
    if vals is None:
        errs.append("balances unavailable")
    else:
        for i, r in enumerate(top):
            amt = 0.0
            for v in vals[2 * i: 2 * i + 2]:
                try: amt += float(v["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                except Exception: pass
            r["balance"] = amt
            r["holding"] = amt * price >= DUST_USD if price else amt > 0
        for f, v in zip(funders, vals[len(atas):]):
            lamports[f] = (v or {}).get("lamports") or 0

    fm = cache["funder_mints"]
    def kind(f):
        if not f: return None
        if f in CEX_WALLETS: return "cex"
        if lamports.get(f, 0) > EXCHANGE_SOL * 1e9: return "big"
        if len(set(fm.get(f) or []) | {mint}) >= MULTI_TOKEN_FUNDER: return "multi"
        return "private"
    kinds = {r["wallet"]: kind(r.get("funder")) for r in top}
    # co-created override: a rich / reused funder whose wallets here were all born together is a dev wallet, not a CEX
    byf = defaultdict(list)
    for r in top:
        if r.get("funder") and r["funder"] not in CEX_WALLETS and (C.get(r["wallet"]) or {}).get("bt"):
            byf[r["funder"]].append(C[r["wallet"]]["bt"])
    co_max = 0
    for f, bts in byf.items():
        bts.sort(); j = 0; best = 0
        for i in range(len(bts)):
            while bts[i] - bts[j] > CO_CREATED_SECS: j += 1
            best = max(best, i - j + 1)
        co_max = max(co_max, best)
        if best >= CO_CREATED_MIN:
            for r in top:
                if r.get("funder") == f: kinds[r["wallet"]] = "private"
    s["co_created_max"] = co_max
    for r in top: r["funder_kind"] = kinds[r["wallet"]]
    known = [r for r in top if r.get("age_d") is not None]
    s["fresh_lt7d"] = sum(1 for r in known if r["age_d"] < FRESH_DAYS)
    s["cex_funded"] = sum(1 for r in top if kinds[r["wallet"]] in ("cex", "big", "multi"))
    cnt = Counter(r["funder"] for r in top if kinds[r["wallet"]] == "private")
    s["shared_funder_max"] = max(cnt.values()) if cnt and max(cnt.values()) >= 2 else 0   # 1 = nobody shares
    if cnt and s["shared_funder_max"] >= 2:
        f0, c0 = cnt.most_common(1)[0]; s["shared_funder"] = f0
    if vals is not None:
        s["still_holding"] = sum(1 for r in top if r.get("holding"))
        s["sold_out"] = s["n"] - s["still_holding"]
    s["wallets"] = top
    if record_funders:
        for r in top:
            f = r.get("funder")
            if f and kinds[r["wallet"]] != "cex":
                L = fm.setdefault(f, [])
                if mint not in L: L.append(mint); del L[:-10]
    _save_cache(cache)
    if errs: s["error"] = "; ".join(errs)
