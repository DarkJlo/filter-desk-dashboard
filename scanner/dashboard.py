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

def build():
    rows = load()
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
        main = "; ".join(reasons[:3]) + (f" (+{len(reasons)-3})" if len(reasons) > 3 else "")
        if r["verdict"] == "PASS": main = "; ".join(warns[:3]) or "clean"
        score = r.get("score", m.get("rc_score")); norm = r.get("score_normalised", m.get("rc_score_norm"))
        lp = r.get("lpLockedPct", m.get("rc_lpLockedPct", m.get("lp_locked_pct")))
        liq = m.get("liquidity_usd") or m.get("dex_liquidity_usd")
        v = r["verdict"]
        sym, nm = (names.get(mint) or ["", ""])
        tok = f'<b>{html.escape(sym)}</b> <span class="nm">{html.escape(nm if nm != sym else "")}</span>' if sym else '<span class="nm">unknown</span>'
        trs.append(f'<tr class="{v.lower()}"><td>{ts}</td><td><a href="https://gmgn.ai/sol/token/{mint}" target="_blank" title="{mint}">{short}</a></td>'
                   f'<td>{tok}</td><td><span class="pill {v.lower()}">{v}</span></td><td class="r">{html.escape(main)}</td>'
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
.pill.pass{{background:#0e3b2a;color:#26d07c}} .pill.kill{{background:#3b1216;color:#f6465d}} .pill.error{{background:#3a2f0b;color:#f0b90b}}
</style></head><body><h1>Filter Desk</h1><div class="sub">Updated {now} · auto-refreshes every 60s <button onclick="location.href=location.pathname+'?t='+Date.now()">↻ Refresh</button>
<a href="{RUN_URL}" target="_blank"><button>▶ Run scan</button></a></div>
<div class="sub">Last scan: <b style="color:#d1d4dc">{last}</b> · ▶ Run scan opens GitHub Actions → click "Run workflow" (owner login required). A scan takes a few minutes; then hit ↻ Refresh (Pages may lag ~1 min).</div>
<div class="cards"><div class="card"><span>Checked</span><b>{total}</b></div><div class="card"><span>Kills</span><b style="color:#f6465d">{kills}</b></div>
<div class="card"><span>Passes</span><b style="color:#26d07c">{passes}</b></div><div class="card"><span>Kill rate</span><b>{rate}</b></div></div>
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
