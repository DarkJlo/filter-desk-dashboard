#!/usr/bin/env python3
"""
Strict free Solana memecoin filter desk (RugCheck full report + DexScreener).
Usage:
  python filter_desk.py <mint1> <mint2> ...
  python filter_desk.py --scan [N]        # fresh mints from DexScreener (default N=30)
  python filter_desk.py --scan-older [N]  # graduated 2-48h, liq>=$15k (GeckoTerminal + DexScreener search)
  python filter_desk.py --bottoms [N]     # safety + bottom-candidate price action (1-14d, liq>=$15k)
  python filter_desk.py --bundle <mint>   # bundle/cluster analysis via Helius
  python filter_desk.py --trace <mint>    # funding-source cluster trace via Helius
  python filter_desk.py --scan-all [N]    # both, deduped
  python filter_desk.py --entry <mint>... # r10 entry-timing label only (no Helius, not logged)
Read-only: no keys, no signing. Every check appended to checks_log.jsonl.
"""
import sys, json, os, time, requests
from collections import Counter
from datetime import datetime, timezone

# ---------------- rules version (bump on ANY rule/threshold change; see docs/RULES_CHANGELOG.md) ----------------
FILTER_VERSION = "r10-2026-10-23"

# ---------------- thresholds ----------------
MAX_SCORE_NORM = 50
MAX_SCORE_RAW = 5000
MIN_LP_LOCKED_PCT = 50
MAX_TOP10_PCT = 30.0          # excl. LP/AMM/bonding-curve accounts
MAX_SINGLE_HOLDER_PCT = 10.0  # non-LP holder
MIN_LIQUIDITY_USD = 15_000
MIN_PAIR_AGE_MIN = 30
MIN_VOLUME_24H = 10_000
MIN_SELL_BUY_RATIO = 0.5      # h24 sells/buys < 0.5 (buys > 2x sells) = kill
MAX_SELL_BUY_RATIO = 4.0      # h24 sells/buys > 4 = kill
H1_ZERO_SELL_MAX_BUYS = 20    # h1 sells == 0 with h1 buys > 20 = kill
# r8: buy-heavy h24 flow on an AGED pair is momentum, not a fake-volume launch -> WARN instead of KILL
# (startupSI killed at 5-9h with sells/buys 0.40-0.47, ran to ~20x; rugs Vader/CATONICE had 0.25-0.31)
LOPSIDED_SOFT_MIN_AGE_H = 4.0     # pair age >= 4h ...
LOPSIDED_SOFT_MIN_RATIO = 0.38    # ... and h24 sells/buys >= 0.38 (buys <= ~2.6x sells) -> LOPSIDED WARN
# r9 (WARN only, no verdict change): supply spread so thin that no non-LP wallet holds >= 1% and top10 < 7% at a
# tradeable size looks like a wallet farm. Of 10 such tokens with liq >= $15k in the log, 6 went to ~$2-3k FDV
# (supply dumped into a burned-LP pool: Doomsday, Vader, CATONICE, Piccolo, 3oUzvv, PIERRE-v1), 3 fell 60-97%
# (MINTER, FTR, WORTHLESS), and 1 ran 24x (startupSI, identical profile). It cannot be a KILL; treat it as an exit-risk flag.
FLAT_WARN_MAX_TOP10_PCT = 7.0
FLAT_WARN_MAX_TOP1_PCT = 1.0
SELL_QUOTE_USD = 100
SELL_SLIPPAGE_BPS = 1500
MAX_SELL_IMPACT_PCT = 15.0
MIN_SELL_RETURN_FRAC = 0.80
JUP_QUOTE_URLS = ["https://lite-api.jup.ag/swap/v1/quote", "https://quote-api.jup.ag/v6/quote"]
MAX_FDV_LIQ_RATIO = 50
MAX_CREATOR_PCT = 0.0         # creator still holding any supply -> kill
RUGCHECK_DELAY = 1.2
UNIFORM_CLUSTER_MIN = 8       # >= this many top non-LP holders with near-identical balances = bundle
UNIFORM_CLUSTER_TOL = 0.15    # within +/-15% of the cluster median balance
SCAN_DEFAULT_N = 30

MAX_HOLDER_MISMATCH_PTS = 10.0  # |top10 rugcheck - top10 on-chain| > this = kill
HELIUS_URL = "https://mainnet.helius-rpc.com/"  # key from env HELIUS_API_KEY, never printed/logged
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
DEX_FEEDS = ["https://api.dexscreener.com/token-profiles/latest/v1",
             "https://api.dexscreener.com/token-boosts/latest/v1",
             "https://api.dexscreener.com/token-boosts/top/v1"]
