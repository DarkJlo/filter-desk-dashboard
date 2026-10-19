#!/usr/bin/env python3
"""Builds dashboard.html from checks_log.jsonl. Run: python3 dashboard.py"""
import json, os, html
from datetime import datetime
from zoneinfo import ZoneInfo
_HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.environ.get("FD_REPO") or os.path.dirname(_HERE)
D = os.environ.get("FD_DATA_DIR") or os.path.join(REPO, "data")
OUT = os.environ.get("FD_OUT") or os.path.join(REPO, "index.html")
RUN_URL = "https://github.com/DarkJlo/filter-desk-dashboard/actions/workflows/scan.yml"
TZ = ZoneInfo("America/Phoenix")

def load():
    rows = []
    for line in open(os.path.join(D, "checks_log.jsonl")):
        try: e = json.loads(line)
        except Exception: continue
        if e.get("verdict") not in ("PASS", "KILL", "ERROR"): continue  # skip sub-tool runs
        rows.append(e)
    return rows

NAMES = os.path.join(D, "names.json")
def find_name(e):
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("symbol"), str) and o["symbol"]: return o["symbol"], o.get("name") or ""
            for v in o.values():
                r = walk(v)
                if r: return r
        elif isinstance(o, list):
            for v in o[:5]:
                r = walk(v)
                if r: return r
    return walk(e.get("metrics") or {})

def names_for(rows):
    try: cache = json.load(open(NAMES))
    except Exception: cache = {}
    for r in rows:
        if r["mint"] not in cache:
            n = find_name(r)
            if n: cache[r["mint"]] = list(n)
    missing = list({r["mint"] for r in rows if r["mint"] not in cache})
    try:
        import requests
        for i in range(0, len(missing), 30):
            for p in requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(missing[i:i+30]), timeout=10).json() or []:
                b = p.get("baseToken") or {}
                if b.get("address") in missing and b.get("address") not in cache:
                    cache[b["address"]] = [b.get("symbol") or "", b.get("name") or ""]
    except Exception: pass
    json.dump(cache, open(NAMES, "w"))
    return cache

def num(v, f="{:.1f}"):
    try: return f.format(float(v))
    except Exception: return "—"

def filter_version():
    """Current rules version, read from filter_desk.py without importing it (avoids circular import)."""
    import re
    try:
        m = re.search(r'^FILTER_VERSION\s*=\s*"([^"]+)"', open(os.path.join(_HERE, "filter_desk.py")).read(), re.M)
        return m.group(1) if m else "unknown"
    except Exception: return "unknown"

STATUS_COL = {"WINNER": "#26d07c", "RUGGED": "#f6465d", "DEAD": "#f0b90b"}

def oc_badge(OC, mint):
    t = OC["tokens"].get(mint)
    if not t or t.get("cur_mult") is None: return ""
    col = STATUS_COL.get(t["status"], "#9aa4b2")
    return f' <span class="nm" style="color:{col}" title="outcome: peak {t.get("peak_mult",0):.2f}x">{t["status"]} {t["cur_mult"]:.2f}x</span>'

def warn_html(w):
    """Colour a warning; bundler WARN band (10-20%) is highlighted so it is visible before it becomes a KILL."""
    col = "#f0b90b" if w.startswith(("BUNDLER WARN", "BUNDLE WARN", "LOPSIDED WARN", "FUNDING WARN", "bundled launch", "FLAT WARN")) else "#9aa4b2"
    return f'<span style="color:{col}">{html.escape(w)}</span>'

def load_all():
    """Every log entry incl. sub-tool runs (BUNDLERS/TRACE/BUNDLE) grouped by mint, oldest first."""
    by = {}
    for line in open(os.path.join(D, "checks_log.jsonl")):
        try: e = json.loads(line)
        except Exception: continue
        by.setdefault(e.get("mint"), []).append(e)
    return by

LOPSIDED_SOFT_MIN_AGE_H, LOPSIDED_SOFT_MIN_RATIO = 4.0, 0.38   # mirror of filter_desk r8 gate

