"""r10.5 FUNDER CLUSTER KILL: >= 12/15 top traders sharing one non-exchange funder -> KILL. Offline (network monkeypatched)."""
import os, sys, io, contextlib, unittest
HERE = os.path.dirname(os.path.realpath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scanner"))
import filter_desk as fd
import top_traders as tt

MINT = "TESTmint1111111111111111111111111111111pump"
FUNDER = "FuNdEr9xQ2222222222222222222222222222abcd"

def summ(shared, fresh=15, funder=FUNDER, n=15, **kw):
    s = {"n": n, "fresh_lt7d": fresh, "shared_funder_max": shared, "cex_funded": 0, "still_holding": 3, "sold_out": 12,
         "window_hours": 5.0, "helius_credits_est": 151, "error": None, "wallets": []}
    if funder and shared >= 2: s["shared_funder"] = funder
    s.update(kw); return s

class _Patch:
    """Monkeypatch tt.top_traders to return a fixed summary (or raise)."""
    def __init__(self, s=None, exc=None): self.s, self.exc = s, exc
    def __enter__(self):
        self.saved = tt.top_traders
        def fake(m, p=None, record_funders=True):
            if self.exc: raise self.exc
            return dict(self.s)
        tt.top_traders = fake; return self
    def __exit__(self, *a): tt.top_traders = self.saved

def verdict(s=None, exc=None, v="PASS", R=None):
    M = {"warns": ["FLAT WARN: y"]}
    with _Patch(s, exc), contextlib.redirect_stdout(io.StringIO()):
        v2, R2 = fd.trader_verdict(MINT, None, M, v, list(R or []))
    return v2, R2, M

class FunderClusterKill(unittest.TestCase):
    def test_version_and_constant(self):
        self.assertEqual(fd.FILTER_VERSION, "r10.7-2026-11-07")
        self.assertEqual(tt.TRADER_CLUSTER_KILL_MIN, 12)
        import entry_watch as ew
        self.assertEqual(ew.GATE_VERSION, fd.FILTER_VERSION)

    def test_12_of_15_kills(self):
        v, R, M = verdict(summ(12))
        self.assertEqual(v, "KILL")
        self.assertEqual(R, ["FUNDER CLUSTER: 12/15 top traders share funder FuNd\u2026abcd"])
        self.assertTrue(M["warns"][0].startswith("\U0001F6A9 FUNDER CLUSTER (HIGH RISK): 12/15"))   # HIGH warn still recorded
        self.assertEqual(fd.tag(R[0]), "FUNDER CLUSTER")

    def test_15_of_15_kills(self):   # SA / AAPLCAT / AgenC / ANAKIN pattern
        self.assertEqual(verdict(summ(15))[0], "KILL")

    def test_11_of_15_pass_with_high_warn(self):
        v, R, M = verdict(summ(11))
        self.assertEqual((v, R), ("PASS", []))
        self.assertNotIn("trader_kill", M)
        self.assertTrue(M["warns"][0].startswith("\U0001F6A9 FUNDER CLUSTER (HIGH RISK): 11/15"))
        self.assertEqual(fd.pass_tier(M["warns"]), "high_risk")
        self.assertEqual(M["top_traders"]["tier"], "funder")

    def test_7_of_15_stays_warn(self):   # VI
        v, R, M = verdict(summ(7, fresh=3))
        self.assertEqual(v, "PASS"); self.assertEqual(fd.pass_tier(M["warns"]), "high_risk")

    def test_fresh_only_15_no_shared_funder_not_killed(self):   # DESSA pattern
        v, R, M = verdict(summ(0, fresh=15, funder=None))
        self.assertEqual((v, R), ("PASS", []))
        self.assertEqual(M["top_traders"]["tier"], "fresh")
        self.assertEqual(fd.warn_tags(M["warns"]), ["flat", "trader_fresh"])
        self.assertIsNone(tt.cluster_kill_reason(summ(1, fresh=15, funder=None)))

    def test_unavailable_data_no_kill(self):
        v, R, M = verdict(summ(0, n=0, error="GeckoTerminal trades unavailable", shared_funder_max=None))
        self.assertEqual(v, "PASS")   # r10.6: still not a KILL, but a HIGH flag
        self.assertTrue(M["warns"][-1].startswith("\u26a0 TRADERS UNCHECKED (HIGH RISK)"))
        self.assertIn("traders_unchecked", fd.warn_tags(M["warns"])); self.assertEqual(fd.pass_tier(M["warns"]), "high_risk")
        self.assertEqual(verdict(summ(0, n=15, shared_funder_max=None, error="HELIUS_API_KEY missing (volume only)"))[0], "PASS")
        self.assertEqual(verdict(exc=RuntimeError("boom"))[0], "PASS")
        saved = os.environ.get("FD_TOP_TRADERS"); os.environ["FD_TOP_TRADERS"] = "0"
        try: self.assertEqual(verdict(summ(15))[0], "PASS")
        finally:
            if saved is None: os.environ.pop("FD_TOP_TRADERS", None)
            else: os.environ["FD_TOP_TRADERS"] = saved
        self.assertIsNone(tt.cluster_kill_reason(None)); self.assertIsNone(tt.cluster_kill_reason({"n": 0}))

    def test_non_pass_untouched(self):
        called = []
        saved = tt.top_traders; tt.top_traders = lambda *a, **k: called.append(1) or summ(15)
        try: v, R = fd.trader_verdict(MINT, None, {}, "KILL", ["x"])
        finally: tt.top_traders = saved
        self.assertEqual((v, R, called), ("KILL", ["x"], []))

    def test_run_overrides_pass(self):
        """End to end through fd.run(): a stage-B PASS with a 13/15 funder cluster is logged as KILL."""
        logged = []
        names = ["rugcheck_health", "fetch_dex", "prefetch_mints", "rugcheck_report", "evaluate_stage_a",
                 "evaluate_stage_b", "entry_timing", "log_check", "cache_flush"]
        saved = {k: getattr(fd, k) for k in names}; saved_tt = tt.top_traders
        fd.rugcheck_health = lambda: True; fd.fetch_dex = lambda ms: {}; fd.prefetch_mints = lambda ms: None
        fd.rugcheck_report = lambda m: {}; fd.evaluate_stage_a = lambda m, rep, p: ([], {"warns": [], "symbol": "SA"})
        fd.evaluate_stage_b = lambda m, rep, p, M: []; fd.entry_timing = lambda p, M: "OK"
        fd.log_check = lambda m, v, R, M: logged.append((m, v, list(R))); fd.cache_flush = lambda: None
        tt.top_traders = lambda m, p=None, record_funders=True: summ(13)
        mode = dict(fd.MODE); fd.MODE.update(bottoms=False, stage_b_max=None)
        try:
            with contextlib.redirect_stdout(io.StringIO()): surv, tally = fd.run([MINT], quiet=True)
        finally:
            for k, f in saved.items(): setattr(fd, k, f)
            tt.top_traders = saved_tt; fd.MODE.clear(); fd.MODE.update(mode)
        self.assertEqual(logged, [(MINT, "KILL", ["FUNDER CLUSTER: 13/15 top traders share funder FuNd\u2026abcd"])])
        self.assertEqual(surv, []); self.assertEqual(tally["FUNDER CLUSTER"], 1)

if __name__ == "__main__":
    unittest.main()