_HERE = os.path.dirname(os.path.realpath(__file__))
REPO_DIR = os.environ.get("FD_REPO") or os.path.dirname(_HERE)
DATA_DIR = os.environ.get("FD_DATA_DIR") or os.path.join(REPO_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_PATH = os.path.join(DATA_DIR, "checks_log.jsonl")
SOL = "So11111111111111111111111111111111111111112"

import threading
from concurrent.futures import ThreadPoolExecutor
CACHE_TTL = 3600   # 60 min per-token cache
CACHE_PATH = os.path.join(DATA_DIR, "token_cache.json")
_cache_lock = threading.Lock(); _tcache = None
STAGE_A_WORKERS = 3

def _tc():
    global _tcache
    if _tcache is None:
        try: _tcache = json.load(open(CACHE_PATH))
        except Exception: _tcache = {}
    return _tcache

def cache_get(kind, key):
    with _cache_lock:
        e = _tc().get(f"{kind}:{key}")
        return e["v"] if e and time.time() - e["t"] < CACHE_TTL else None

def cache_put(kind, key, v):
    with _cache_lock:
        c = _tc(); c[f"{kind}:{key}"] = {"t": time.time(), "v": v}
        for k in [k for k, e in c.items() if time.time() - e["t"] > CACHE_TTL]: del c[k]

def cache_flush():
    with _cache_lock:
        json.dump(_tc(), open(CACHE_PATH, "w"), default=str)

def cached(kind, mint, fn, M, mkey):
    """Cache a verdict function's (reasons, metric, warns) for 60 min."""
    ck = f"{FILTER_VERSION}:{mint}"   # never reuse a verdict cached under older rules
    hit = cache_get(kind, ck)
    if hit is not None:
        M[mkey] = hit["m"]; M.setdefault("warns", []).extend(hit["w"]); M[f"{mkey}_cached"] = True
        return hit["r"]
    w0 = len(M.get("warns") or []); R = fn()
    cache_put(kind, ck, {"r": R, "m": M.get(mkey), "w": (M.get("warns") or [])[w0:]})
    return R

RC_STATE = {"consec_fail": 0, "fails": 0, "ok": 0, "dead": False, "skipped": 0}
_rc_lock = threading.Lock(); _rc_last = [0.0]

def rugcheck_health():
    for probe in range(2):
        try:
            r = requests.get(RUGCHECK_REPORT.format(mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") + "/summary", timeout=8)
            if r.status_code == 200: RC_STATE["dead"] = False; return True
        except Exception: pass
        time.sleep(1)
    RC_STATE["dead"] = True; return False
RC_BREAKER = 5   # after 5 consecutive failures, drop to 1 attempt per token until RugCheck recovers

def rugcheck_report(mint):
    hit = cache_get("rugcheck", mint)
    if hit is not None: return hit
    if RC_STATE["dead"] or RC_STATE["consec_fail"] >= RC_BREAKER:
        RC_STATE["dead"] = True; RC_STATE["skipped"] += 1; return None
    with _rc_lock:  # keep ~1.2s spacing even with parallel workers
        wait = RUGCHECK_DELAY - (time.time() - _rc_last[0])
        if wait > 0: time.sleep(wait)
        rep = get_json(RUGCHECK_REPORT.format(mint=mint), timeout=20, tries=3, backoff="exp")
        _rc_last[0] = time.time()
    if rep: cache_put("rugcheck", mint, rep)
    if rep: RC_STATE.update(consec_fail=0, ok=RC_STATE["ok"] + 1)
    else: RC_STATE.update(consec_fail=RC_STATE["consec_fail"] + 1, fails=RC_STATE["fails"] + 1)
    return rep

def get_json(url, timeout=15, tries=3, backoff="lin"):
    for t in range(tries):
        try:
            r = requests.get(url, timeout=timeout); r.raise_for_status()
            j = r.json()
            if isinstance(j, dict) and j.get("error"): raise ValueError(j["error"])
            return j
        except Exception as e:
            err = e
            if t < tries - 1: time.sleep(2 ** (t + 1) if backoff == "exp" else 2 * (t + 1))
    print(f"  fetch error {url[:80]}: {str(err)[:120]}"); return None

HELIUS_CACHED = {"getTokenLargestAccounts", "getTokenSupply", "getMultipleAccounts", "getAccountInfo"}
MINT_PREFETCH = {}

def helius(method, params):
    key = os.environ.get("HELIUS_API_KEY")
    if not key: return None
    if method == "getAccountInfo" and params and params[0] in MINT_PREFETCH:
        return {"value": MINT_PREFETCH[params[0]]}
    ck = json.dumps([method, params], sort_keys=True) if method in HELIUS_CACHED else None
    if ck:
        hit = cache_get("helius", ck)
        if hit is not None: return hit
    res = _helius_raw(method, params, key)
    if ck and res is not None: cache_put("helius", ck, res)
    return res

def prefetch_mints(mints):
    for i in range(0, len(mints), 100):
        r = _helius_raw("getMultipleAccounts", [mints[i:i+100], {"encoding": "jsonParsed"}], os.environ.get("HELIUS_API_KEY"))
        for m, v in zip(mints[i:i+100], (r or {}).get("value") or []):
            if v: MINT_PREFETCH[m] = v

def _helius_raw(method, params, key):
    for t in range(5):
        try:
            r = requests.post(HELIUS_URL, params={"api-key": key}, timeout=20,
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            j = r.json()
            if "result" in j: return j["result"]
        except Exception:
            pass  # never print exception text: it would contain the URL with the key
        time.sleep(min(8, 0.5 * 2 ** t))  # backoff for 429 under parallel load
    print(f"  helius {method} failed"); return None

def onchain_checks(mint, rep, M):
    """Independent on-chain cross-check via Helius. Returns kill reasons."""
    R = []
    acct = helius("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    try:
        info = acct["value"]["data"]["parsed"]["info"]
        M["oc_program"] = acct["value"]["owner"]
    except Exception:
        return ["missing data: on-chain mint account"]
    M.update(oc_mintAuthority=info.get("mintAuthority"), oc_freezeAuthority=info.get("freezeAuthority"), oc_decimals=info.get("decimals"))
    if info.get("mintAuthority"): R.append("on-chain: mint authority not null")
    if info.get("freezeAuthority"): R.append("on-chain: freeze authority not null")
    exts = {e.get("extension"): e.get("state") for e in info.get("extensions") or []}
    M["oc_extensions"] = sorted(exts)
    tfc = exts.get("transferFeeConfig") or {}
    bps = max(int(((tfc.get(k) or {}).get("transferFeeBasisPoints")) or 0)
              for k in ("newerTransferFee", "olderTransferFee"))
    if bps > 0: R.append(f"on-chain token2022: transfer fee {bps/100}%")
    if (exts.get("transferHook") or {}).get("programId"): R.append("on-chain token2022: transfer hook")
    if "permanentDelegate" in exts and (exts["permanentDelegate"] or {}).get("delegate"):
        R.append("on-chain token2022: permanent delegate")
    if "nonTransferable" in exts: R.append("on-chain token2022: non-transferable")
    if "frozen" in json.dumps(exts.get("defaultAccountState") or "").lower():
        R.append("on-chain token2022: default account state frozen")
    # holders
    sup = helius("getTokenSupply", [mint]); big = helius("getTokenLargestAccounts", [mint])
    try:
        ui_supply = float(sup["value"]["uiAmountString"]); L = big["value"]
    except Exception:
        return R + ["missing data: on-chain holders"]
    owners = helius("getMultipleAccounts", [[a["address"] for a in L], {"encoding": "jsonParsed"}])
    try: ov = owners["value"]
    except Exception: return R + ["missing data: on-chain holder owners"]
    known = (rep or {}).get("knownAccounts") or {}
    excl = {a for a, v in known.items() if (v or {}).get("type") in ("AMM", "LP")}
    excl |= set(ALL_PAIRS.get(mint, set())) | set(KNOWN_AMM_AUTHORITIES)
    # resolve each holder's owner -> owning program; exclude AMM-program-owned PDAs
    owner_addrs = []
    for o in ov:
        try: owner_addrs.append(o["data"]["parsed"]["info"]["owner"])
        except Exception: owner_addrs.append(None)
    oinfo = helius("getMultipleAccounts", [[x for x in owner_addrs if x], {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])
    excluded_as = {}
    if oinfo and oinfo.get("value") is not None:
        for addr, acc in zip([x for x in owner_addrs if x], oinfo["value"]):
            prog = (acc or {}).get("owner")
            if prog in AMM_PROGRAMS: excl.add(addr); excluded_as[addr] = AMM_PROGRAMS[prog]
    else:
        R.append("missing data: holder owner programs")
    for a_ in excl & set(owner_addrs):
        excluded_as.setdefault(a_, "DexScreener pair / known AMM")
    M["oc_excluded_owners"] = excluded_as
    for mk in (rep or {}).get("markets") or []:
        excl.add(mk.get("pubkey"))
        for k in ("liquidityA", "liquidityB"):
            if isinstance(mk.get(k), str): excl.add(mk[k])
    pcts, holders = [], []
    for a, o in zip(L, ov):
        try: owner = o["data"]["parsed"]["info"]["owner"]
        except Exception: owner = None
        amt = float(a.get("uiAmountString") or 0)
        if amt <= 0 or a["address"] in excl or owner in excl: continue
        pcts.append(amt / ui_supply * 100)
        holders.append({"owner": owner, "amount": amt, "pct": amt / ui_supply * 100})
    pcts.sort(reverse=True)
    top10, top1 = sum(pcts[:10]), (pcts[0] if pcts else 0.0)
    med = sorted(pcts)[len(pcts)//2] if pcts else 0
    cluster = sum(1 for x in pcts if med and abs(x - med) <= UNIFORM_CLUSTER_TOL * med)
    M.update(oc_top10_pct=top10, oc_top_holder_pct=top1, oc_uniform_cluster=cluster, oc_supply=ui_supply)
    M["oc_holders"] = holders
    M["cluster_wallets"] = [h for h in holders if med and abs(h["pct"] - med) <= UNIFORM_CLUSTER_TOL * med]
    if top10 > MAX_TOP10_PCT: R.append(f"on-chain top10 {top10:.1f}% > {MAX_TOP10_PCT}%")
    if top1 > MAX_SINGLE_HOLDER_PCT: R.append(f"on-chain single holder {top1:.1f}% > {MAX_SINGLE_HOLDER_PCT}%")
    if cluster >= UNIFORM_CLUSTER_MIN: M["cluster_flag"] = True  # judged by bundle analysis
    rc = M.get("top10_pct")
    if rc is not None and abs(rc - top10) > MAX_HOLDER_MISMATCH_PTS:
        R.append(f"holder data mismatch (rugcheck {rc:.1f}% vs on-chain {top10:.1f}%)")
    # authority agreement with rugcheck
    if rep is not None and (bool(M.get("mintAuthority")) != bool(info.get("mintAuthority")) or
                            bool(M.get("freezeAuthority")) != bool(info.get("freezeAuthority"))):
        R.append("authority data mismatch (rugcheck vs on-chain)")
    return R

# ---------------- bundle analysis ----------------
BUNDLE_KILL_HELD_PCT = 5.0     # cluster holds >= 5% of supply ...
BUNDLE_KILL_MULTIPLE = 3.0     # ... and sits at >= 3x unrealized -> KILL; otherwise BUNDLE WARN
BUNDLE_TX_PAGES = 3            # up to 3 x 100 txs per wallet
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{addr}/transactions"
WSOL = SOL

def helius_txs(addr, stop_before_ts=None):
    key = os.environ.get("HELIUS_API_KEY")
    if not key: return None
    out, before = [], None
    for _ in range(BUNDLE_TX_PAGES):
        params = {"api-key": key, "limit": 100}
        if before: params["before"] = before
        page = None
        for t in range(3):
            try:
                r = requests.get(HELIUS_TX_URL.format(addr=addr), params=params, timeout=30)
                if r.status_code == 200: page = r.json(); break
            except Exception: pass  # never print: URL holds the key
            time.sleep(1 + t)
        if page is None: return out or None
        if not page: break
        out += page; before = page[-1].get("signature")
        if stop_before_ts and (page[-1].get("timestamp") or 0) < stop_before_ts: break
        time.sleep(0.3)
    return out

def wallet_flows(owner, mint, created_ts):
    txs = helius_txs(owner, created_ts)
    if txs is None: return None
    bought = cost = tin = tout = sold_sol = 0.0
    for tx in txs:
        tt = tx.get("tokenTransfers") or []
        ti = sum(float(x.get("tokenAmount") or 0) for x in tt if x.get("mint") == mint and x.get("toUserAccount") == owner)
        to = sum(float(x.get("tokenAmount") or 0) for x in tt if x.get("mint") == mint and x.get("fromUserAccount") == owner)
        if not ti and not to: continue
        nat = tx.get("nativeTransfers") or []
        so = sum(x.get("amount", 0) for x in nat if x.get("fromUserAccount") == owner) / 1e9 + \
             sum(float(x.get("tokenAmount") or 0) for x in tt if x.get("mint") == WSOL and x.get("fromUserAccount") == owner)
        si = sum(x.get("amount", 0) for x in nat if x.get("toUserAccount") == owner) / 1e9 + \
             sum(float(x.get("tokenAmount") or 0) for x in tt if x.get("mint") == WSOL and x.get("toUserAccount") == owner)
        tin += ti; tout += to
        if ti > to and so - si > 0: bought += ti - to; cost += so - si
        elif to > ti and si - so > 0: sold_sol += si - so
    return dict(received=tin, sent=tout, bought=bought, cost_sol=cost, sold_sol=sold_sol, txs_scanned=len(txs))

def analyze_bundle(mint, pair, M):
    W = M.get("cluster_wallets") or []
    if not W or not pair: return None
    created = (pair.get("pairCreatedAt") or 0) / 1000 - 86400  # include pre-migration bonding-curve buys
    pn, pu = float(pair.get("priceNative") or 0), float(pair.get("priceUsd") or 0)
    sol_usd = pu / pn if pn else None
    rows, tot_b, tot_c, tot_init, tot_cur = [], 0.0, 0.0, 0.0, 0.0
    for w in W:
        f = wallet_flows(w["owner"], mint, created)
        if f is None: continue
        init = max(f["received"], w["amount"])
        tot_b += f["bought"]; tot_c += f["cost_sol"]; tot_init += init; tot_cur += w["amount"]
        rows.append(dict(owner=w["owner"], cur=w["amount"], init=init,
                         sold_pct=(1 - w["amount"] / init) * 100 if init else None,
                         entry_sol=(f["cost_sol"] / f["bought"]) if f["bought"] else None,
                         funded_by_transfer=f["bought"] < 0.5 * init, txs=f["txs_scanned"]))
    if not rows: return None
    entry = tot_c / tot_b if tot_b else None
    sup = M.get("oc_supply") or 0
    st = dict(wallets=len(W), analyzed=len(rows), held_pct=sum(w["pct"] for w in W),
              init_pct=(tot_init / sup * 100) if sup else None,
              sold_pct=(1 - tot_cur / tot_init) * 100 if tot_init else None,
              avg_entry_sol=entry, avg_entry_usd=entry * sol_usd if (entry and sol_usd) else None,
              cur_price_usd=pu, multiple=(pn / entry) if entry else None,
              transfer_funded_wallets=sum(1 for r in rows if r["funded_by_transfer"]), per_wallet=rows)
    return st

def bundle_verdict(mint, pair, M):
    st = analyze_bundle(mint, pair, M)
    M["bundle"] = st
    if not st: return ["missing data: bundle analysis"]
    held, mult = st["held_pct"], st["multiple"]
    if held >= BUNDLE_KILL_HELD_PCT and (mult is None or mult >= BUNDLE_KILL_MULTIPLE):
        M["bundle_status"] = "KILL"
        return [f"bundle: cluster of {st['wallets']} holds {held:.1f}% at " + (f"{mult:.1f}x" if mult else "unknown entry (strict)")]
    M["bundle_status"] = "BUNDLE WARN"
    M.setdefault("warns", []).append(f"BUNDLE WARN: {st['wallets']} wallets hold {held:.2f}% (sold {st.get('sold_pct') or 0:.0f}%), "
                                     f"{('%.2fx' % mult) if mult else 'unknown'} unrealized")
    return []

# ---------------- funding trace (Bubblemaps-style) ----------------
TRACE_KILL_LINKED_PCT = 15.0    # linked clusters hold >= 15% of supply -> KILL
TRACE_KILL_LARGEST_PCT = 8.0    # largest single funder cluster >= 8% -> KILL
TRACE_WARN_LINKED_PCT = 5.0     # linked >= 5% -> WARN
TRACE_MIN_FUND_LAMPORTS = 10_000_000  # 0.01 SOL; ignores ~0.002 SOL rent refunds from closed token accounts
TRACE_HUB_OVERRIDE_MIN = 5      # busy unlabeled funder still counts if it funds >= 5 near-identical holders here
TRACE_HOPS = 2                  # 1 = direct funder only; 2 = also funder's funder
TRACE_MAX_SIG_PAGES = 5         # walk back up to 5 x 1000 signatures per wallet to find its first tx
TRACE_HUB_SIGS_SPAN_H = 6       # funder with 1000 sigs inside 6h = exchange/hub-like -> non-signal
TRACE_CACHE_PATH = os.path.join(DATA_DIR, "funder_cache.json")
CEX_WALLETS = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase", "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "Coinbase",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX", "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken", "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC",
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "Gate", "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": "KuCoin",
}
_fcache = None

def _fc():
    global _fcache
    if _fcache is None:
        try: _fcache = json.load(open(TRACE_CACHE_PATH))
        except Exception: _fcache = {"funder": {}, "hub": {}}
    return _fcache

def _fc_save(): json.dump(_fc(), open(TRACE_CACHE_PATH, "w"))

def first_funders(wallet):
    """Earliest incoming SOL senders for wallet (cached). None = unknown (too active / error)."""
    c = _fc()["funder"]
    if wallet in c: return c[wallet]
    before, oldest = None, None
    for _ in range(TRACE_MAX_SIG_PAGES):
        opts = {"limit": 1000}
        if before: opts["before"] = before
        sigs = helius("getSignaturesForAddress", [wallet, opts])
        if sigs is None: return None
        if not sigs: break
        oldest = sigs[-10:]
        before = sigs[-1]["signature"]
        if len(sigs) < 1000: break
    else:
        c[wallet] = {"funders": [], "status": "too active"}; _fc_save(); return c[wallet]
    if not oldest: c[wallet] = {"funders": [], "status": "no txs"}; _fc_save(); return c[wallet]
    key = os.environ.get("HELIUS_API_KEY"); txs = None
    for t in range(3):
        try:
            r = requests.post("https://api.helius.xyz/v0/transactions", params={"api-key": key}, timeout=30,
                              json={"transactions": [x["signature"] for x in oldest]})
            if r.status_code == 200: txs = r.json(); break
        except Exception: pass  # never print: URL holds the key
        time.sleep(1 + t)
    if txs is None: return None
    txs.sort(key=lambda x: x.get("timestamp") or 0)
    funders = []
    for tx in txs:
        fp = tx.get("feePayer")
        gain = sum((a.get("nativeBalanceChange") or 0) for a in tx.get("accountData") or [] if a.get("account") == wallet)
        # funded by someone else: they paid the fee and our SOL balance rose (covers plain transfers AND
        # close-account lamport routing used by bundlers); self-initiated txs (swaps, wSOL unwraps) are ignored
        if fp and fp != wallet and gain >= TRACE_MIN_FUND_LAMPORTS and fp not in funders:
            funders.append(fp)
        if funders: break
    c[wallet] = {"funders": funders[:2], "status": "ok" if funders else "no SOL inflow found"}; _fc_save()
    return c[wallet]

def is_hub(addr):
    if addr in CEX_WALLETS: return True
    c = _fc()["hub"]
    if addr in c: return c[addr]
    sigs = helius("getSignaturesForAddress", [addr, {"limit": 1000}]) or []
    hub = len(sigs) >= 1000 and ((sigs[0].get("blockTime") or 0) - (sigs[-1].get("blockTime") or 0)) / 3600 < TRACE_HUB_SIGS_SPAN_H
    c[addr] = hub; _fc_save(); return hub

def funding_trace(mint, M):
    H = M.get("oc_holders") or []
    if not H: return None
    wallets = [h["owner"] for h in H if h.get("owner")]
    pct = {h["owner"]: h["pct"] for h in H}
    info = {w: first_funders(w) for w in wallets}
    parent = {w: w for w in wallets}
    def find(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b): parent[find(a)] = find(b)
    by_funder, hubs, unknown = {}, set(), 0
    for w, f in info.items():
        if not f or f.get("status") not in ("ok",): unknown += 1; continue
        for fu in f["funders"]:
            if fu in parent: union(w, fu); continue          # holders funding each other
            by_funder.setdefault(fu, []).append(w)
    overridden = set()
    for fu, ws in by_funder.items():
        if len(ws) < 2: continue
        if fu in CEX_WALLETS: hubs.add(fu); continue
        if is_hub(fu):
            ps = sorted(pct[w] for w in ws); med = ps[len(ps)//2]
            uniform = sum(1 for x in ps if abs(x - med) <= UNIFORM_CLUSTER_TOL * med)
            if not (len(ws) >= TRACE_HUB_OVERRIDE_MIN and uniform >= TRACE_HUB_OVERRIDE_MIN):
                hubs.add(fu); continue
            overridden.add(fu)   # busy unlabeled funder seeding many identical holders = bundler service
        for w in ws[1:]: union(ws[0], w)
    # hop 2: funders' own funders (bundlers usually route through fresh intermediate wallets)
    by_root = {}
    if TRACE_HOPS >= 2:
        for fu, ws in by_funder.items():
            if fu in CEX_WALLETS or fu in hubs: continue
            f2 = first_funders(fu)
            for r2 in (f2 or {}).get("funders", []):
                if r2 in ws: continue  # circular (funder was funded by the holder) = not a hop
                if r2 in parent: union(ws[0], r2)
                by_root.setdefault(r2, set()).add(fu)
        for r2, fus in by_root.items():
            if len(fus) < 2 or is_hub(r2): continue
            ws = [w for fu in fus for w in by_funder[fu]]
            for w in ws[1:]: union(ws[0], w)
    comps = {}
    for w in wallets: comps.setdefault(find(w), []).append(w)
    clusters = sorted([c for c in comps.values() if len(c) >= 2], key=lambda c: -sum(pct[x] for x in c))
    out = dict(holders_traced=len(wallets), unknown_funding=unknown, hub_funders_ignored=sorted(hubs),
               busy_funders_counted=sorted(overridden),
               clusters=len(clusters), linked_pct=sum(pct[x] for c in clusters for x in c),
               largest_pct=(sum(pct[x] for x in clusters[0]) if clusters else 0.0),
               cluster_detail=[{"wallets": len(c), "pct": sum(pct[x] for x in c),
                                "funders": sorted({fu for x in c for fu in (info[x] or {}).get("funders", [])})[:3]} for c in clusters])
    return out

def trace_verdict(mint, M):
    tr = funding_trace(mint, M); M["trace"] = tr
    if tr is None: return ["missing data: funding trace"]
    if tr["linked_pct"] >= TRACE_KILL_LINKED_PCT:
        return [f"funding trace: linked clusters hold {tr['linked_pct']:.1f}% >= {TRACE_KILL_LINKED_PCT}%"]
    if tr["largest_pct"] >= TRACE_KILL_LARGEST_PCT:
        return [f"funding trace: largest funder cluster {tr['largest_pct']:.1f}% >= {TRACE_KILL_LARGEST_PCT}%"]
    if tr["linked_pct"] >= TRACE_WARN_LINKED_PCT:
        M.setdefault("warns", []).append(f"FUNDING WARN: {tr['clusters']} clusters hold {tr['linked_pct']:.1f}% (largest {tr['largest_pct']:.1f}%)")
    return []

# ---------------- GMGN-style same-slot bundler detection ----------------
BUNDLER_KILL_PCT = 20.0        # same-slot buyer groups still holding >= 20% of supply -> KILL
BUNDLER_WARN_PCT = 10.0        # >= 10% -> WARN
BUNDLER_SLOT_WINDOW = 2        # first buys within <= 2 slots of each other are grouped
BUNDLER_EARLY_SIGS = 1000      # earliest transactions of the mint scanned
BUNDLER_MAX_PARSE = 600        # successful early txs parsed via Helius enhanced API

def helius_enhanced(sigs):
    key = os.environ.get("HELIUS_API_KEY"); out = []
    for i in range(0, len(sigs), 100):
        for t in range(3):
            try:
                r = requests.post("https://api.helius.xyz/v0/transactions", params={"api-key": key}, timeout=60,
                                  json={"transactions": sigs[i:i+100]})
                if r.status_code == 200: out += r.json(); break
            except Exception: pass  # never print: URL holds the key
            time.sleep(1 + t)
        else: return None
    return out

def token_balance(owner, mint):
    r = helius("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    if r is None: return None
    return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"] or 0) for a in r.get("value") or [])

def bundler_analysis(mint, M):
    r = helius("getTransactionsForAddress", [mint, {"sortOrder": "asc", "limit": BUNDLER_EARLY_SIGS,
                                                     "transactionDetails": "signatures"}])
    try: sigs = [x for x in r["data"] if not x.get("err")]
    except Exception: return None
    txs = helius_enhanced([x["signature"] for x in sigs[:BUNDLER_MAX_PARSE]])
    if txs is None: return None
    pools = set(ALL_PAIRS.get(mint, set())) | set((M.get("oc_excluded_owners") or {}).keys())
    first_buy = {}  # wallet -> (slot, amount)
    for tx in txs:
        fp = tx.get("feePayer")
        for x in tx.get("tokenTransfers") or []:
            to = x.get("toUserAccount")
            if x.get("mint") != mint or not to or to in pools: continue
            frm = x.get("fromUserAccount")
            if frm in pools or (frm and frm not in first_buy and to == fp):  # bought from pool/curve
                if to not in first_buy: first_buy[to] = (tx.get("slot"), float(x.get("tokenAmount") or 0))
    slots = sorted((v[0], w) for w, v in first_buy.items() if v[0])
    groups, cur = [], []
    for sl, w in slots:
        if cur and sl - cur[0][0] > BUNDLER_SLOT_WINDOW:
            if len({x[1] for x in cur}) >= 2: groups.append(cur)
            cur = []
        cur.append((sl, w))
    if len({x[1] for x in cur}) >= 2: groups.append(cur)
    bundlers = {w for g in groups for _, w in g}
    top = {h["owner"]: h["amount"] for h in (M.get("oc_holders") or [])}
    sup = M.get("oc_supply")
    if not sup: return None
    held, per_group = 0.0, []
    for g in groups:
        gh = 0.0
        for _, w in g:
            bal = top[w] if w in top else token_balance(w, mint)
            gh += bal or 0
        held += gh
        per_group.append({"slot": g[0][0], "wallets": len({w for _, w in g}), "held_pct": gh / sup * 100})
    first_slot = slots[0][0] if slots else None
    early_pct = held / sup * 100
    # whole-life: current holders' first-receipt slots
    hb = holder_slot_bundles(mint, M)
    life_wallets, life_held = set(), 0.0
    for g in (hb or {}).get("groups", []):
        for w, amt in zip(g["wallets"], g["amounts"]):
            if w not in bundlers and w not in life_wallets:   # don't double count early-window bundlers
                life_wallets.add(w); life_held += amt
    for g in (hb or {}).get("groups", []):
        per_group.append({"slot": g["slot"], "wallets": len(g["wallets"]), "held_pct": g["held"] / sup * 100, "src": "holder-first"})
    total = early_pct + life_held / sup * 100
    return dict(early_txs=len(txs), early_buyers=len(first_buy), bundle_groups=len(groups),
                bundler_wallets=len(bundlers) + len(life_wallets), early_bundler_pct=early_pct,
                lifetime_bundler_pct=life_held / sup * 100, bundler_pct=total, first_buy_slot=first_slot,
                early_bundled_frac=(len(bundlers) / len(first_buy)) if first_buy else None,
                holders_total=(hb or {}).get("holders_total"), holders_scanned=(hb or {}).get("holders_scanned"),
                holder_groups=len((hb or {}).get("groups", [])), adjacent_bundled=(hb or {}).get("adjacent_bundled"),
                top_groups=sorted(per_group, key=lambda g: -g["held_pct"])[:6])

BUNDLER_HOLDER_SCAN = 100       # top-N current holders (via Helius DAS getTokenAccounts) checked for first-buy slot
BUNDLER_DAS_MAX_PAGES = 20
BUNDLED_LAUNCH_FRAC = 0.80      # >= 80% of early buyers in same-slot groups -> WARN 'bundled launch'

def all_token_accounts(mint):
    accts = []
    for page in range(1, BUNDLER_DAS_MAX_PAGES + 1):
        r = helius("getTokenAccounts", {"mint": mint, "limit": 1000, "page": page})
        if r is None: return None if not accts else accts
        ta = r.get("token_accounts") or []
        accts += ta
        if len(ta) < 1000: break
    return accts

BUNDLER_TXIDX_WINDOW = 10       # another buy of this mint within +/-10 tx positions in the same slot = bundled entry

def first_receipt_slot(token_account, mint=None):
    """(slot, bundled) for the token account's first receipt; bundled = another mint tx adjacent in same block."""
    r = helius("getTransactionsForAddress", [token_account, {"sortOrder": "asc", "limit": 3, "transactionDetails": "signatures"}])
    try:
        ok = [x for x in r["data"] if not x.get("err")]
        if not ok: return None
        x = ok[0]
    except Exception: return None
    bundled = False
    if mint:
        s = helius("getTransactionsForAddress", [mint, {"sortOrder": "asc", "limit": 100, "transactionDetails": "signatures",
                                                        "filters": {"slot": {"gte": x["slot"], "lte": x["slot"]}}}])
        try:
            bundled = any(t["signature"] != x["signature"] and not t.get("err") and
                          abs(t["transactionIndex"] - x["transactionIndex"]) <= BUNDLER_TXIDX_WINDOW for t in s["data"])
        except Exception: pass
    return (x["slot"], bundled)

def holder_slot_bundles(mint, M):
    """Whole-life detection: current top holders grouped by the slot their token account first received tokens."""
    accts = all_token_accounts(mint)
    if not accts: return None
    excl = set(ALL_PAIRS.get(mint, set())) | set((M.get("oc_excluded_owners") or {}).keys()) | set(KNOWN_AMM_AUTHORITIES)
    dec = M.get("oc_decimals") or 6; sup = M.get("oc_supply")
    by_owner = {}
    for a in accts:
        o = a.get("owner"); amt = (a.get("amount") or 0) / 10 ** dec
        if not o or o in excl or amt <= 0: continue
        cur = by_owner.get(o)
        if not cur or amt > cur[0]: by_owner[o] = (amt, a.get("address"))
    top = sorted(by_owner.items(), key=lambda kv: -kv[1][0])[:BUNDLER_HOLDER_SCAN]
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(lambda kv: first_receipt_slot(kv[1][1], mint), top))
    rows = sorted((r[0], o, amt_ta[0]) for (o, amt_ta), r in zip(top, res) if r)
    adjacent = [(o, amt_ta[0]) for (o, amt_ta), r in zip(top, res) if r and r[1]]
    groups, cur = [], []
    for r in rows:
        if cur and r[0] - cur[0][0] > BUNDLER_SLOT_WINDOW:
            if len(cur) >= 2: groups.append(cur)
            cur = []
        cur.append(r)
    if len(cur) >= 2: groups.append(cur)
    if adjacent:
        groups.append([(None, o, amt) for o, amt in adjacent])   # bundled-entry holders (adjacent same-block buys)
    return dict(holders_total=len(by_owner), holders_scanned=len(top), slots_found=len(rows), adjacent_bundled=len(adjacent),
                groups=[{"slot": g[0][0], "wallets": [x[1] for x in g], "amounts": [x[2] for x in g],
                          "held": sum(x[2] for x in g)} for g in groups])

def bundler_verdict(mint, M):
    b = bundler_analysis(mint, M); M["bundler"] = b
    if b is None: return ["missing data: bundler analysis"]
    if b["bundler_pct"] >= BUNDLER_KILL_PCT:
        return [f"bundlers: same-slot buyer groups hold {b['bundler_pct']:.1f}% >= {BUNDLER_KILL_PCT}%"]
    if b["bundler_pct"] >= BUNDLER_WARN_PCT:
        M.setdefault("warns", []).append(f"BUNDLER WARN: {b['bundler_wallets']} same-slot buyers hold {b['bundler_pct']:.1f}%")
    if (b.get("early_bundled_frac") or 0) >= BUNDLED_LAUNCH_FRAC:
        M.setdefault("warns", []).append(f"bundled launch: {b['early_bundled_frac']*100:.0f}% of {b['early_buyers']} early buyers in same-slot groups")
    return []

ALL_PAIRS = {}   # mint -> every DexScreener pairAddress seen
AMM_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun bonding curve",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora DAMM v1",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
}
KNOWN_AMM_AUTHORITIES = {"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium AMM v4 authority",
                         "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "Raydium CPMM authority"}
LP_BURNED_DEXES = {"pumpswap", "pumpfun"}  # pump.fun graduation burns LP; bonding curve has no LP

def fetch_dex(mints):
    """Return {mint: best (highest-liquidity) pair}."""
    out = {}
    for i in range(0, len(mints), 30):
        pairs = get_json(DEX_TOKENS.format(mints=",".join(mints[i:i+30]))) or []
        for p in pairs:
            if p.get("chainId") != "solana": continue
            m = (p.get("baseToken") or {}).get("address")
            if m not in mints: continue
            liq = ((p.get("liquidity") or {}).get("usd")) or 0
            ALL_PAIRS.setdefault(m, set()).add(p.get("pairAddress"))
            cur = out.get(m)
            if cur is None or liq > (((cur.get("liquidity") or {}).get("usd")) or 0):
                out[m] = p
    return out

def holder_metrics(rep):
    """Return (top10_pct, top1_pct, insiders, uniform_cluster_size).
    Percentages are recomputed from uiAmount / supply: RugCheck's `pct` can be stale
    (closed accounts with uiAmount 0 still report pct ~0.1)."""
    known = rep.get("knownAccounts") or {}
    excl = {a for a, v in known.items() if (v or {}).get("type") in ("AMM", "LP")}
    for mk in rep.get("markets") or []:
        excl.add(mk.get("pubkey"))
        for k in ("liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if isinstance(mk.get(k), str): excl.add(mk[k])
    th = rep.get("topHolders"); tok = rep.get("token") or {}
    supply, dec = tok.get("supply"), tok.get("decimals")
    if not th or not supply or dec is None: return None, None, [], 0
    ui_supply = supply / 10**dec
    non_lp = [h for h in th if h.get("address") not in excl and h.get("owner") not in excl
              and (h.get("uiAmount") or 0) > 0]
    pcts = sorted(((h.get("uiAmount") or 0) / ui_supply * 100 for h in non_lp), reverse=True)
    cluster = 0
    if pcts:
        med = sorted(pcts)[len(pcts)//2]
        cluster = sum(1 for x in pcts if abs(x - med) <= UNIFORM_CLUSTER_TOL * med)
    return sum(pcts[:10]), (pcts[0] if pcts else 0.0), [h for h in non_lp if h.get("insider")], cluster

def token2022_traps(rep, M):
    R = []
    ext = rep.get("token_extensions") or {}
    M["tokenProgram"] = rep.get("tokenProgram")
    tf = rep.get("transferFee") or {}
    cfg = ext.get("transferFeeConfig") or {}
    fee_pct = tf.get("pct") or 0
    bps = 0
    for k in ("newerTransferFee", "olderTransferFee"):
        bps = max(bps, int(((cfg.get(k) or {}).get("transferFeeBasisPoints")) or 0))
    M["transfer_fee_pct"] = max(fee_pct, bps / 100)
    if fee_pct > 0 or bps > 0: R.append(f"token2022: transfer fee {max(fee_pct, bps/100)}%")
    th = ext.get("transferHook")
    if th and (not isinstance(th, dict) or th.get("programId") or th.get("program_id")):
        R.append("token2022: transfer hook")
    if ext.get("permanentDelegate"): R.append("token2022: permanent delegate")
    if ext.get("nonTransferable"): R.append("token2022: non-transferable")
    das = ext.get("defaultAccountState")
    if das and "frozen" in json.dumps(das).lower(): R.append("token2022: default account state frozen")
    return R

def sell_quote(mint, rep, pair, M):
    rep = rep or {}
    """Quote selling ~$SELL_QUOTE_USD of token into SOL. Returns list of kill reasons."""
    try:
        dec = ((rep or {}).get("token") or {}).get("decimals")
        if dec is None: dec = M.get("oc_decimals")
        pu, pn = float(pair.get("priceUsd")), float(pair.get("priceNative"))
        if (pair.get("quoteToken") or {}).get("address") != SOL: raise ValueError("non-SOL quote pair")
        sol_usd = pu / pn
        amount = int(SELL_QUOTE_USD / pu * 10**dec)
        expected = SELL_QUOTE_USD / sol_usd * 1e9
    except Exception as e:
        return [f"missing data: sell quote inputs ({e})"]
    params = dict(inputMint=mint, outputMint=SOL, amount=amount, slippageBps=SELL_SLIPPAGE_BPS)
    reached = False
    for u in JUP_QUOTE_URLS:
        try:
            r = requests.get(u, params=params, timeout=15)
        except Exception:
            continue
        reached = True
        try: q = r.json()
        except Exception: q = {}
        if r.status_code != 200 or "outAmount" not in q:
            M["sell_quote_error"] = str(q.get("error") or q.get("errorCode") or r.status_code)[:120]
            continue
        impact = float(q.get("priceImpactPct") or 0) * 100
        frac = int(q["outAmount"]) / expected
        M.update(sell_impact_pct=impact, sell_return_frac=frac, sell_quote_src=u)
        R = []
        if impact > MAX_SELL_IMPACT_PCT: R.append(f"sell impact {impact:.1f}% > {MAX_SELL_IMPACT_PCT}%")
        if frac < MIN_SELL_RETURN_FRAC: R.append(f"sell returns {frac*100:.0f}% of expected")
        return R
    return ["sell quote: no route/error"] if reached else ["missing data: sell quote"]

def evaluate_stage_a(mint, rep, pair):
    R, M = [], {"warns": []}
    if rep is None:
        M["warns"].append("rugcheck unavailable")
        dexid = (pair or {}).get("dexId")
        if dexid in LP_BURNED_DEXES: M["lp_status"] = f"burned ({dexid} graduation / bonding curve)"
        else:
            M["lp_status"] = "unverified"
            M["warns"].append(f"LP lock unverified on-chain ({dexid})")
    else:
        tok = rep.get("token") or {}
        score, norm = rep.get("score"), rep.get("score_normalised")
        M.update(score=score, score_normalised=norm)
        if score is None or norm is None: R.append("missing data: rugcheck score")
        elif norm >= MAX_SCORE_NORM or score > MAX_SCORE_RAW:
            R.append(f"high risk score (raw={score}, norm={norm})")
        # LP locked (pump.fun bonding curve reports lpLockedPct=100 meaninglessly; still keep check)
        lp = [ (mk.get("lp") or {}).get("lpLockedPct") for mk in rep.get("markets") or [] ]
        lp = [x for x in lp if x is not None]
        M["lpLockedPct"] = max(lp) if lp else None
        if not lp: R.append("missing data: lpLockedPct")
        elif max(lp) < MIN_LP_LOCKED_PCT: R.append(f"LP locked only {max(lp):.1f}%")
        for risk in rep.get("risks") or []:
            lvl, name = (risk.get("level") or "").lower(), risk.get("name", "unknown")
            if lvl == "danger": R.append(f"danger: {name}")
            elif lvl == "warn" and "mutable" not in name.lower(): R.append(f"warn: {name}")
        ma = rep.get("mintAuthority", tok.get("mintAuthority"))
        fa = rep.get("freezeAuthority", tok.get("freezeAuthority"))
        M.update(mintAuthority=ma, freezeAuthority=fa)
        if ma: R.append("mint authority not null")
        if fa: R.append("freeze authority not null")
        top10, top1, ins, cluster = holder_metrics(rep)
        M.update(top10_pct=top10, top_holder_pct=top1, uniform_cluster=cluster)
        if cluster >= UNIFORM_CLUSTER_MIN: M["cluster_flag"] = True  # judged by bundle analysis
        if top10 is None: R.append("missing data: topHolders")
        else:
            if top10 > MAX_TOP10_PCT: R.append(f"top10 non-LP holders {top10:.1f}% > {MAX_TOP10_PCT}%")
            if top1 > MAX_SINGLE_HOLDER_PCT: R.append(f"single holder {top1:.1f}% > {MAX_SINGLE_HOLDER_PCT}%")
        if ins: R.append(f"{len(ins)} top holder(s) flagged insider")
        gi = rep.get("graphInsidersDetected")
        M["graphInsidersDetected"] = gi
        if gi is None: R.append("missing data: graphInsidersDetected")
        elif gi > 0: R.append(f"graph-detected insiders: {gi}")
        if rep.get("insiderNetworks"): R.append("insider networks detected")
        supply = tok.get("supply"); cb = rep.get("creatorBalance")
        cpct = (cb / supply * 100) if (cb is not None and supply) else None
        M["creator_pct"] = cpct
        if cpct is None: R.append("missing data: creator balance")
        elif cpct > MAX_CREATOR_PCT and cb > 1:  # ignore dust (1 raw unit)
            R.append(f"creator still holds {cpct:.3f}%")
        if rep.get("rugged"): R.append("rugcheck: rugged")
        R += token2022_traps(rep, M)
        M["rc_liquidity_usd"] = rep.get("totalMarketLiquidity")
        M["launchpad"] = (rep.get("launchpad") or {}).get("platform")
    if pair is None: R.append("missing data: dexscreener pair")
    else:
        liq = (pair.get("liquidity") or {}).get("usd")
        created = pair.get("pairCreatedAt")
        age = (time.time()*1000 - created)/60000 if created else None
        vol = (pair.get("volume") or {}).get("h24")
        tx = (pair.get("txns") or {}).get("h24") or {}
        b, s = tx.get("buys"), tx.get("sells")
        fdv = pair.get("fdv")
        M.update(symbol=(pair.get("baseToken") or {}).get("symbol"), name=(pair.get("baseToken") or {}).get("name"), dex=pair.get("dexId"), liquidity_usd=liq, pair_age_min=age, volume_h24=vol,
                 buys_h24=b, sells_h24=s, fdv=fdv)
        if liq is None: R.append("missing data: liquidity (dex)")
        elif liq < MIN_LIQUIDITY_USD: R.append(f"liquidity ${liq:,.0f} < ${MIN_LIQUIDITY_USD:,}")
        if age is None: R.append("missing data: pairCreatedAt")
        elif age < MIN_PAIR_AGE_MIN: R.append(f"pair age {age:.0f}m < {MIN_PAIR_AGE_MIN}m")
        if vol is None: R.append("missing data: volume h24")
        elif vol < MIN_VOLUME_24H: R.append(f"24h volume ${vol:,.0f} < ${MIN_VOLUME_24H:,}")
        if b is None or s is None: R.append("missing data: txns h24")
        else:
            ratio = s / b if b else float("inf")
            M["sell_buy_ratio_h24"] = ratio if b else None
            if ratio < MIN_SELL_BUY_RATIO and age is not None and age >= LOPSIDED_SOFT_MIN_AGE_H * 60 \
                    and ratio >= LOPSIDED_SOFT_MIN_RATIO:
                M["warns"].append(f"LOPSIDED WARN: h24 buys/sells {b}/{s} (ratio {ratio:.2f}, pair {age/60:.1f}h old)")
            elif not (MIN_SELL_BUY_RATIO <= ratio <= MAX_SELL_BUY_RATIO):
                R.append(f"lopsided h24 buys/sells {b}/{s}")
        t1 = (pair.get("txns") or {}).get("h1") or {}
        b1, s1 = t1.get("buys"), t1.get("sells")
        M.update(buys_h1=b1, sells_h1=s1)
        if b1 is None or s1 is None: R.append("missing data: txns h1")
        elif s1 == 0 and b1 > H1_ZERO_SELL_MAX_BUYS: R.append(f"h1 zero sells with {b1} buys")
        if fdv is None: R.append("missing data: fdv")
        elif liq:
            if fdv / liq > MAX_FDV_LIQ_RATIO: R.append(f"FDV/liq {fdv/liq:.0f} > {MAX_FDV_LIQ_RATIO}")
    R += onchain_checks(mint, rep, M)
    flat_warn(M)
    if M.get("cluster_flag") and R: M["bundle_status"] = "not analyzed (already killed)"
    return R, M

def flat_warn(M):
    """r9: FLAT WARN when the non-LP holder distribution is suspiciously flat (possible wallet farm). WARN only."""
    t10, t1 = M.get("oc_top10_pct"), M.get("oc_top_holder_pct")
    if not t1:   # on-chain holders missing (or empty): fall back to RugCheck's numbers
        t10, t1 = M.get("top10_pct"), M.get("top_holder_pct")
    if t10 is None or not t1: return
    if t10 < FLAT_WARN_MAX_TOP10_PCT and t1 < FLAT_WARN_MAX_TOP1_PCT:
        M["warns"].append(f"FLAT WARN: top10 {t10:.1f}% / top holder {t1:.2f}% (possible wallet farm; "
                          f"6/10 past cases dumped to ~0 with LP burned, 1/10 ran 24x) -- size small, take profit early")

def evaluate_stage_b(mint, rep, pair, M):
    """Expensive checks, stop at first kill. Order: OHLCV/dead pump, sell quote, funding trace, bundler, bundle."""
    steps = []
    if MODE["dead_pump"] and not MODE["bottoms"]: steps.append(lambda: dead_pump(pair, M))
    steps += [lambda: sell_quote(mint, rep, pair, M),
              lambda: cached("trace", mint, lambda: trace_verdict(mint, M), M, "trace"),
              lambda: cached("bundler", mint, lambda: bundler_verdict(mint, M), M, "bundler")]
    if M.get("cluster_flag"): steps.append(lambda: bundle_verdict(mint, pair, M))
    for st in steps:
        R = st()
        if R: return R
    return []

def evaluate(mint, rep, pair):
    R, M = evaluate_stage_a(mint, rep, pair)
    if not R: R = evaluate_stage_b(mint, rep, pair, M)
    return ("KILL" if R else "PASS"), R, M

WARN_TAGS = [("BUNDLER WARN", "bundler"), ("BUNDLE WARN", "bundle"), ("FUNDING WARN", "funding"),
             ("LOPSIDED WARN", "lopsided"), ("bundled launch", "bundled_launch"), ("FLAT WARN", "flat"),
             ("rugcheck unavailable", "rc_down"), ("LP lock unverified", "lp_unverified")]
DEGRADED_TAGS = {"rc_down", "lp_unverified"}   # a data source was skipped, not a risk signal by itself

def warn_tags(warns):
    return sorted({tag for w in warns or [] for pfx, tag in WARN_TAGS if w.startswith(pfx)})

def pass_tier(warns):
    """clean | warn | degraded | degraded+warn  (lets outcomes segment clean vs warn passes)."""
    t = set(warn_tags(warns)); risk, deg = t - DEGRADED_TAGS, t & DEGRADED_TAGS
    return "+".join(x for x, on in (("degraded", deg), ("warn", risk)) if on) or "clean"

def log_check(mint, verdict, reasons, metrics):
    metrics = dict(metrics or {}); metrics["rules_version"] = FILTER_VERSION
    metrics["warn_tags"] = warn_tags(metrics.get("warns"))
    rec = {"timestamp": datetime.now(timezone.utc).isoformat(), "mint": mint, "verdict": verdict, "reasons": reasons}
    if verdict == "PASS":
        metrics["pass_tier"] = pass_tier(metrics.get("warns"))
        rec["entry"] = metrics.get("entry") or "UNKNOWN"          # r10 entry timing (PASS only)
        rec["entry_reasons"] = metrics.get("entry_reasons") or []
    rec["metrics"] = metrics
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    if verdict in ("PASS", "KILL", "ERROR"):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(os.path.join(DATA_DIR, "checks.log"), "a") as f:
            f.write(f"{ts} | {verdict:<5} | {mint} | {'; '.join(reasons) if reasons else 'clean'}\n")
        try:
            import dashboard; dashboard.build()
        except Exception as e:
            print(f"  (dashboard update failed: {e})")

def tag(r):  # normalize reason for tally
    return r.split(":")[0] + ":" + r.split(":")[1].split("(")[0].strip() if r.startswith(("missing data", "danger", "warn")) \
        else "".join(c for c in r if not c.isdigit() and c not in "$,.%").split(">")[0].split("<")[0].strip()

# ---------------- GeckoTerminal (rate-limited + cached) ----------------
GECKO_PAGE_CACHE_TTL = 600     # candidate pages cached 10 min
GECKO_OHLCV_CACHE_TTL = 3600
GECKO_CACHE_PATH = os.path.join(DATA_DIR, "gecko_cache.json")
_gcache = None

def gecko_get(url, cache_ttl=None):
    global _gcache
    if _gcache is None:
        try: _gcache = json.load(open(GECKO_CACHE_PATH))
        except Exception: _gcache = {}
    ttl = cache_ttl if cache_ttl is not None else GECKO_OHLCV_CACHE_TTL
    hit = _gcache.get(url)
    if hit and time.time() - hit["t"] < ttl: return hit["j"]
    for t in range(5):
        wait = GECKO_DELAY - (time.time() - _last_gecko[0])
        if wait > 0: time.sleep(wait)
        try:
            r = requests.get(url, timeout=20); _last_gecko[0] = time.time()
            if r.status_code == 429:
                time.sleep(6 * 2 ** t); continue   # 6, 12, 24, 48s
            r.raise_for_status(); j = r.json()
            with _cache_lock: _gcache[url] = {"t": time.time(), "j": j}
            _gcache = {k: v for k, v in _gcache.items() if time.time() - v["t"] < 3600}
            json.dump(_gcache, open(GECKO_CACHE_PATH, "w"))
            return j
        except Exception:
            _last_gecko[0] = time.time(); time.sleep(2 * (t + 1))
    print(f"  gecko fetch failed: {url[:90]}"); return None

# ---------------- price-action layer ----------------
DEAD_PUMP_DD = 0.60            # default scans: kill if down >60% from ATH and still bleeding/fading
BOTTOM_DD_MIN, BOTTOM_DD_MAX = 0.60, 0.95
BOTTOM_MIN_HOURS_SINCE_LOW = 12
BOTTOM_MIN_VOL24 = 20_000
BOTTOM_MAX_SELL_BUY = 1.1      # sells/buys h24 <= 1.1 = balanced or buyer-leaning
BOTTOM_MIN_AGE_D, BOTTOM_MAX_AGE_D = 1, 14
BOTTOM_FRESH_MIN_AGE_H = 6     # fresh-feed tokens also eligible for --bottoms once >= 6h old
SOURCE = {}                    # mint -> "new" | "older"
OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour?limit=1000"
_last_gecko = [0.0]
OHLCV_MIN_HOURLY = 12
OHLCV_15M_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute?aggregate=15&limit=1000"

def ohlcv_hourly(pool):
    wait = GECKO_DELAY - (time.time() - _last_gecko[0])
    if wait > 0: time.sleep(wait)
    j = gecko_get(OHLCV_URL.format(pool=pool))
    try: rows = j["data"]["attributes"]["ohlcv_list"]
    except Exception: return None
    return sorted(rows)  # oldest first: [ts, o, h, l, c, v]

def price_action(pair, M):
    """Compute price-action metrics into M. Returns False if data missing."""
    rows = ohlcv_hourly(pair.get("pairAddress")) if pair else None
    if rows is not None and len(rows) < OHLCV_MIN_HOURLY:  # young pool: fall back to 15m candles
        j = gecko_get(OHLCV_15M_URL.format(pool=pair.get("pairAddress")))
        try: rows = sorted(j["data"]["attributes"]["ohlcv_list"]); M["ohlcv_tf"] = "15m"
        except Exception: rows = None
    if not rows or len(rows) < 8: return False
    now = time.time()
    ath_i = max(range(len(rows)), key=lambda i: rows[i][2]); ath = rows[ath_i][2]
    cur = rows[-1][4]
    post = rows[ath_i:]
    lo_row = min(post, key=lambda r: r[3]); floor = lo_row[3]
    def win(a, b):  # candles with age in hours [a, b)
        return [r for r in rows if a <= (now - r[0]) / 3600 < b]
    hist_h = (now - rows[0][0]) / 3600
    w = max(0.5, min(6.0, hist_h / 2))   # young pools: compare last half vs first half of history
    l6, p6 = win(0, w), win(w, 2 * w)
    pa = dict(ath_price=ath, cur_price=cur, drawdown=1 - cur / ath if ath else None,
              floor_price=floor, hours_since_low=(now - lo_row[0]) / 3600,
              low_last6=min((r[3] for r in l6), default=None), low_prev6=min((r[3] for r in p6), default=None),
              vol_last6=sum(r[5] for r in l6), vol_prev6=sum(r[5] for r in p6), ohlcv_hours=len(rows),
              window_h=w, history_h=hist_h)
    pa["vol6_change_pct"] = ((pa["vol_last6"] / pa["vol_prev6"] - 1) * 100) if pa["vol_prev6"] else None
    price, mc = pair.get("priceUsd"), pair.get("marketCap") or pair.get("fdv")
    try:
        units = float(mc) / float(price)
        pa.update(cur_mcap=float(mc), ath_mcap=ath * units)
    except Exception: pa.update(cur_mcap=mc, ath_mcap=None)
    M.update(pa); return True

def dead_pump(pair, M):
    if not price_action(pair, M): return ["missing data: ohlcv"]
    if M["drawdown"] is not None and M["drawdown"] > DEAD_PUMP_DD:
        lower = M["low_last6"] is not None and M["low_prev6"] is not None and M["low_last6"] < M["low_prev6"]
        fading = M["vol_last6"] < M["vol_prev6"]
        if lower or fading:
            return [f"dead pump: -{M['drawdown']*100:.0f}% from ATH, " + ("lower lows" if lower else "volume declining")]
    return []

# ---------------- r10 entry timing (label on PASS only; never changes the safety verdict) ----------------
# A PASS says "probably not a rug". It says nothing about WHERE you buy. r10 adds entry = OK | WAIT | UNKNOWN:
#   WAIT  if price >= ENTRY_NEAR_ATH_FRAC x ATH (buying the top, e.g. MCRIBINU passed -10% from ATH)
#   WAIT  if the 15m (or 5m) chart has no held higher low (still sliding, e.g. ODIE passed -54% and went to -80%)
#   OK    only if neither fires; UNKNOWN if candles are missing.
# Swing low (fractal): candle i is a swing low if its low is strictly below the lows of the K candles before it
# and <= the lows of the K candles after it (so only CONFIRMED lows count; the newest K candles can't be one).
# "Held higher low" on a timeframe = within the window there are >= 2 swing lows, the latest is above the one
# before it, no candle after the latest swing low has traded below it, and the current price is above it.
ENTRY_NEAR_ATH_FRAC = 0.75        # price >= 0.75x ATH (within 25% of the high) -> WAIT
ENTRY_TFS = [                     # (label, aggregate minutes, window hours, fractal K)
    ("15m", 15, 6.0, 2),          # last 6h = 24 candles, swing low = lowest of a 5-candle (75 min) span
    ("5m", 5, 3.0, 3),            # last 3h = 36 candles, swing low = lowest of a 7-candle (35 min) span
]
ENTRY_OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute?aggregate={agg}&limit={lim}"
ENTRY_CACHE_TTL = 300             # entry timing needs fresh candles: 5 min cache

def swing_lows(rows, k):
    """Indices of confirmed fractal swing lows in rows ([ts, o, h, l, c, v], oldest first)."""
    out = []
    for i in range(k, len(rows) - k):
        lo = rows[i][3]
        if all(lo < rows[j][3] for j in range(i - k, i)) and all(lo <= rows[j][3] for j in range(i + 1, i + k + 1)):
            out.append(i)
    return out

def higher_low_state(rows, k, cur):
    """(held: bool, reason: str, detail: dict) for one timeframe."""
    if len(rows) < 2 * k + 3: return None, "too few candles", {"candles": len(rows)}
    sw = swing_lows(rows, k)
    lows = [rows[i][3] for i in sw]
    d = {"candles": len(rows), "swing_lows": [round(x, 12) for x in lows[-3:]]}
    win_lo_i = min(range(len(rows)), key=lambda i: rows[i][3])
    if win_lo_i >= len(rows) - k:
        return False, "still making lower lows (new low in the last candles)", d
    if len(sw) < 2: return False, "no confirmed higher low yet", d
    last_i, last, prev = sw[-1], lows[-1], lows[-2]
    after = min(r[3] for r in rows[last_i + 1:])
    d.update(last_low=last, prev_low=prev, cur=cur)
    if last <= prev: return False, "still making lower lows", d
    if after < last: return False, "broke below its latest higher low", d
    if cur is not None and cur <= last: return False, "price back at/below latest low", d
    return True, f"higher low held (+{(last / prev - 1) * 100:.0f}% over prior low)", d

def entry_timing(pair, M):
    """Set M['entry'] (OK/WAIT/UNKNOWN), M['entry_reasons'], M['entry_detail']. Never raises."""
    try:
        return _entry_timing(pair, M)
    except Exception as e:
        M.update(entry="UNKNOWN", entry_reasons=[f"entry check error: {str(e)[:80]}"]); return M["entry"]

def _entry_timing(pair, M):
    pool = (pair or {}).get("pairAddress")
    if not pool:
        M.update(entry="UNKNOWN", entry_reasons=["no pool for OHLCV"]); return M["entry"]
    if M.get("ath_price") is None: price_action(pair, M)   # hourly ATH (already done by dead_pump in default scans)
    try: cur = float(pair.get("priceUsd"))
    except Exception: cur = M.get("cur_price")
    tf_rows = {}
    for lab, agg, win_h, k in ENTRY_TFS:
        lim = int(win_h * 60 / agg) + 2 * k + 2   # a few spare candles; only the window itself is judged
        j = gecko_get(ENTRY_OHLCV_URL.format(pool=pool, agg=agg, lim=lim), cache_ttl=ENTRY_CACHE_TTL)
        try:
            rows = sorted(j["data"]["attributes"]["ohlcv_list"])
            cut = time.time() - win_h * 3600
            tf_rows[lab] = (rows, [r for r in rows if r[0] >= cut], k)
        except Exception:
            tf_rows[lab] = None
    highs = [M["ath_price"]] if M.get("ath_price") else []
    highs += [max(r[2] for r in v[0]) for v in tf_rows.values() if v and v[0]]
    ath = max(highs) if highs else None
    if cur is None and tf_rows.get("15m"): cur = tf_rows["15m"][0][-1][4]
    W, notes, det = [], [], {"ath": ath, "cur": cur}
    dd = (1 - cur / ath) if (ath and cur) else None
    det["drawdown"] = dd
    if dd is not None and cur >= ENTRY_NEAR_ATH_FRAC * ath:
        W.append(f"near ATH: -{max(dd, 0) * 100:.0f}% from high")
    held_tfs = []
    for lab, agg, win_h, k in ENTRY_TFS:
        v = tf_rows.get(lab)
        if not v or not v[1]:
            det[lab] = "unavailable"; continue
        held, why, d = higher_low_state(v[1], k, cur)
        det[lab] = dict(d, held=held, why=why)
        if held is False: W.append(f"{lab}: {why}")
        elif held: held_tfs.append(lab); notes.append(f"{lab} {why}")
    M["entry_detail"] = det
    if W:
        M.update(entry="WAIT", entry_reasons=W)
    elif dd is None or "15m" not in held_tfs:
        miss = [lab for lab, *_ in ENTRY_TFS if not isinstance(det.get(lab), dict)]
        M.update(entry="UNKNOWN", entry_reasons=["OHLCV unavailable: " + (", ".join(miss) or "ATH") +
                                                 (" (no WAIT signal on the rest)" if held_tfs else "")])
    else:
        extra = "" if "5m" in held_tfs else " (5m unavailable)"
        M.update(entry="OK", entry_reasons=[f"pulled back -{dd * 100:.0f}% and holding higher low{extra}"] + notes)
    return M["entry"]

def prev_log(mint):
    """Most recent earlier log metrics for this mint (for holder/liquidity trend)."""
    last = None
    try:
        with open(LOG_PATH) as f:
            for l in f:
                if mint in l:
                    try:
                        d = json.loads(l)
                        if d.get("mint") == mint: last = d
                    except Exception: pass
    except FileNotFoundError: pass
    return last

def bottom_check(mint, rep, pair, M):
    """Returns (is_candidate, reasons)."""
    R = []
    prev = prev_log(mint)
    if not price_action(pair, M): return False, ["missing data: ohlcv"]
    dd = M["drawdown"]
    if dd is None or not (BOTTOM_DD_MIN <= dd <= BOTTOM_DD_MAX):
        R.append(f"drawdown {dd*100:.0f}% outside {BOTTOM_DD_MIN*100:.0f}-{BOTTOM_DD_MAX*100:.0f}%" if dd is not None else "missing data: drawdown")
    if M["hours_since_low"] < BOTTOM_MIN_HOURS_SINCE_LOW:
        R.append(f"still bleeding: new low {M['hours_since_low']:.0f}h ago")
    fl = M["floor_price"]
    if M["low_last6"] is None or M["low_prev6"] is None: R.append("missing data: recent lows")
    elif not (M["low_last6"] > fl and M["low_prev6"] > fl and M["low_last6"] >= M["low_prev6"]):
        R.append("no higher lows")
    if not M["vol_prev6"] or M["vol_last6"] <= M["vol_prev6"]: R.append("volume not recovering (6h <= prior 6h)")
    v24 = M.get("volume_h24")
    if v24 is None or v24 < BOTTOM_MIN_VOL24: R.append(f"dead volume: 24h ${v24 or 0:,.0f} < ${BOTTOM_MIN_VOL24:,}")
    b, s_ = M.get("buys_h24"), M.get("sells_h24")
    if not b or s_ is None or s_ / b > BOTTOM_MAX_SELL_BUY: R.append(f"sellers dominate ({b}/{s_} buys/sells)")
    # holder count trend: rugcheck totalHolders vs previous log
    th = (rep or {}).get("totalHolders"); M["total_holders"] = th
    ph = ((prev or {}).get("metrics") or {}).get("total_holders")
    M["holder_trend"] = "unknown" if (th is None or ph is None) else ("growing" if th > ph else "not growing")
    if M["holder_trend"] == "not growing": R.append(f"holders not growing ({ph}->{th})")
    pl = ((prev or {}).get("metrics") or {}).get("liquidity_usd"); cl = M.get("liquidity_usd")
    M["liquidity_trend"] = "unknown" if (pl is None or cl is None) else ("draining" if cl < 0.9 * pl else "stable")
    if M["liquidity_trend"] == "draining": R.append(f"liquidity draining (${pl:,.0f}->${cl:,.0f})")
    return (not R), R

MODE = {"dead_pump": True, "bottoms": False}

def run(mints, quiet=False):
    t0 = time.time()
    if not RC_STATE.get("checked"):
        RC_STATE["checked"] = True
        ok = rugcheck_health()
        print(f"RugCheck health: {'up' if ok else 'DOWN - skipping for this scan (warn only)'}")
    dex = fetch_dex(mints); prefetch_mints(mints)
    t1 = time.time(); print(f"[stage 0] DexScreener batch + mint prefetch: {t1-t0:.1f}s")
    def stage_a(m):
        rep = rugcheck_report(m)
        R, M = evaluate_stage_a(m, rep, dex.get(m))
        return m, rep, R, M
    with ThreadPoolExecutor(STAGE_A_WORKERS) as ex: A = list(ex.map(stage_a, mints))
    t2 = time.time(); surv_a = [x for x in A if not x[2]]
    print(f"[stage A] cheap checks on {len(mints)} tokens ({STAGE_A_WORKERS} threads): {t2-t1:.1f}s, survivors {len(surv_a)}")
    results = {}
    for m, rep, R, M in A:
        if R: results[m] = ("KILL", R, M)
    for m, rep, R, M in surv_a:
        tb = time.time()
        R = evaluate_stage_b(m, rep, dex.get(m), M); v = "KILL" if R else "PASS"
        if v == "PASS" and MODE["bottoms"]:
            ok, BR = bottom_check(m, rep, dex.get(m), M)
            M["bottom_verdict"] = "BOTTOM CANDIDATE" if ok else "NOT BOTTOM"
            if not ok: v, R = "NOT_BOTTOM", ["bottom: " + r for r in BR]
        if v == "PASS":
            et = entry_timing(dex.get(m), M)   # r10: entry-timing label, does not change the verdict
            print(f"  entry {et}: {'; '.join(M.get('entry_reasons') or [])}")
        M["stage_b_secs"] = round(time.time() - tb, 1)
        results[m] = (v, R, M)
    t3 = time.time(); print(f"[stage B] expensive checks on {len(surv_a)} tokens: {t3-t2:.1f}s")
    tally, survivors = Counter(), []
    for i, m in enumerate(mints, 1):
        v, R, M = results[m]
        if M.get("bundle_status") == "BUNDLE WARN":
            b = M["bundle"]; mu = b.get("multiple")
            print(f"  BUNDLE WARN {m}: {b['wallets']} wallets hold {b['held_pct']:.2f}% (sold {b.get('sold_pct') or 0:.0f}%), "
                  f"multiple {('%.2fx' % mu) if mu else 'unknown'}")
        M["source"] = SOURCE.get(m)
        log_check(m, v, R, M)
        for r in R: tally[tag(r)] += 1
        if v == "PASS": survivors.append((m, M))
        if not quiet or v == "PASS":
            print(f"[{i}/{len(mints)}] {m} -> {v}")
            for r in R: print(f"     - {r}")
    cache_flush()
    print(f"[total] {time.time()-t0:.1f}s")
    return survivors, tally

OLDER_MIN_AGE_H, OLDER_MAX_AGE_H = 2, 48
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/solana/"
GECKO_LISTS = ["trending_pools", "new_pools", "pools?sort=h24_volume_usd_desc"]
GECKO_PAGES = 5
GECKO_DELAY = 3.0   # free tier ~30 req/min; keep well under
DEX_SEARCH_QUERIES = ["pumpswap SOL", "raydium SOL", "meteora SOL"]
SKIP_MINTS = {SOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}

def fresh_mints():
    seen = []
    for u in DEX_FEEDS:
        for t in get_json(u) or []:
            a = t.get("tokenAddress")
            if t.get("chainId") == "solana" and a and a not in seen: seen.append(a)
    return seen

def older_mints(min_h=None, max_h=None):
    """Graduated tokens 2-48h old with liquidity >= MIN_LIQUIDITY_USD (prefiltered via DexScreener)."""
    raw, src = [], Counter()
    for lst in GECKO_LISTS:
        for pg in range(1, GECKO_PAGES + 1):
            sep = "&" if "?" in lst else "?"
            j = gecko_get(f"{GECKO_BASE}{lst}{sep}page={pg}", cache_ttl=GECKO_PAGE_CACHE_TTL)
            for pool in (j or {}).get("data") or []:
                for side in ("base_token", "quote_token"):
                    tid = (((pool.get("relationships") or {}).get(side) or {}).get("data") or {}).get("id", "")
                    m = tid.replace("solana_", "")
                    if m and m not in SKIP_MINTS and m not in raw:
                        raw.append(m); src["geckoterminal"] += 1
    for q in DEX_SEARCH_QUERIES:
        for pr in (get_json("https://api.dexscreener.com/latest/dex/search?q=" + requests.utils.quote(q)) or {}).get("pairs") or []:
            m = (pr.get("baseToken") or {}).get("address")
            if pr.get("chainId") == "solana" and m and m not in SKIP_MINTS and m not in raw:
                raw.append(m); src["dexscreener search"] += 1
    dex = fetch_dex(raw); now = time.time() * 1000; keep = []
    for m in raw:
        p = dex.get(m) or {}
        liq = (p.get("liquidity") or {}).get("usd") or 0
        c = p.get("pairCreatedAt")
        lo, hi = (min_h or OLDER_MIN_AGE_H), (max_h or OLDER_MAX_AGE_H)
        if c and lo * 60 <= (now - c) / 60000 <= hi * 60 and liq >= MIN_LIQUIDITY_USD:
            keep.append(m)
    print(f"Older-source raw candidates: {len(raw)} ({dict(src)}); after age & liq>=${MIN_LIQUIDITY_USD:,} prefilter ({min_h or OLDER_MIN_AGE_H}-{max_h or OLDER_MAX_AGE_H}h): {len(keep)}")
    return keep, {m: dex[m] for m in keep}

def report(mints, label):
    print(f"Filter rules {FILTER_VERSION}. Scanning {len(mints)} {label} Solana mints...")
    surv, tally = run(mints, quiet=True)
    print(f"\nChecked {len(mints)} | PASS {len(surv)} | KILL {len(mints)-len(surv)}")
    for m, M in surv:
        b = M.get("bundle") or {}
        bw = ""
        print(f"  PASS [{M.get('source')}] {m} {M.get('symbol','')}{bw} liq=${M.get('liquidity_usd'):,.0f} age={M.get('pair_age_min')/60:.1f}h "
              f"top10_oc={M.get('oc_top10_pct'):.1f}% sell_impact={M.get('sell_impact_pct'):.2f}% return={M.get('sell_return_frac',0)*100:.0f}%")
        for w in M.get("warns") or []: print(f"      WARN: {w}")
        print(f"      ENTRY {M.get('entry') or 'UNKNOWN'}: {'; '.join(M.get('entry_reasons') or [])}")
    print(f"\nRugCheck: ok {RC_STATE['ok']} | failed {RC_STATE['fails']} | skipped (down) {RC_STATE['skipped']}")
    print("\nKill-reason tally:")
    for k, c in tally.most_common(): print(f"  {c:3d}  {k}")

def _publish_dashboard():
    try:
        import dashboard; dashboard.build()
        print("Dashboard:", dashboard.publish() if not os.environ.get("FD_NO_PUSH") else "built (CI commits separately)")
    except Exception as e:
        print(f"(dashboard publish failed: {e})")

if __name__ == "__main__":
    import atexit; atexit.register(_publish_dashboard)
    if not os.environ.get("FD_NO_PUSH"):
        try:
            import dashboard; print("Sync:", dashboard.git_sync_pull())
        except Exception as e: print(f"(git pull failed: {e})")
    a = sys.argv[1:]
    if not a: a = ["--scan-all"]  # default: newest + older
    n = int(a[1]) if len(a) > 1 and a[1].isdigit() else SCAN_DEFAULT_N
    if a[0] == "--scan":
        f = fresh_mints(); report(f[:n], f"fresh ({len(f)} found)")
    elif a[0] == "--scan-older":
        o, _ = older_mints(); report(o[:n], "older (2-48h)")
    elif a[0] == "--bundlers":
        for m in a[1:]:
            fetch_dex([m]); M = {"warns": []}
            onchain_checks(m, None, M)
            R = bundler_verdict(m, M); b = M.get("bundler") or {}
            v = "KILL" if R else ("WARN" if any(w.startswith("BUNDLER") for w in M["warns"]) else "PASS")
            print(m, json.dumps({k: v_ for k, v_ in b.items() if k != "top_groups"}))
            for g in b.get("top_groups", []): print(f"   slot {g['slot']}: {g['wallets']} wallets hold {g['held_pct']:.2f}%")
            print("   BUNDLER VERDICT:", v, R)
            log_check(m, "BUNDLERS " + v, R, M)
    elif a[0] == "--trace":
        m = a[1]; pair = fetch_dex([m]).get(m)
        rep = rugcheck_report(m); M = {"warns": []}
        if rep: holder_metrics(rep)
        Rc = onchain_checks(m, rep, M)
        R = trace_verdict(m, M); tr = M.get("trace") or {}
        print(f"{m} {(pair or {}).get('baseToken',{}).get('symbol','')}  rugcheck={'ok' if rep else 'unavailable'}")
        print(json.dumps({k: v for k, v in tr.items() if k != "cluster_detail"}, indent=1))
        for c in tr.get("cluster_detail", []):
            print(f"  cluster: {c['wallets']} wallets, {c['pct']:.2f}% supply, funders {[f[:6]+'…' for f in c['funders']]}")
        v = "KILL" if R else ("WARN" if any(w.startswith("FUNDING WARN") for w in M["warns"]) else "PASS")
        print("TRACE VERDICT:", v, R or [w for w in M["warns"] if w.startswith("FUNDING")])
        log_check(m, "TRACE " + v, R, M)
    elif a[0] == "--bundle":
        m = a[1]; pair = fetch_dex([m]).get(m)
        rep = rugcheck_report(m)
        M = {}
        if rep: holder_metrics(rep)
        onchain_checks(m, rep, M)
        print(f"Cluster size (on-chain): {M.get('oc_uniform_cluster')}  wallets matched: {len(M.get('cluster_wallets') or [])}"
              + ("" if rep else "  [rugcheck report missing: pool may not be excluded]"))
        R = bundle_verdict(m, pair, M); b = M.get("bundle") or {}
        print(json.dumps({k: v for k, v in b.items() if k != "per_wallet"}, indent=1, default=str))
        for r in b.get("per_wallet", []):
            print(f"  {r['owner'][:6]}… cur={r['cur']:,.0f} init={r['init']:,.0f} sold={r['sold_pct'] or 0:.0f}% "
                  f"entry_sol={r['entry_sol'] if r['entry_sol'] is None else '%.3e' % r['entry_sol']} transfer_funded={r['funded_by_transfer']} txs={r['txs']}")
        print("VERDICT:", M.get("bundle_status") or "KILL", R)
        log_check(m, M.get("bundle_status") or "KILL", R, M)
    elif a[0] == "--bottoms":
        MODE.update(bottoms=True, dead_pump=False)
        o, _ = older_mints(BOTTOM_MIN_AGE_D * 24, BOTTOM_MAX_AGE_D * 24)
        f = [m for m in fresh_mints() if m not in o]
        fd, now = fetch_dex(f), time.time() * 1000
        fk = [m for m in f if (fd.get(m) or {}).get("pairCreatedAt") and
              (now - fd[m]["pairCreatedAt"]) / 3.6e6 >= BOTTOM_FRESH_MIN_AGE_H and
              ((fd[m].get("liquidity") or {}).get("usd") or 0) >= MIN_LIQUIDITY_USD]
        print(f"Fresh-feed tokens >= {BOTTOM_FRESH_MIN_AGE_H}h & liq>=$15k added: {len(fk)}")
        SOURCE.update({m: "older" for m in o}); SOURCE.update({m: "new" for m in fk})
        o = o + fk
        surv, tally = run(o[:n], quiet=True)
        print(f"\nChecked {len(o[:n])} | BOTTOM CANDIDATES {len(surv)}")
        for m, M in surv:
            print(f"  BOTTOM {m} {M.get('symbol')} athMC=${M.get('ath_mcap') or 0:,.0f} MC=${M.get('cur_mcap') or 0:,.0f} "
                  f"dd={M['drawdown']*100:.0f}% since_low={M['hours_since_low']:.0f}h vol6chg={M.get('vol6_change_pct') or 0:+.0f}% "
                  f"liq=${M.get('liquidity_usd'):,.0f} holders={M.get('holder_trend')}")
        for m, M in surv: print(f"    source={M.get('source')}")
        print(f"\nRugCheck: ok {RC_STATE['ok']} | failed {RC_STATE['fails']}")
        print("\nRejection tally:")
        for k, c in tally.most_common(): print(f"  {c:3d}  {k}")
    elif a[0] == "--entry":   # r10 entry timing only (DexScreener + GeckoTerminal, no Helius, nothing logged)
        for m in a[1:]:
            pair = fetch_dex([m]).get(m); M = {}
            et = entry_timing(pair, M)
            print(f"{m} {((pair or {}).get('baseToken') or {}).get('symbol', '')} ENTRY {et}: {'; '.join(M.get('entry_reasons') or [])}")
            print("   " + json.dumps(M.get("entry_detail"), default=str))
    elif a[0] == "--scan-all":
        f = fresh_mints(); o, _ = older_mints()
        allm = list(dict.fromkeys(o + f))
        SOURCE.update({m: "new" for m in f}); SOURCE.update({m: "older" for m in o})
        report(allm[:n], f"combined (older first; {len(allm)} unique)")
    else: run(a)
