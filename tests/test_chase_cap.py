"""r10.6.1 chase cap: suppress late / stopped-out flips; exit plan off check FDV.
Offline.  python3 -m unittest discover tests
"""
import os, sys, io, json, shutil, tempfile, unittest, contextlib
HERE = os.path.dirname(os.path.realpath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scanner"))
import filter_desk as fd
import entry_watch as ew

MINT = "TESTmint1111111111111111111111111111111pump"
CLEAN_TT = {"n": 15, "fresh_lt7d": 0, "shared_funder_max": 0, "cex_funded": 8, "still_holding": 9, "sold_out": 2,
            "line": "Top traders (15): 0 fresh <7d, 0 share a non-CEX funder, 8 CEX-funded, 9 still holding, 2 sold out"}

def pas(ts="2026-11-05T20:00:00+00:00", fdv=98000, liq=50000, entry="WAIT"):
    return {"timestamp": ts, "mint": MINT, "verdict": "PASS", "entry": entry, "entry_reasons": [],
            "top_traders": CLEAN_TT,
            "metrics": {"symbol": "inu", "warn_tags": [], "warns": [], "rules_version": fd.FILTER_VERSION,
                        "liquidity_usd": liq, "fdv": fdv, "pass_tier": "clean"}}

class ChaseCapPure(unittest.TestCase):
    def test_constant(self):
        self.assertEqual(fd.CHASE_CAP_MULT, 1.5)
        self.assertEqual(fd.FILTER_VERSION, "r10.7.2-2026-11-07")
        self.assertEqual(ew.GATE_VERSION, fd.FILTER_VERSION)

    def test_1_4x_allowed(self):
        self.assertIsNone(fd.chase_cap(100_000, 140_000))
        self.assertIsNone(fd.chase_cap(100_000, 149_999))

    def test_1_5x_suppressed(self):
        r = fd.chase_cap(100_000, 150_000)
        self.assertEqual(r, "chase cap: flip FDV 1.50x check >= 1.5x")

    def test_inu_like_2_9x_suppressed(self):
        # inu: ~$98K check FDV -> $282K flip (~2.88x)
        r = fd.chase_cap(98_000, 282_000)
        self.assertIsNotNone(r)
        self.assertIn("2.88x", r)
        self.assertTrue(r.startswith("chase cap:"))
        self.assertIn(">= 1.5x", r)

    def test_0_5x_stopped_out_suppressed(self):
        r = fd.chase_cap(100_000, 50_000)
        self.assertEqual(r, "chase cap: flip FDV 0.50x check <= 0.5x (stopped out)")
        self.assertIsNone(fd.chase_cap(100_000, 50_001))

    def test_missing_fdv_no_suppress(self):
        self.assertIsNone(fd.chase_cap(None, 100))
        self.assertIsNone(fd.chase_cap(100, None))
        self.assertIsNone(fd.chase_cap(0, 100))
        self.assertIsNone(fd.chase_cap(100, 0))

class ExitPlanOffCheck(unittest.TestCase):
    def test_issue_body_uses_check_fdv(self):
        p = pas(fdv=98_000)
        # flip at ~2.88x would have been the old (wrong) anchor
        rec = {"mint": MINT, "symbol": "inu", "first_wait": p["timestamp"],
               "flipped_at": "2026-11-05T23:00:00+00:00",
               "fdv_at_flip": 282_000, "liq_at_flip": 40_000, "reasons": ["holding higher low"],
               "gate": ew.gate(p, [])}
        body = ew.issue_body(rec, p)
        self.assertIn("FDV at check: $98K", body)
        self.assertIn("FDV at flip: $282K", body)
        self.assertIn("Exit plan (off the PASS check FDV)", body)
        # 2x / 0.5x of check 98K, not of flip 282K
        ep = fd.exit_plan(98_000, p["timestamp"])
        self.assertIn(ep["text"], body)
        self.assertEqual(ep["take_profit_fdv"], 196_000)
        self.assertEqual(ep["stop_fdv"], 49_000)
        self.assertEqual(ep["sell_by"], "2026-11-06T20:00:00+00:00")  # 24h after check, not flip
        self.assertNotIn("FDV $564K", body)  # would be 2x of flip

class MainFlowChase(unittest.TestCase):
    def run_main(self, flip_fdv, check_fdv=98_000):
        d = tempfile.mkdtemp()
        try:
            log = os.path.join(d, "checks_log.jsonl")
            with open(log, "w") as f:
                f.write(json.dumps(pas(fdv=check_fdv)) + "\n")
            saved = (fd.LOG_PATH, ew.WATCH_PATH, fd.fetch_dex, fd.entry_timing, ew.now_utc, ew.TOKEN, sys.argv, ew.live_traders)
            fd.LOG_PATH, ew.WATCH_PATH, ew.TOKEN, sys.argv = log, os.path.join(d, "entry_watch.json"), "", ["entry_watch.py"]
            fd.fetch_dex = lambda ms: {m: {"liquidity": {"usd": 50_000}, "fdv": flip_fdv} for m in ms}
            def et(pair, M):
                M["entry_reasons"] = ["pulled back -50% and holding higher low"]; return "OK"
            fd.entry_timing = et; ew.live_traders = lambda m, p: None
            ew.now_utc = lambda: ew.ts("2026-11-05T23:00:00+00:00")
            out = io.StringIO()
            with contextlib.redirect_stdout(out): ew.main()
            W = json.load(open(ew.WATCH_PATH)) if os.path.exists(ew.WATCH_PATH) else {}
            return out.getvalue(), W
        finally:
            fd.LOG_PATH, ew.WATCH_PATH, fd.fetch_dex, fd.entry_timing, ew.now_utc, ew.TOKEN, sys.argv, ew.live_traders = saved
            shutil.rmtree(d)

    def test_1_4x_alerts(self):
        out, W = self.run_main(flip_fdv=137_200)  # 1.4x of 98K
        rec = W["tokens"][MINT]
        self.assertEqual(rec["status"], "flipped")
        self.assertIn("would open issue", out)
        self.assertNotIn("chase cap", out)

    def test_inu_like_suppressed(self):
        out, W = self.run_main(flip_fdv=282_000)
        rec = W["tokens"][MINT]
        self.assertEqual(rec["status"], "suppressed")
        self.assertTrue(any("chase cap" in r for r in rec["suppressed_reasons"]))
        self.assertIn("chase cap: flip FDV 2.88x check >= 1.5x", out)
        self.assertNotIn("would open issue", out)

    def test_stopped_out_suppressed(self):
        out, W = self.run_main(flip_fdv=49_000)  # 0.5x of 98K
        rec = W["tokens"][MINT]
        self.assertEqual(rec["status"], "suppressed")
        self.assertIn("stopped out", rec["suppressed_reasons"][0])
        self.assertNotIn("would open issue", out)

if __name__ == "__main__":
    unittest.main()
