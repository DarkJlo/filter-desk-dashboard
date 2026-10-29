#!/usr/bin/env python3
"""Entry watch: re-check the r10 entry label on recent WAIT / UNKNOWN passes and alert when one flips to ENTRY OK.

Run hourly by .github/workflows/entry_watch.yml (no Helius, no keys, no signing):
  python scanner/entry_watch.py              # check, update data/entry_watch.json, open GitHub issues on new flips
  python scanner/entry_watch.py --dry-run    # same checks, writes nothing, prints the issue it would open
  python scanner/entry_watch.py --test-issue # also open + immediately close one "[TEST]" issue (notification test)

Watch list = latest PASS per mint in the last WATCH_HOURS whose entry label is WAIT or UNKNOWN.
Skipped: a KILL logged for the mint after that PASS (status "killed"), or DexScreener liquidity < MIN_LIQ ("low_liq",
re-checked next hour). Each watched mint is re-checked with filter_desk.entry_timing (DexScreener + GeckoTerminal).
First time the label is OK -> status "flipped", flipped_at + fdv/liq stamped, one GitHub issue (label entry-ok,
@mentions the owner so GitHub emails / pushes him). Issues are opened with the workflow's GITHUB_TOKEN; without a
token the alert is only printed. Open entry-ok issues older than ISSUE_MAX_AGE_D days are closed automatically.

r10.3 gate (risk flags, not just price structure): before any flip issue goes out, the latest PASS and the log for
the mint are checked (gate()). The issue is NEVER opened, and the record gets status "suppressed" + gate reasons
(printed as a SUPPRESSED line in the job output and shown on the dashboard), when:
  - the latest PASS is in the r10.4 HIGH trader tier: WARN tag trader_cluster_funder, or its top_traders summary has
    >= 5 of the top traders sharing one non-CEX funder (any wallet age), or a legacy trader_cluster tag whose stored
    numbers show that (legacy tag without numbers = HIGH), or
  - the mint has >= MAX_PRIOR_KILLS (3) KILL verdicts logged before that PASS (repeat-killed token).
If the PASS has no top-traders summary (older rules or a failed check) and HELIUS_API_KEY is set, the top-traders
check is run once at flip time (~151 Helius credits, only for that flip) and gated the same way.
r10.4: the LOW tier (trader_fresh: >= 8 fresh wallets < 7d, no shared funder, e.g. DESSA) does NOT block; the issue
opens with a "fresh buyers" flag in the title and the note in the body.
Flips that do go out carry the flags in the body (top-traders line, WARN tags, prior KILL count, X-check reminder)
and a short flag summary in the title.
  python scanner/entry_watch.py --gate <mint>   # print the gate decision for a mint from the current log (offline)
"""
import sys, os, json, time
from datetime import datetime, timezone, timedelta
import requests

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import filter_desk as fd   # importing has no side effects (main guarded); uses its DexScreener/GeckoTerminal helpers

WATCH_HOURS = 24
MIN_LIQ = 10_000
KEEP_DAYS = 7               # drop finished records older than this from entry_watch.json
ISSUE_MAX_AGE_D = 3
OWNER = "DarkJlo"
LABEL = "entry-ok"
DASHBOARD = "https://darkjlo.github.io/filter-desk-dashboard/"
WATCH_PATH = os.path.join(fd.DATA_DIR, "entry_watch.json")
REPO = os.environ.get("GITHUB_REPOSITORY") or "DarkJlo/filter-desk-dashboard"
TOKEN = os.environ.get("GITHUB_TOKEN") or ""
API = "https://api.github.com/repos/" + REPO
MARK = "<!-- fd-entry-watch mint:{mint} -->"
MAX_PRIOR_KILLS = 3          # r10.3: >= this many KILLs before the PASS -> no flip issue
GATE_VERSION = fd.FILTER_VERSION
X_REMINDER = ("X check not run automatically: send the mint to Filter Desk for an X check before entering.")

