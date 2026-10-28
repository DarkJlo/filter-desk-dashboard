"""r10.3 entry-watch gate tests. Offline: network helpers are monkeypatched.  python3 -m unittest discover tests"""
import os, sys, json, glob, shutil, tempfile, unittest, io, contextlib, py_compile
HERE = os.path.dirname(os.path.realpath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scanner"))
import entry_watch as ew
import filter_desk as fd

MINT = "TESTmint1111111111111111111111111111111pump"

def pas(ts="2026-10-28T20:00:00+00:00", tags=(), tt=None, entry="WAIT"):
    r = {"timestamp": ts, "mint": MINT, "verdict": "PASS", "entry": entry, "entry_reasons": [],
         "metrics": {"symbol": "TST", "warn_tags": list(tags), "warns": [], "rules_version": fd.FILTER_VERSION}}
    if tt is not None: r["top_traders"] = tt
    return r

def kill(ts): return {"timestamp": ts, "mint": MINT, "verdict": "KILL", "reasons": ["x"], "metrics": {}}

CLEAN_TT = {"n": 15, "fresh_lt7d": 0, "shared_funder_max": 0, "cex_funded": 8, "still_holding": 9, "sold_out": 2,
            "line": "Top traders (15): 0 fresh <7d, 0 share a non-CEX funder, 8 CEX-funded, 9 still holding, 2 sold out"}

class Gate(unittest.TestCase):
    def test_trader_cluster_tag_blocks(self):
        g = ew.gate(pas(tags=["trader_cluster"], tt=CLEAN_TT), [])
        self.assertTrue(g["block"]); self.assertIn("trader_cluster", g["reasons"][0])

    def test_tt_summary_thresholds_block_without_tag(self):
        self.assertTrue(ew.gate(pas(tt=dict(CLEAN_TT, fresh_lt7d=15)), [])["block"])
        self.assertTrue(ew.gate(pas(tt=dict(CLEAN_TT, shared_funder_max=5)), [])["block"])
        self.assertFalse(ew.gate(pas(tt=dict(CLEAN_TT, fresh_lt7d=7, shared_funder_max=4)), [])["block"])

    def test_repeat_kills(self):
        p = pas(tt=CLEAN_TT)
        two = [kill("2026-10-27T0%d:00:00+00:00" % i) for i in range(2)]
        self.assertFalse(ew.gate(p, two)["block"])
        three = two + [kill("2026-10-28T01:00:00+00:00")]
        g = ew.gate(p, three); self.assertTrue(g["block"]); self.assertEqual(g["prior_kills"], 3)
        later = two + [kill("2026-10-28T23:00:00+00:00")]          # kills after the PASS don't count
        self.assertFalse(ew.gate(p, later)["block"])

    def test_clean_flags_and_unchecked(self):
        g = ew.gate(pas(tt=CLEAN_TT), []); self.assertFalse(g["block"]); self.assertEqual(g["flags"], "")
        g = ew.gate(pas(tags=["bundle", "rc_down"]), [kill("2026-10-27T00:00:00+00:00")])
        self.assertFalse(g["block"]); self.assertEqual(g["flags"], "bundle, 1x killed, traders unchecked")

    def test_issue_body_and_title(self):
        p = pas(tags=["flat"], tt=CLEAN_TT)
        rec = {"mint": MINT, "symbol": "TST", "first_wait": p["timestamp"], "flipped_at": "2026-10-28T22:00:00+00:00",
               "fdv_at_flip": 500000, "liq_at_flip": 50000, "reasons": ["ok"], "gate": ew.gate(p, [kill("2026-10-27T00:00:00+00:00")])}
        body, title = ew.issue_body(rec, p), ew.issue_title(rec)
        self.assertIn(CLEAN_TT["line"], body); self.assertIn("WARN tags: flat", body)
        self.assertIn("Prior KILL verdicts for this mint: 1", body); self.assertIn("X check not run automatically", body)
        self.assertEqual(title, "ENTRY OK: TST @ $500K [warn: flat, 1x killed]")
        rec["gate"] = ew.gate(pas(tt=CLEAN_TT), [])
        self.assertEqual(ew.issue_title(rec), "ENTRY OK: TST @ $500K")

class MainFlow(unittest.TestCase):
    """Full main() with a temp log: a WAIT pass flips to OK; gated ones get status suppressed and no issue."""
    def run_main(self, rows, argv=("entry_watch.py",)):
        d = tempfile.mkdtemp()
        try:
            log = os.path.join(d, "checks_log.jsonl")
            with open(log, "w") as f:
                for r in rows: f.write(json.dumps(r) + "\n")
            saved = (fd.LOG_PATH, ew.WATCH_PATH, fd.fetch_dex, fd.entry_timing, ew.now_utc, ew.TOKEN, sys.argv, ew.live_traders)
            fd.LOG_PATH, ew.WATCH_PATH, ew.TOKEN, sys.argv = log, os.path.join(d, "entry_watch.json"), "", list(argv)
            fd.fetch_dex = lambda ms: {m: {"liquidity": {"usd": 50000}, "fdv": 400000} for m in ms}
            def et(pair, M): M["entry_reasons"] = ["pulled back -50% and holding higher low"]; return "OK"
            fd.entry_timing = et; ew.live_traders = lambda m, p: None
            ew.now_utc = lambda: ew.ts("2026-10-28T23:00:00+00:00")
            out = io.StringIO()
            with contextlib.redirect_stdout(out): ew.main()
            W = {}
            if os.path.exists(ew.WATCH_PATH):
                with open(ew.WATCH_PATH) as f: W = json.load(f)
            return out.getvalue(), W
        finally:
            fd.LOG_PATH, ew.WATCH_PATH, fd.fetch_dex, fd.entry_timing, ew.now_utc, ew.TOKEN, sys.argv, ew.live_traders = saved
            shutil.rmtree(d)

    def test_repeat_killed_suppressed(self):
        rows = [kill("2026-10-2%dT0%d:00:00+00:00" % (7 + i // 4, i % 4)) for i in range(6)] + [pas()]
        out, W = self.run_main(rows)
        rec = W["tokens"][MINT]
        self.assertEqual(rec["status"], "suppressed"); self.assertNotIn("issue", rec)
        self.assertIn("SUPPRESSED TST", out); self.assertNotIn("would open issue", out)

    def test_trader_cluster_suppressed_and_clean_alerts(self):
        out, W = self.run_main([pas(tags=["trader_cluster"])])
        self.assertEqual(W["tokens"][MINT]["status"], "suppressed")
        out, W = self.run_main([pas(tt=CLEAN_TT)])
        self.assertEqual(W["tokens"][MINT]["status"], "flipped")
        self.assertIn("NO GITHUB_TOKEN, would open issue", out); self.assertIn("X check not run automatically", out)

class Py311(unittest.TestCase):
    def test_compiles(self):
        for p in glob.glob(os.path.join(REPO, "scanner", "*.py")) + glob.glob(os.path.join(HERE, "*.py")):
            py_compile.compile(p, doraise=True)

if __name__ == "__main__":
    unittest.main()
