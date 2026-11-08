import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scanner"))
import filter_desk as fd

NOW = 1_762_500_000 // 900 * 900 + 300   # 5 min into a 15m candle
TT = {"n": 15, "shared_funder_max": 2}

def rows(highs, now=NOW):
    """completed 15m candles ending just before the current candle, oldest first"""
    start = int(now // 900) * 900 - 900 * len(highs)
    return [[start + i * 900, h, h, h * 0.9, h, 1] for i, h in enumerate(highs)]

class CrashGuardEval(unittest.TestCase):
    def test_triggers_at_or_below_65pct(self):
        r = fd.crash_guard_eval(rows([1.0, 2.0, 1.5]), 1.3, NOW)
        self.assertTrue(r["hit"]); self.assertAlmostEqual(r["ratio"], 0.65)
    def test_no_trigger_above_65pct(self):
        self.assertFalse(fd.crash_guard_eval(rows([1.0, 2.0, 1.5]), 1.4, NOW)["hit"])
    def test_current_candle_excluded(self):
        rr = rows([1.0, 1.0]) + [[int(NOW // 900) * 900, 5, 5, 1, 1, 1]]   # spike in the forming candle
        self.assertFalse(fd.crash_guard_eval(rr, 1.0, NOW)["hit"])
    def test_older_than_24h_ignored(self):
        old = [[NOW - 30 * 3600, 9, 9, 9, 9, 1]]
        self.assertFalse(fd.crash_guard_eval(old + rows([1.0]), 1.0, NOW)["hit"])
    def test_missing_data(self):
        self.assertIsNone(fd.crash_guard_eval([], 1.0, NOW))
        self.assertIsNone(fd.crash_guard_eval(rows([1.0]), None, NOW))
        self.assertEqual(fd.crash_guard(None, {})["hit"], False)

class CrashGuardAction(unittest.TestCase):
    def test_hit_forces_skip(self):
        m = {"liquidity_usd": 75000, "crash_guard": {"hit": True, "ratio": 0.6}}
        act, r = fd.pass_action(m, tt=TT, tags=[])
        self.assertEqual(act, "SKIP"); self.assertTrue(any("crash guard" in x for x in r))
    def test_no_hit_or_missing_keeps_buy(self):
        for cg in ({"hit": False, "ratio": 0.9}, {"hit": False, "note": "unavailable"}, None):
            m = {"liquidity_usd": 75000}
            if cg is not None: m["crash_guard"] = cg
            self.assertEqual(fd.pass_action(m, tt=TT, tags=[])[0], "BUY")

if __name__ == "__main__": unittest.main()