def today_rules(t, entries):
    """Would the CURRENT rules kill this pass? Inferred from later check logs (bundler %, bundle, rechecks)."""
    t0 = t["t0"]; later = [e for e in entries if e["timestamp"] >= t0]
    if not later: return ""
    notes, kill = [], False
    bp = [((e.get("metrics") or {}).get("bundler") or {}).get("bundler_pct") for e in later]
    bp = [(x, e["timestamp"]) for x, e in zip(bp, later) if x is not None]
    if bp:
        x, when = max(bp)
        dh = (datetime.fromisoformat(when) - datetime.fromisoformat(t0)).total_seconds() / 3600
        lag = f" ({dh:.0f}h after pass)" if dh >= 1 else ""
        if x >= 20: notes.append(f"KILL: bundlers {x:.0f}% ≥ 20%{lag}"); kill = True
        elif x >= 10: notes.append(f"warn: bundlers {x:.0f}%{lag}")
    full = [e for e in later if e.get("verdict") in ("PASS", "KILL") and e["timestamp"] > t0]
    if full:
        e = full[-1]; m = e.get("metrics") or {}
        rs = [r for r in e.get("reasons") or [] if not r.startswith("missing data")]
        b, s_ = m.get("buys_h24"), m.get("sells_h24"); age = (m.get("pair_age_min") or 0) / 60
        if b and s_ is not None and age >= LOPSIDED_SOFT_MIN_AGE_H and LOPSIDED_SOFT_MIN_RATIO <= s_ / b < 0.5:
            if any(r.startswith("lopsided") for r in rs): notes.append("lopsided → WARN under r8")
            rs = [r for r in rs if not r.startswith("lopsided")]   # r8 turns this into a WARN
        rs = [r for r in rs if not r.startswith("bundlers")]
        when = datetime.fromisoformat(e["timestamp"]).astimezone(TZ).strftime("%b %d %H:%M")
        if rs: notes.append(f"recheck {when}: KILL ({'; '.join(rs[:2])})"); kill = True
        elif e.get("verdict") == "KILL" and not rs:
            notes.append(f"recheck {when}: would PASS under r8" if (e.get("reasons") or [None])[0] and
                         not all(r.startswith("missing data") for r in e["reasons"]) else f"recheck {when}: data gap only")
    col = "#f6465d" if kill else "#9aa4b2"
    return f'<span style="color:{col}">{html.escape(" · ".join(notes))}</span>' if notes else ""

def _age(t0):
    h = (datetime.now().astimezone() - datetime.fromisoformat(t0)).total_seconds() / 3600
    return f"{h:.0f}h" if h < 48 else f"{h/24:.1f}d"

