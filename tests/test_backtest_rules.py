import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scanner"))
import backtest_rules as br

class T(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(br.classify(100, 250, 50), "win"); self.assertEqual(br.classify(100, 120, 160), "win")
        self.assertEqual(br.classify(100, 150, 20), "rug"); self.assertEqual(br.classify(100, 150, 80), "flat")
    def test_first_checks(self):
        rows = [{"mint": "A", "verdict": "KILL", "reasons": ["graph-detected insiders: 3"]},
                {"mint": "A", "verdict": "KILL", "reasons": ["graph-detected insiders: 9", "lp unlocked"]},
                {"mint": "B", "verdict": "KILL", "reasons": ["graph-detected insiders: 3", "lp unlocked"]},
                {"mint": "C", "verdict": "PASS", "reasons": [], "metrics": {"warn_tags": ["flat"]}}]
        self.assertEqual(set(br.first_checks(rows, "KILL", "insider", None, None)), {"A", "B"})
        self.assertEqual(set(br.first_checks(rows, "KILL", "insider", "insider", None)), {"A"})
        self.assertEqual(set(br.first_checks(rows, "PASS", None, None, "flat")), {"C"})

if __name__ == "__main__": unittest.main()
