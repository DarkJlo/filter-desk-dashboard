import os, sys, json, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scanner"))
import wallet_intel as wi

class DevTier(unittest.TestCase):
    def test_bad_serial_flops(self):   # backtest shapes: zen / Dodo
        self.assertEqual(wi.dev_tier([3e3, 7e3, 7e3, 11e3, 10e3, 5e3, 7e3]), "bad")
        self.assertEqual(wi.dev_tier([6e3, 51e3, 7e3, 85e3, 8e3, 8e3, 9e3, 7e3]), "bad")
    def test_good_runner_dev(self):
        self.assertEqual(wi.dev_tier([450e3, 1.2e6, 20e3, 800e3]), "good")
    def test_neutral(self):
        self.assertEqual(wi.dev_tier([]), "neutral")
        self.assertEqual(wi.dev_tier([7e3, 10e3]), "neutral")            # BNICE: only 2 prior
        self.assertEqual(wi.dev_tier([400e3, 20e3, 150e3, 30e3]), "neutral")
    def test_unknown(self):
        self.assertEqual(wi.dev_tier(None), "unknown")
        self.assertEqual(wi.dev_tier([None, None, 5e3, None]), "unknown")
    def test_lines(self):
        d = {"creator": "C", "prior": 4, "died": 1, "peaks": [450e3, 1.2e6, 20e3, 800e3], "peak_hours_med": 3,
             "tier": "good", "dev_buy": 1, "sold_pct": 100}
        s = wi.dev_line(d)
        self.assertIn("3 ran to $300K+", s); self.assertIn("GOOD DEV", s); self.assertIn("~3h", s)
        d = {"creator": "C", "prior": 7, "died": 7, "peaks": [3e3] * 7, "tier": "bad", "dev_buy": 1, "sold_pct": 100}
        s = wi.dev_line(d); self.assertIn("BAD DEV", s); self.assertNotIn("serial launcher", s)
    def test_record_good_dev(self):
        p = os.path.join(tempfile.mkdtemp(), "g.json")
        d = {"creator": "C", "prior": 3, "peaks": [5e5, 6e5, None]}
        wi.record_good_dev(d, "M1", p); wi.record_good_dev(d, "M1", p); wi.record_good_dev(d, "M2", p)
        j = json.load(open(p)); self.assertEqual(j["devs"]["C"]["seen_on"], ["M1", "M2"])
    def test_gt_peak_cached(self):
        self.assertEqual(wi.gt_peak("X", {"peak:X": [123.0, 2.0]}), (123.0, 2.0))

if __name__ == "__main__": unittest.main()


class TestBadDevSkip(unittest.TestCase):
    def test_bad_dev_forces_skip(self):
        import filter_desk as fd
        tt = {"n": 15, "shared_funder_max": 2}
        m = {"liquidity_usd": 167000, "dev_tier": "bad"}
        act, r = fd.pass_action(m, tt=tt, tags=[])
        self.assertEqual(act, "SKIP")
        self.assertTrue(any("bad dev" in x for x in r))