def outcomes_html(OC):
    S = OC.get("summary") or {}
    if not S: return ""
    P, K = S.get("pass") or {}, S.get("kill_control") or {}
    pct = lambda v: "—" if v is None else f"{v}%"
    mx = lambda v: "—" if v is None else f"{v:.2f}x"
    def card(label, val, sub, col="#fff"):
        return f'<div class="card"><span>{label}</span><b style="color:{col}">{val}</b><div class="nm">{sub}</div></div>'
    cards = (card("Pass hit 2x", pct(P.get("hit2x")), f'n={P.get("tracked",0)} strict passes', "#26d07c")
             + card("Pass rug/dead", pct(P.get("rugged_or_dead")), f'rugged {pct(P.get("rugged"))}', "#f6465d")
             + card("Kill-control hit 2x", pct(K.get("hit2x")), f'n={K.get("tracked",0)} random kills')
             + card("Kill-control rug/dead", pct(K.get("rugged_or_dead")), f'rugged {pct(K.get("rugged"))}')
             + card("Median peak", f'{mx(P.get("median_peak"))}', f'control {mx(K.get("median_peak"))}')
             + card("Median now", f'{mx(P.get("median_cur"))}', f'control {mx(K.get("median_cur"))}'))
    seg = []
    for g, lab in (("pass_clean", "clean at entry"), ("pass_warn", "warn at entry"), ("pass_never_warn", "never warned"),
                   ("pass_warn_any", "warned on any pass check"), ("pass_degraded", "RugCheck-down pass")):
        x = S.get(g) or {}
        if x.get("tracked"): seg.append(f'{lab}: n={x["tracked"]} · 2x {x["hit2x"]}% · rug/dead {x["rugged_or_dead"]}% · med peak {mx(x.get("median_peak"))}')
    by = load_all()
    f = lambda t, k: (f'{t[k]:.2f}x' if t.get(k) is not None else "—")
    TAGS = [("BUNDLER WARN", "bundler"), ("BUNDLE WARN", "bundle"), ("FUNDING WARN", "funding"), ("LOPSIDED WARN", "lopsided"),
            ("bundled launch", "bundled launch"), ("FLAT WARN", "flat"), ("rugcheck unavailable", "RC down"), ("LP lock unverified", "LP unverified")]
    rows = ""
    for m, t in sorted(OC["tokens"].items(), key=lambda kv: kv[1]["t0"], reverse=True):
        if t["group"] != "pass": continue
        when = datetime.fromisoformat(t["t0"]).astimezone(TZ).strftime("%b %d %H:%M")
        col = STATUS_COL.get(t.get("status"), "#9aa4b2")
        ws = t.get("warns_at_entry") or []
        wtxt = ", ".join(lab for pfx, lab in TAGS if any(w.startswith(pfx) for w in ws)) or "none"
        tier = t.get("pass_tier") or "—"
        rows += (f'<tr><td><a href="https://gmgn.ai/sol/token/{m}" target="_blank" title="{m}"><b>{html.escape(t.get("symbol") or m[:6])}</b></a></td>'
                 f'<td>{when}</td><td>{_age(t["t0"])}</td><td>${num(t.get("entry_mcap"),"{:,.0f}")}{"*" if t.get("entry_approx") else ""}</td>'
                 f'<td>{f(t,"m1h")}</td><td>{f(t,"m24h")}</td><td><b>{f(t,"peak_mult")}</b></td><td>{f(t,"cur_mult")}</td>'
                 f'<td style="color:{col}">{t.get("status")}{" (final)" if t.get("final") else ""}</td>'
                 f'<td class="r" title="{html.escape(" | ".join(ws))}">{html.escape(wtxt)} <span class="nm">[{html.escape(tier)} · {html.escape(t.get("rules_version") or "?")}]</span></td>'
                 f'<td class="r">{today_rules(t, by.get(m, []))}</td><td class="r">{html.escape(t.get("note",""))}</td></tr>')
    krows = ""
    for m, t in sorted(OC["tokens"].items(), key=lambda kv: (-(kv[1].get("peak_mult") or 0))):
        if t["group"] != "kill_control": continue
        col = STATUS_COL.get(t.get("status"), "#9aa4b2")
        krows += (f'<tr><td><a href="https://gmgn.ai/sol/token/{m}" target="_blank" title="{m}">{html.escape(t.get("symbol") or m[:6])}</a></td>'
                  f'<td>${num(t.get("entry_mcap"),"{:,.0f}")}</td><td>{f(t,"peak_mult")}</td><td>{f(t,"cur_mult")}</td>'
                  f'<td style="color:{col}">{t.get("status")}</td><td class="r">{html.escape((t.get("kill_reason") or "")[:90])}</td></tr>')
    upd = datetime.fromisoformat(OC["updated"]).astimezone(TZ).strftime("%b %d %H:%M MST") if OC.get("updated") else "—"
    return (f'<h2>Outcomes <span class="nm" style="font-size:12px">updated {upd} · hourly · 7-day window · multiples vs price the scanner saw at check time · 2x = peak ≥ 2x</span></h2>'
            f'<div class="cards">{cards}</div>'
            + (f'<div class="nm" style="margin:-10px 0 14px">Segments: {" &nbsp;|&nbsp; ".join(seg)}</div>' if seg else "")
            + f'<table style="margin-bottom:8px"><thead><tr><th>Token</th><th>Passed (MST)</th><th>Age</th><th>Entry mcap</th><th>+1h</th><th>+24h</th><th>Peak</th><th>Now</th><th>Status</th>'
            f'<th>Warns at pass [tier · rules]</th><th>Would today\'s rules kill?</th><th>Note</th></tr></thead>'
            f'<tbody>{rows or "<tr><td colspan=12>No passes tracked yet.</td></tr>"}</tbody></table>'
            f'<div class="nm" style="margin-bottom:18px">* entry approximated from first observation · "Would today\'s rules kill?" is inferred from later check logs (bundler %, rechecks); missing-data kills are ignored.</div>'
            f'<details style="margin-bottom:26px"><summary class="nm" style="cursor:pointer">Kill control group ({K.get("tracked",0)} random kills, 5/scan) — sorted by peak</summary>'
            f'<table class="small"><thead><tr><th>Token</th><th>Entry mcap</th><th>Peak</th><th>Now</th><th>Status</th><th>Kill reason</th></tr></thead>'
            f'<tbody>{krows or "<tr><td colspan=6>None yet.</td></tr>"}</tbody></table></details>')

