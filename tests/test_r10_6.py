"""r10.6: lopsided soft band = KILL, missing top traders = HIGH flag, entry label without near-ATH, PASS action BUY/SKIP
+ exit plan. Offline (network monkeypatched).  python3 -m unittest discover tests"""
import os, sys, io, json, time, shutil, tempfile, contextlib, unittest
HERE = os.path.dirname(os.path.realpath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scanner"))
import filter_desk as fd
import top_traders as tt

MINT = "TESTmint1111111111111111111111111111111pump"
CLEAN = {"n": 15, "fresh_lt7d": 1, "shared_funder_max": 0, "cex_funded": 8, "still_holding": 9, "sold_out": 2, "error": None}

def met(liq=50000, warns=(), tt_=CLEAN):
    m = {"liquidity_usd": liq, "fdv": 400000, "warns": list(warns)}
    if tt_ is not None: m["top_traders"] = tt_
    return m

class Action(unittest.TestCase):
    def act(self, **kw):
        m = met(**kw); return fd.pass_action(m)

    def test_buy_clean(self):
        a, r = self.act()
        self.assertEqual(a, "BUY"); self.assertIn("liquidity $50K", r)

    def test_liquidity_threshold(self):
        self.assertEqual(fd.ACTION_MIN_LIQ_USD, 30_000)
        self.assertEqual(self.act(liq=30000)[0], "BUY")
        a, r = self.act(liq=29999); self.assertEqual(a, "SKIP"); self.assertIn("liquidity $30K < $30K", r)
        self.assertEqual(self.act(liq=None)[0], "SKIP")

    def test_funder_cluster_5_to_11_skips(self):
        for k in (5, 8, 11):
            a, r = self.act(tt_=dict(CLEAN, shared_funder_max=k))
            self.assertEqual(a, "SKIP"); self.assertIn(f"funder cluster {k}/15 (HIGH)", r)
        self.assertEqual(self.act(tt_=dict(CLEAN, shared_funder_max=4))[0], "BUY")

    def test_missing_traders_skips(self):
        for t in (None, {"n": 0, "error": "GeckoTerminal trades unavailable"},
                  {"n": 15, "error": "HELIUS_API_KEY missing (volume only)"}):
            a, r = self.act(tt_=t); self.assertEqual(a, "SKIP"); self.assertIn("top traders unchecked (HIGH)", r)
        # partial Helius errors with wallet data still count as checked (same as the entry-watch gate)
        self.assertEqual(self.act(tt_=dict(CLEAN, error="2 wallets failed"))[0], "BUY")

    def test_flat_skips_unless_fresh_only(self):
        a, r = self.act(warns=["FLAT WARN: x"]); self.assertEqual(a, "SKIP"); self.assertIn("flat holders (wallet-farm risk)", r)
        fresh = dict(CLEAN, fresh_lt7d=12, shared_funder_max=1)
        self.assertEqual(self.act(warns=["FLAT WARN: x", tt.warn_text(fresh)], tt_=fresh)[0], "BUY")
        # fresh numbers but a funder cluster too -> funder tier, flat still counts
        both = dict(CLEAN, fresh_lt7d=12, shared_funder_max=6)
        self.assertEqual(self.act(warns=["FLAT WARN: x"], tt_=both)[0], "SKIP")

    def test_legacy_record_tags(self):
        m = {"liquidity_usd": 80000, "warn_tags": ["trader_cluster"]}   # legacy tag without numbers -> HIGH
        self.assertEqual(fd.pass_action(m)[0], "SKIP")
        self.assertEqual(fd.pass_action({"liquidity_usd": 80000, "warn_tags": ["lopsided"]}, tt=CLEAN)[0], "SKIP")

    def test_exit_plan(self):
        ep = fd.exit_plan(400000, "2026-11-05T15:00:00+00:00")
        self.assertEqual((ep["take_profit_fdv"], ep["stop_fdv"]), (800000, 200000))
        self.assertEqual(ep["sell_by"], "2026-11-06T15:00:00+00:00")
        self.assertEqual(ep["text"], "Buy near FDV $400K. Sell 100% at 2x (FDV $800K). Stop: sell all at FDV $200K (-50%). "
                                     "Otherwise sell everything at 24h (Nov 06 08:00 MST).")
        self.assertIsNone(fd.exit_plan(None))

class LogCheck(unittest.TestCase):
    def logged(self, verdict, M):
        d = tempfile.mkdtemp(); saved = fd.LOG_PATH
        try:
            fd.LOG_PATH = os.path.join(d, "log.jsonl")
            fd.log_check(MINT, verdict, [] if verdict == "PASS" else ["x"], M)
            with open(fd.LOG_PATH) as f: return json.loads(f.readline())
        finally:
            fd.LOG_PATH = saved; shutil.rmtree(d)

    def test_pass_buy_logged_with_plan(self):
        r = self.logged("PASS", met())
        self.assertEqual(r["action"], "BUY"); self.assertEqual(r["metrics"]["action"], "BUY")
        self.assertTrue(r["metrics"]["action_reasons"])
        self.assertEqual(r["metrics"]["exit_plan"]["take_profit_fdv"], 800000)
        self.assertEqual(r["metrics"]["rules_version"], "r10.7-2026-11-07")

    def test_pass_missing_traders_high_skip(self):
        r = self.logged("PASS", met(tt_=None))
        self.assertEqual(r["action"], "SKIP"); self.assertNotIn("exit_plan", r["metrics"])
        self.assertIn("traders_unchecked", r["metrics"]["warn_tags"])
        self.assertEqual(r["metrics"]["pass_tier"], "high_risk")
        self.assertTrue(r["metrics"]["warns"][0].startswith("\u26a0 TRADERS UNCHECKED (HIGH RISK)"))

    def test_kill_has_no_action(self):
        r = self.logged("KILL", met(tt_=None))
        self.assertNotIn("action", r); self.assertNotIn("traders_unchecked", r["metrics"].get("warn_tags") or [])

class Lopsided(unittest.TestCase):
    def test_soft_band_is_kill(self):
        R, M = [], {"warns": []}
        # replicate the stage-A branch with a soft-band pair: 5h old, buys/sells 1000/400 (ratio 0.40)
        b, s, age = 1000, 400, 300
        ratio = s / b
        self.assertTrue(fd.LOPSIDED_SOFT_MIN_RATIO <= ratio < fd.MIN_SELL_BUY_RATIO and age >= fd.LOPSIDED_SOFT_MIN_AGE_H * 60)
        src = open(os.path.join(REPO, "scanner", "filter_desk.py")).read()
        self.assertNotIn('M["warns"].append(f"LOPSIDED WARN', src)
        reason = f"lopsided h24 buys/sells {b}/{s} (ratio {ratio:.2f}, pair {age/60:.1f}h old; r8 warn band, r10.6 KILL)"
        self.assertIn('r8 warn band, r10.6 KILL)")', src)
        self.assertEqual(fd.tag(reason), fd.tag("lopsided h24 buys/sells 10/2"))

class EntryTiming(unittest.TestCase):
    def run_et(self, closes_15m, closes_5m, ath):
        now = time.time()
        def rows(closes, step):
            n = len(closes); return [[now - (n - i) * step, c, c * 1.01, c * 0.99, c, 1000] for i, c in enumerate(closes)]
        saved = (fd.gecko_get, fd.price_action)
        def gg(url, cache_ttl=None):
            lst = rows(closes_5m, 300) if "aggregate=5&" in url else rows(closes_15m, 900)
            return {"data": {"attributes": {"ohlcv_list": lst}}}
        fd.gecko_get = gg; fd.price_action = lambda p, M: M.update(ath_price=ath)
        try:
            M = {}; lab = fd.entry_timing({"pairAddress": "POOL", "priceUsd": str(closes_15m[-1])}, M); return lab, M
        finally:
            fd.gecko_get, fd.price_action = saved

    def test_near_ath_no_longer_waits(self):
        self.assertFalse(hasattr(fd, "ENTRY_NEAR_ATH_FRAC"))
        # zigzag uptrend: a dip every 8 candles, each dip higher; price ends ~2% under the high (r10 would say WAIT)
        up = [1 + 0.02 * i - (0.06 if i % 8 == 4 else 0) for i in range(60)]
        lab, M = self.run_et(up, up, ath=max(up) * 1.02)
        self.assertEqual(lab, "OK", M.get("entry_reasons"))
        self.assertNotIn("near ATH", " ".join(M["entry_reasons"]))
        self.assertTrue(M["entry_reasons"][0].startswith("holding higher low ("))
        self.assertIn("% below high)", M["entry_reasons"][0])

    def test_sliding_still_waits(self):
        down = [2 - 0.02 * i for i in range(60)]
        lab, M = self.run_et(down, down, ath=2.5)
        self.assertEqual(lab, "WAIT")

if __name__ == "__main__":
    unittest.main()