def now_utc(): return datetime.now(timezone.utc)
def iso(dt): return dt.isoformat(timespec="seconds")
def ts(s): return datetime.fromisoformat(s)

def money(v):
    try: v = float(v)
    except Exception: return "?"
    if v >= 1e6: return f"{v / 1e6:.2f}M"
    if v >= 1e3: return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"

def load_log():
    rows = []
    with open(fd.LOG_PATH) as f:
        for line in f:
            try: e = json.loads(line)
            except Exception: continue
            if e.get("verdict") in ("PASS", "KILL") and e.get("mint") and e.get("timestamp"): rows.append(e)
    rows.sort(key=lambda e: e["timestamp"])
    return rows

def candidates(rows, now):
    """{mint: (latest PASS in window, first WAIT/UNKNOWN time in window, killed_after: bool)}"""
    cut = iso(now - timedelta(hours=WATCH_HOURS))
    latest, first_wait, last_kill = {}, {}, {}
    for e in rows:
        m = e["mint"]
        if e["verdict"] == "KILL": last_kill[m] = e["timestamp"]; continue
        if e["timestamp"] < cut: continue
        latest[m] = e
        lab = e.get("entry") or (e.get("metrics") or {}).get("entry")
        if lab in ("WAIT", "UNKNOWN") and m not in first_wait: first_wait[m] = e["timestamp"]
    out = {}
    for m, e in latest.items():
        if m not in first_wait: continue   # never WAIT/UNKNOWN in the window
        out[m] = (e, first_wait[m], last_kill.get(m, "") > e["timestamp"])
    return out

# ---------------- r10.3 gate: risk flags on the PASS + repeat kills ----------------
TT_FRESH, TT_SHARED = 8, 5   # mirror of top_traders WARN_FRESH / WARN_SHARED (imported below when available)
try:
    import top_traders as _tt
    TT_FRESH, TT_SHARED = _tt.WARN_FRESH, _tt.WARN_SHARED
except Exception:
    _tt = None
TRADER_TAGS = {"trader_cluster", "trader_cluster_funder", "trader_fresh"}

def trader_tier(tt, tags):
    """r10.4 tier 'funder' (HIGH) | 'fresh' (LOW) | None. Legacy trader_cluster: stored numbers decide, else HIGH."""
    if _tt: return _tt.trader_tier(tt, tags)
    tags = set(tags or [])
    if "trader_cluster_funder" in tags: return "funder"
    if tt and tt.get("n"):
        if (tt.get("shared_funder_max") or 0) >= TT_SHARED: return "funder"
        if (tt.get("fresh_lt7d") or 0) >= TT_FRESH: return "fresh"
    if "trader_fresh" in tags: return "fresh"
    return "funder" if "trader_cluster" in tags else None

def pass_tags(pas):
    met = pas.get("metrics") or {}
    tags = met.get("warn_tags")
    if tags is None: tags = fd.warn_tags(met.get("warns"))
    return sorted(set(tags or []))

def tt_flagged(tt):
    """True when a top_traders summary is in the r10.4 HIGH tier (>= TT_SHARED share one non-CEX funder)."""
    return trader_tier(tt, ()) == "funder"

def prior_kills(rows, mint, before):
    return sum(1 for e in rows if e.get("mint") == mint and e.get("verdict") == "KILL" and e["timestamp"] < before)

