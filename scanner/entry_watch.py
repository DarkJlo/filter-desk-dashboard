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
        f"[DexScreener](https://dexscreener.com/solana/{m}) · [GMGN](https://gmgn.ai/sol/token/{m}) · [Dashboard]({DASHBOARD})",
        "",
        "Reminder: the account is tiny, so size small. You decide and you sign; this bot never signs or touches keys.",
        "",
        "_Auto-opened by Filter Desk entry watch (hourly). Closes automatically after 3 days._",
    ]
    if not test: lines.append(MARK.format(mint=m))
    return "\n".join(lines)

def issue_title(rec, test=False):
    return ("[TEST] " if test else "") + f"ENTRY OK: {rec.get('symbol') or rec['mint'][:6]} @ ${money(rec.get('fdv_at_flip'))}"

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
        if rec.get("status") == "flipped": continue          # alert once; stop re-checking
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
    existing = []
    if TOKEN and not dry:
        ensure_label(); existing = entry_issues()
    elif TOKEN and dry:
        existing = entry_issues()
    for m in new:
        n = open_issue(T[m], pass_by_mint.get(m) or {}, existing, dry)
        if n: T[m]["issue"] = n
    if test:
        tm = next((m for m in live if m in T), None) or next(iter(T), None)
        if tm:
            rec = dict(T[tm]); rec.setdefault("flipped_at", iso(now))
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
    print(f"entry watch: {len(live)} checked, {len(new)} new flip(s), statuses {tally}")
    if dry: print("DRY RUN: entry_watch.json not written"); return
    with open(WATCH_PATH, "w") as f: json.dump(W, f, indent=1, sort_keys=True)

if __name__ == "__main__":
    main()