def build():
    rows = load()
    try: OC = json.load(open(os.path.join(D, "outcomes.json")))
    except Exception: OC = {"tokens": {}, "summary": {}}
    kills = sum(r["verdict"] == "KILL" for r in rows); passes = sum(r["verdict"] == "PASS" for r in rows)
    total = len(rows); rate = f"{100*kills/total:.1f}%" if total else "—"
    names = names_for(rows)
    trs = []
    order = list(reversed(rows))
    order.sort(key=lambda r: r["verdict"] != "PASS")  # stable: passes first, newest first within each
    for r in order:
        m = r.get("metrics") or {}
        ts = datetime.fromisoformat(r["timestamp"]).astimezone(TZ).strftime("%b %d %H:%M")
        mint = r["mint"]; short = mint[:4] + "…" + mint[-4:]
        reasons = r.get("reasons") or []
        warns = m.get("warns") or []
        main = html.escape("; ".join(reasons[:3]) + (f" (+{len(reasons)-3})" if len(reasons) > 3 else ""))
        if r["verdict"] == "PASS": main = "; ".join(warn_html(w) for w in warns[:4]) or "clean"
        score = r.get("score", m.get("rc_score")); norm = r.get("score_normalised", m.get("rc_score_norm"))
        lp = r.get("lpLockedPct", m.get("rc_lpLockedPct", m.get("lp_locked_pct")))
        liq = m.get("liquidity_usd") or m.get("dex_liquidity_usd")
        v = r["verdict"]
        sym, nm = (names.get(mint) or ["", ""])
        tok = f'<b>{html.escape(sym)}</b> <span class="nm">{html.escape(nm if nm != sym else "")}</span>' if sym else '<span class="nm">unknown</span>'
        trs.append(f'<tr class="{v.lower()}"><td>{ts}</td><td><a href="https://gmgn.ai/sol/token/{mint}" target="_blank" title="{mint}">{short}</a></td>'
                   f'<td>{tok}</td><td><span class="pill {v.lower()}" title="rules {html.escape(m.get("rules_version") or "pre-r8")}">{v}</span>{oc_badge(OC, mint)}</td><td class="r">{main}</td>'
                   f'<td>{num(score,"{:.0f}")} / {num(norm,"{:.0f}")} / {num(lp)}</td><td>{"$"+num(liq,"{:,.0f}") if liq else "—"}</td></tr>')
    now = datetime.now(TZ).strftime("%b %d, %Y %H:%M MST")
    last = datetime.fromisoformat(max(r["timestamp"] for r in rows)).astimezone(TZ).strftime("%b %d, %Y %H:%M MST") if rows else "never"
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>Filter Desk</title><style>
body{{background:#0b0e11;color:#d1d4dc;font:13px/1.4 -apple-system,Segoe UI,Roboto,monospace;margin:0;padding:24px}}
h1{{font-size:18px;margin:0 0 4px;color:#fff}} .sub{{color:#6b7280;margin-bottom:18px}}
.cards{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.card{{background:#151a21;border:1px solid #232a33;border-radius:8px;padding:12px 16px;min-width:120px}}
.card b{{display:block;font-size:22px;color:#fff}} .card span{{color:#6b7280;font-size:11px;text-transform:uppercase}}
button{{background:#1f2630;color:#d1d4dc;border:1px solid #2d3643;border-radius:6px;padding:4px 10px;margin-left:10px;cursor:pointer}} button:hover{{background:#2a3340}}
input{{background:#151a21;border:1px solid #232a33;color:#d1d4dc;padding:6px 10px;border-radius:6px;margin-bottom:10px;width:260px}}
table{{width:100%;border-collapse:collapse}} th{{text-align:left;color:#6b7280;font-weight:500;border-bottom:1px solid #232a33;padding:8px}}
td{{padding:7px 8px;border-bottom:1px solid #161b22;white-space:nowrap}} td.r{{white-space:normal;color:#9aa4b2;max-width:520px}}
.nm{{color:#6b7280}} a{{color:#8ab4f8;text-decoration:none}} .pill{{padding:2px 8px;border-radius:10px;font-weight:600;font-size:11px}}
h2{{font-size:15px;color:#fff;margin:6px 0 8px}} table.small td,table.small th{{padding:4px 8px;font-size:12px}} .ver{{background:#1f2630;border:1px solid #2d3643;border-radius:10px;padding:1px 8px;font-size:11px;color:#8ab4f8;margin-left:8px;vertical-align:middle}}
.pill.pass{{background:#0e3b2a;color:#26d07c}} .pill.kill{{background:#3b1216;color:#f6465d}} .pill.error{{background:#3a2f0b;color:#f0b90b}}
</style></head><body><h1>Filter Desk <span class="ver" title="current filter rules version (docs/RULES_CHANGELOG.md)">rules {filter_version()}</span></h1><div class="sub">Updated {now} · auto-refreshes every 60s <button onclick="location.href=location.pathname+'?t='+Date.now()">↻ Refresh</button>
<a href="{RUN_URL}" target="_blank"><button>▶ Run scan</button></a></div>
<div class="sub">Last scan: <b style="color:#d1d4dc">{last}</b> · Auto-scan 3×/day (8:00, 14:00 &amp; 20:00 MST). ▶ Run scan still works on demand. Takes a few minutes; then ↻ Refresh (Pages may lag ~1 min).</div>
<div class="cards"><div class="card"><span>Checked</span><b>{total}</b></div><div class="card"><span>Kills</span><b style="color:#f6465d">{kills}</b></div>
<div class="card"><span>Passes</span><b style="color:#26d07c">{passes}</b></div><div class="card"><span>Kill rate</span><b>{rate}</b></div></div>
{outcomes_html(OC)}
<input id="q" placeholder="Filter by mint, verdict, reason…" oninput="for(const t of document.querySelectorAll('tbody tr'))t.style.display=t.innerText.toLowerCase().includes(this.value.toLowerCase())?'':'none'">
<table><thead><tr><th>Time (MST)</th><th>Mint</th><th>Token</th><th>Verdict</th><th>Main reasons / warnings</th><th>Score / Norm / LP%</th><th>Liquidity</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan=7>No checks yet.</td></tr>'}</tbody></table></body></html>"""
    open(OUT, "w").write(page)
    return total, kills, passes

if __name__ == "__main__":
    print(build())

def _git(*a):
    import subprocess
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True)

def git_sync_pull():
    _git("config", "merge.ours.driver", "true")
    r = _git("pull", "-q", "--rebase", "--autostash")
    return "pulled" if r.returncode == 0 else "pull failed: " + r.stderr[-200:]

def publish():
    """Commit data/ + index.html and push (pull --rebase first, retry once)."""
    _git("config", "merge.ours.driver", "true")
    _git("add", "data", "index.html")
    if not _git("diff", "--cached", "--quiet").returncode: return "no change"
    _git("-c", "user.name=DarkJlo", "-c", "user.email=DarkJlo@users.noreply.github.com", "commit", "-qm", "box scan update")
    for attempt in range(2):
        p = _git("pull", "-q", "--rebase", "--autostash")
        if p.returncode != 0:
            _git("rebase", "--abort")
            continue
        build()  # regenerate page from merged log
        _git("add", "index.html")
        if _git("diff", "--cached", "--quiet").returncode:
            _git("-c", "user.name=DarkJlo", "-c", "user.email=DarkJlo@users.noreply.github.com", "commit", "-qm", "rebuild page")
        if _git("push", "-q").returncode == 0: return "pushed"
    return "push failed after retry"

if __name__ == "__main__" and "--publish" in __import__("sys").argv:
    print(publish())