def gate(pas, rows, tt_override=None):
    """Risk flags for a flip. {'block': bool, 'reasons': [...], 'tags': [...], 'tt_line': str, 'tt_checked': bool,
    'prior_kills': int, 'flags': short title summary}. Pure: no network."""
    m = pas.get("mint") or ""
    tags = pass_tags(pas)
    tt = tt_override if tt_override is not None else (pas.get("top_traders") or (pas.get("metrics") or {}).get("top_traders"))
    tt_ok = bool(tt and tt.get("n") and not str(tt.get("error") or "").startswith("HELIUS_API_KEY missing"))
    line = (tt or {}).get("line") or (pas.get("metrics") or {}).get("top_traders_line") or ""
    if tt and not line and _tt: line = _tt.summary_line(tt)
    pk = prior_kills(rows, m, pas.get("timestamp") or "9999")
    tier = trader_tier(tt, tags)
    if line and tier and "tier:" not in line:   # legacy line: say which r10.4 tier the stored numbers put it in
        line += " \u00b7 tier: " + ("HIGH (funder cluster)" if tier == "funder" else "LOW (fresh buyers, no shared funder)")
    tier_text = (_tt.tier_warn(tier, tt if (tt and tt.get("n")) else None) if (_tt and tier) else "")
    reasons = []
    if tier == "funder":   # r10.4 HIGH tier blocks; LOW fresh-only tier is annotated only
        reasons.append("trader_cluster_funder (HIGH): " + (tier_text or line or "top traders share a non-exchange funder"))
    if pk >= MAX_PRIOR_KILLS:
        reasons.append(f"repeat-killed: {pk} KILL verdicts before this PASS (limit {MAX_PRIOR_KILLS})")
    risk = [t for t in tags if t not in fd.DEGRADED_TAGS and t not in TRADER_TAGS]
    flags = []
    if tier == "funder": flags.append("FUNDER CLUSTER HIGH RISK")
    elif tier == "fresh":
        k = (tt or {}).get("fresh_lt7d")
        flags.append("fresh buyers" + (f" {k}/{(tt or {}).get('n') or 15}" if isinstance(k, int) else "") + " (no shared funder)")
    flags += risk
    if pk: flags.append(f"{pk}x killed")
    if not tt_ok: flags.append("traders unchecked")
    return {"block": bool(reasons), "reasons": reasons, "tags": tags, "tt_line": line, "tt_checked": tt_ok,
            "trader_tier": tier, "trader_note": tier_text,
            "prior_kills": pk, "flags": ", ".join(flags), "gate_version": GATE_VERSION}

def live_traders(mint, pair):
    """Run the r10.2 top-traders check at flip time when the PASS lacks one. Needs HELIUS_API_KEY; else None."""
    if not os.environ.get("HELIUS_API_KEY") or os.environ.get("FD_TOP_TRADERS", "1") == "0" or not _tt: return None
    try:
        s = _tt.top_traders(mint, pair)
        summ = {k: s.get(k) for k in fd.TT_SUMMARY_KEYS}
        summ["line"] = _tt.summary_line(s); summ["tier"] = _tt.cluster_tier(s); summ["at_flip"] = True
        return summ
    except Exception as e:
        print(f"  top traders at flip failed: {type(e).__name__}"); return None

# ---------------- GitHub issues (REST, workflow GITHUB_TOKEN) ----------------
def gh(method, path, **kw):
    r = requests.request(method, API + path, timeout=20, headers={
        "Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, **kw)
    if r.status_code >= 400 and r.status_code != 404:
        print(f"  github {method} {path}: HTTP {r.status_code} {r.text[:200]}")
    return r

def ensure_label():
    if gh("GET", "/labels/" + LABEL).status_code == 404:
        gh("POST", "/labels", json={"name": LABEL, "color": "26d07c",
                                    "description": "Filter Desk: a WAIT pass flipped to ENTRY OK"})

def entry_issues(state="all"):
    out, page = [], 1
    while page <= 5:
        r = gh("GET", "/issues", params={"labels": LABEL, "state": state, "per_page": 100, "page": page})
        if r.status_code != 200: break
        batch = [i for i in r.json() if "pull_request" not in i]
        out += batch
        if len(r.json()) < 100: break
        page += 1
    return out

def issue_body(rec, pas, test=False):
    m = rec["mint"]; met = pas.get("metrics") or {}
    g = rec.get("gate") or {}
    warns = met.get("warns") or []
    passed = ts(pas["timestamp"]).astimezone(fd_tz()).strftime("%b %d %H:%M MST")
    flipped = ts(rec["flipped_at"]).astimezone(fd_tz()).strftime("%b %d %H:%M MST")
    lines = [
        ("**TEST issue (notification check only, not a signal). Closed automatically.**\n" if test else "")
        + f"@{OWNER} **{rec.get('symbol') or m[:6]}** flipped from {rec.get('first_label') or 'WAIT'} to **ENTRY OK** at {flipped}.",
        "",
        f"- Mint: `{m}`",
        f"- FDV at flip: ${money(rec.get('fdv_at_flip'))} · liquidity at flip: ${money(rec.get('liq_at_flip'))}",
        f"- Entry reasons: " + ("; ".join(rec.get("reasons") or []) or "?"),
        f"- Original PASS: {passed} (rules {met.get('rules_version') or '?'}, tier {met.get('pass_tier') or '?'}, FDV then ${money(met.get('fdv'))})",
        f"- Warns at PASS: " + ("; ".join(warns) or "none"),
        f"- First WAIT seen: " + ts(rec["first_wait"]).astimezone(fd_tz()).strftime("%b %d %H:%M MST"),
        "",
        "**Risk flags (r10.4 gate)**",
    ] + ([f"- **{g['trader_note']}**" if g.get("trader_tier") == "funder" else f"- {g['trader_note']} (low tier, annotation only: not gated)"]
         if g.get("trader_note") else []) + [
        "- WARN tags: " + (", ".join(g.get("tags") or []) or "none"),
        "- " + (g.get("tt_line") or "Top traders: not checked on this PASS")
        + ("" if g.get("tt_checked") else (" (incomplete: treat as unknown)" if g.get("tt_line") else " (treat as unknown)")),
        "- Prior KILL verdicts for this mint: " + str(g.get("prior_kills", "?")),
        "- " + X_REMINDER,
        "",
        f"[DexScreener](https://dexscreener.com/solana/{m}) · [GMGN](https://gmgn.ai/sol/token/{m}) · [Dashboard]({DASHBOARD})",
        "",
        "Reminder: the account is tiny, so size small. You decide and you sign; this bot never signs or touches keys.",
        "",
        "_Auto-opened by Filter Desk entry watch (hourly). Closes automatically after 3 days._",
    ]
    if not test: lines.append(MARK.format(mint=m))
    return "\n".join(lines)

def issue_title(rec, test=False):
    t = ("[TEST] " if test else "") + f"ENTRY OK: {rec.get('symbol') or rec['mint'][:6]} @ ${money(rec.get('fdv_at_flip'))}"
    fl = (rec.get("gate") or {}).get("flags")
    return t + (f" [warn: {fl}]" if fl else "")

def fd_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/Phoenix")

def open_issue(rec, pas, existing, dry, test=False):
    title, body = issue_title(rec, test), issue_body(rec, pas, test)
    if not test:
        mk = MARK.format(mint=rec["mint"])
        dup = [i for i in existing if mk in (i.get("body") or "")]
        if dup:
            print(f"  issue already exists for {rec['mint'][:8]}: #{dup[0]['number']}")
            return dup[0]["number"]
    if dry or not TOKEN:
        print(("DRY RUN" if dry else "NO GITHUB_TOKEN") + f", would open issue:\n  title: {title}\n" +
              "\n".join("  | " + l for l in body.splitlines()))
        return None
    r = gh("POST", "/issues", json={"title": title, "body": body, "labels": [LABEL]})
    if r.status_code != 201: return None
    n = r.json()["number"]; print(f"  opened issue #{n}: {title}")
    if test:
        gh("PATCH", f"/issues/{n}", json={"state": "closed", "state_reason": "not_planned"})
        print(f"  closed TEST issue #{n}")
    return n

def close_old(existing, now, dry):
    for i in existing:
        if i.get("state") != "open": continue
        age = now - ts(i["created_at"].replace("Z", "+00:00"))
        if age > timedelta(days=ISSUE_MAX_AGE_D):
            if dry: print(f"  DRY RUN: would close #{i['number']} ({age.days}d old)"); continue
            gh("POST", f"/issues/{i['number']}/comments", json={"body": f"Auto-closed: older than {ISSUE_MAX_AGE_D} days."})
            gh("PATCH", f"/issues/{i['number']}", json={"state": "closed", "state_reason": "completed"})
            print(f"  closed #{i['number']} (older than {ISSUE_MAX_AGE_D}d)")

# ---------------- main ----------------
def main():
    if "--gate" in sys.argv:
        return print_gate(sys.argv[sys.argv.index("--gate") + 1:])
    dry, test = "--dry-run" in sys.argv, "--test-issue" in sys.argv
    now = now_utc()
    try: W = json.load(open(WATCH_PATH))
    except Exception: W = {}
    T = W.get("tokens") or {}
    rows = load_log()
    C = candidates(rows, now)
    pass_by_mint = {m: c[0] for m, c in C.items()}
    # mints already watching whose latest WAIT pass aged out of the window -> expired
    for m, rec in T.items():
        if m not in C and rec.get("status") in ("watching", "low_liq"):
            rec["status"] = "expired"; rec["expired_at"] = iso(now)
    live = []
    for m, (pas, first_wait, killed) in C.items():
        met = pas.get("metrics") or {}
        rec = T.get(m) or {"mint": m, "first_wait": first_wait, "checks": 0}
        rec["symbol"] = met.get("symbol") or rec.get("symbol") or ""
        rec["first_wait"] = min(rec.get("first_wait") or first_wait, first_wait)
        rec["pass_ts"] = pas["timestamp"]
        rec.setdefault("first_label", pas.get("entry") or met.get("entry"))
        T[m] = rec
        if rec.get("status") == "flipped" and "gate" not in rec:   # alerted before r10.3: record what the gate says now
            rec["gate"] = gate(pas, rows)
            if rec["gate"]["block"]: rec["gate"]["note"] = "issue opened before the r10.3 gate; it would be suppressed now"
        if rec.get("status") in ("flipped", "suppressed"): continue   # alert (or suppress) once; stop re-checking
        if killed:
            rec["status"] = "killed"; continue
        lab = pas.get("entry") or met.get("entry")
        if lab == "OK":   # a later scan PASS already says OK (scanner flip) -> treat as the flip
            rec.update(status="flipped", label="OK", reasons=pas.get("entry_reasons") or met.get("entry_reasons") or [],
                       flipped_at=pas["timestamp"], flipped_by="scan", last_check=iso(now),
                       fdv_at_flip=met.get("fdv"), liq_at_flip=met.get("liquidity_usd"), new_flip=True)
            continue
        live.append(m)
    dex = fd.fetch_dex(live) if live else {}
    flips = []
    for m in live:
        rec, pair = T[m], dex.get(m)
        liq = ((pair or {}).get("liquidity") or {}).get("usd")
        rec["liq"], rec["fdv"] = liq, (pair or {}).get("fdv")
        rec["last_check"] = iso(now)
        if not pair or (liq or 0) < MIN_LIQ:
            rec.update(status="low_liq", label=None, reasons=[f"liquidity ${money(liq)} < ${money(MIN_LIQ)}; entry check skipped"])
            print(f"{rec['symbol']:>12} {m[:8]} LOW LIQ ${money(liq)}"); continue
        M = {}
        lab = fd.entry_timing(pair, M)
        rec["checks"] = rec.get("checks", 0) + 1
        rec.update(label=lab, reasons=M.get("entry_reasons") or [], status="watching")
        print(f"{rec['symbol']:>12} {m[:8]} ENTRY {lab}: {'; '.join(rec['reasons'])}  (fdv ${money(rec['fdv'])}, liq ${money(liq)})")
        if lab == "OK":
            rec.update(status="flipped", flipped_at=iso(now), flipped_by="watch", fdv_at_flip=rec["fdv"], liq_at_flip=liq, new_flip=True)
    new = [m for m, r in T.items() if r.pop("new_flip", False)]
    # r10.3 gate: risk flags decide whether the flip may alert at all
    sent = []
    for m in new:
        rec, pas = T[m], pass_by_mint.get(m) or {}
        g = gate(pas, rows)
        if not g["tt_checked"] and not g["block"]:
            tt = live_traders(m, dex.get(m) or (fd.fetch_dex([m]).get(m) if os.environ.get("HELIUS_API_KEY") else None))
            if tt: g = gate(pas, rows, tt_override=tt); rec["top_traders_at_flip"] = tt
        rec["gate"] = g
        if g["block"]:
            rec.update(status="suppressed", suppressed_at=iso(now), suppressed_reasons=g["reasons"])
            print(f"SUPPRESSED {rec.get('symbol') or m[:6]} {m[:8]}: ENTRY OK flip, no issue opened: " + " | ".join(g["reasons"]))
        else:
            if g["flags"]: print(f"  flip {rec.get('symbol') or m[:6]} flags: {g['flags']}")
            sent.append(m)
    existing = []
    if TOKEN and not dry:
        ensure_label(); existing = entry_issues()
    elif TOKEN and dry:
        existing = entry_issues()
    for m in sent:
        n = open_issue(T[m], pass_by_mint.get(m) or {}, existing, dry)
        if n: T[m]["issue"] = n
    if test:
        tm = next((m for m in live if m in T), None) or next(iter(T), None)
        if tm:
            rec = dict(T[tm]); rec.setdefault("flipped_at", iso(now))
            rec.setdefault("gate", gate(pass_by_mint.get(tm) or {}, rows))
            rec.setdefault("fdv_at_flip", rec.get("fdv")); rec.setdefault("liq_at_flip", rec.get("liq"))
            open_issue(rec, pass_by_mint.get(tm) or {}, existing, dry, test=True)
        else: print("no token available for --test-issue")
    if TOKEN: close_old(existing, now, dry)
    # prune finished records
    cut = now - timedelta(days=KEEP_DAYS)
    T = {m: r for m, r in T.items() if r.get("status") in ("watching", "low_liq") or ts(r.get("pass_ts") or r["first_wait"]) >= cut}
    W = {"updated": iso(now), "window_h": WATCH_HOURS, "min_liq": MIN_LIQ, "tokens": T}
    tally = {}
    for r in T.values(): tally[r.get("status")] = tally.get(r.get("status"), 0) + 1
    print(f"entry watch: {len(live)} checked, {len(new)} new flip(s) ({len(new) - len(sent)} suppressed by gate), statuses {tally}")
    if dry: print("DRY RUN: entry_watch.json not written"); return
    with open(WATCH_PATH, "w") as f: json.dump(W, f, indent=1, sort_keys=True)

def print_gate(mints):
    rows = load_log()
    for q in mints or []:
        ps = [e for e in rows if e["mint"].startswith(q) and e["verdict"] == "PASS"]
        if not ps: print(f"{q}: no PASS in log"); continue
        pas = ps[-1]; g = gate(pas, rows)
        sym = (pas.get("metrics") or {}).get("symbol") or pas["mint"][:6]
        print(f"{sym} {pas['mint']} latest PASS {pas['timestamp']} entry {pas.get('entry')}")
        print("  gate: " + ("BLOCK (no flip issue): " + " | ".join(g["reasons"]) if g["block"] else "allow"))
        print(f"  tags {g['tags']} · prior KILLs {g['prior_kills']} · top traders: {g['tt_line'] or 'not checked'}")
        print(f"  title flags: {g['flags'] or '-'}")

if __name__ == "__main__":
    main()
