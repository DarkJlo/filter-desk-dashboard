"""r10.7 smart money + dev history (annotation only). Offline.  python3 -m unittest discover tests"""
import os, sys, json, tempfile, unittest
from unittest import mock
HERE = os.path.dirname(os.path.realpath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scanner"))
import filter_desk as fd
import entry_watch as ew
import wallet_intel as wi
import build_smart_wallets as bs

MINT = "TESTmint1111111111111111111111111111111pump"
DEV, A, B_, C = "DevWa11et111111111111111111111111111111111", "WalletA1111111111111111111111111111111111", \
    "WalletB1111111111111111111111111111111111", "WalletC1111111111111111111111111111111111"

def bal(owner, amt, mint=MINT):
    return {"mint": mint, "owner": owner, "programId": wi.TOKEN_PROGRAM, "uiTokenAmount": {"uiAmount": amt}}

def tx(payer, pre, post, logs=(), ins=(), bt=100, err=None):
    return {"blockTime": bt, "meta": {"err": err, "preTokenBalances": pre, "postTokenBalances": post, "logMessages": list(logs)},
            "transaction": {"message": {"accountKeys": [{"pubkey": payer}, {"pubkey": wi.PUMP_PROGRAM}],
                                        "instructions": list(ins)}}}

def create_tx(payer, mint, dev_amt, bt=100):
    return tx(payer, [], [bal(payer, dev_amt, mint), bal("BondingCurve11111111111111111111111111111", 1e9 - dev_amt, mint)],
              logs=["Program log: Instruction: CreateV2"], ins=[{"programId": wi.PUMP_PROGRAM, "accounts": [mint, "x", "y"]}], bt=bt)

class Parsers(unittest.TestCase):
    def test_launch_info(self):
        li = wi.launch_info([create_tx(DEV, MINT, 2e7)], MINT)
        self.assertEqual(li["creator"], DEV); self.assertEqual(li["dev_buy"], 2e7); self.assertTrue(li["pump"])

    def test_early_buyers_only_buys_and_excludes(self):
        txs = [tx(A, [bal(A, 0)], [bal(A, 10)]), tx(B_, [bal(B_, 10)], [bal(B_, 0)]),          # A buys, B sells
               tx(C, [], [bal(C, 5)], err={"x": 1}), tx(DEV, [], [bal(DEV, 3)]), tx(A, [bal(A, 10)], [bal(A, 20)])]
        self.assertEqual(wi.early_buyers(txs, MINT, exclude={DEV}), [A])

    def test_dev_launch_mints(self):
        other = "OtherMint111111111111111111111111111111pump"
        txs = [create_tx(DEV, other, 1, bt=50), create_tx(DEV, MINT, 1, bt=100), create_tx(A, "Xmint", 1),
               tx(DEV, [], [], logs=["Program log: Instruction: Buy"])]
        self.assertEqual(wi.dev_launch_mints(txs, DEV, skip_mint=MINT), [[other, 50]])

class Lines(unittest.TestCase):
    def test_smart_money_count_and_line(self):
        smart = {A: {"wins": ["JOB", "inu"]}, B_: {"wins": ["DESSA", "RAMBO"]}, C: {"wins": ["X", "Y"]}}
        r = wi.smart_money([{"w": A, "hold": True}, {"w": "nobody"}], [B_, C, A], smart)
        self.assertEqual(r["count"], 3); self.assertEqual(r["in_top"], 1); self.assertEqual(r["in_early"], 3)
        self.assertEqual(r["holding"], 1)
        self.assertTrue(r["line"].startswith("Smart money: 3 known winner wallets"))
        self.assertIn("none", wi.smart_money([], [], smart)["line"])

    def test_dev_serial(self):
        d = {"creator": DEV, "prior": 7, "died": 6, "survived": 1, "best_fdv": 80_000, "best_sym": "ZZ", "dev_buy": 1, "sold_pct": 100}
        self.assertEqual(wi.dev_verdict(d), "serial")
        line = wi.dev_line(d)
        self.assertIn("7 prior launches, 6 died <$30K", line); self.assertIn("sold 100%", line); self.assertIn("serial launcher", line)

    def test_dev_selling_alone_is_not_bad(self):
        d = {"creator": DEV, "prior": 0, "died": 0, "dev_buy": 1, "sold_pct": 100}
        self.assertEqual(wi.dev_verdict(d), "ok")
        self.assertEqual(wi.dev_line(d), "Dev: first pump.fun launch seen, dev sold 100% of launch buy")
        self.assertEqual(wi.dev_verdict({"creator": None}), "unknown")
        self.assertIn("n/a", wi.dev_line({"creator": None, "error": "creator unknown"}))

class Build(unittest.TestCase):
    def test_smart_needs_two_independent_wins_and_no_rugs(self):
        sets = {"w1": {A, B_, C}, "w2": {A, B_}, "w3": {C}, "r1": {B_}}
        labels = {"w1": "win", "w2": "win", "w3": "win", "r1": "rug"}
        S = bs.score(sets, labels, bots=set(), groups=bs.link_groups(sets, link_min=15))
        smart = {w for w, s in S.items() if bs.is_smart(s)}
        self.assertEqual(smart, {A, C})   # B_ was early in a rug

    def test_linked_tokens_count_once(self):
        shared = {f"W{i}" for i in range(20)}
        sets = {"w1": set(shared), "w2": set(shared)}
        S = bs.score(sets, {"w1": "win", "w2": "win"}, groups=bs.link_groups(sets, link_min=15))
        self.assertFalse(any(bs.is_smart(s) for s in S.values()))

    def test_bots_excluded(self):
        sets = {f"m{i}": {"BOT", f"u{i}"} for i in range(8)}
        self.assertIn("BOT", bs.bot_set(sets))

class Integration(unittest.TestCase):
    def test_annotation_does_not_change_action(self):
        M = {"liquidity_usd": 50_000, "warns": [], "warn_tags": []}
        tt = {"n": 15, "fresh_lt7d": 0, "shared_funder_max": 0, "line": "x"}
        before = fd.pass_action(M, tt=tt)
        with mock.patch.object(wi, "check", return_value=(
                {"count": 3, "line": "Smart money: 3 known winner wallets (3 early)", "helius_credits": 31},
                {"line": "\u26a0\ufe0f Dev: 7 prior launches, 7 died <$30K", "verdict": "serial"})):
            fd.wallet_intel_check(MINT, M)
        self.assertEqual(M["smart_money"], 3)
        self.assertEqual(fd.pass_action(M, tt=tt), before)
        self.assertEqual(len(fd.intel_lines(M)), 2)

    def test_disabled_env(self):
        M = {}
        with mock.patch.dict(os.environ, {"FD_WALLET_INTEL": "0"}):
            fd.wallet_intel_check(MINT, M)
        self.assertEqual(M, {})

    def test_issue_body_shows_lines(self):
        p = {"timestamp": "2026-11-07T15:00:00+00:00", "mint": MINT, "verdict": "PASS",
             "metrics": {"symbol": "T", "fdv": 100000, "warns": [], "smart_money": 2,
                         "smart_money_line": "Smart money: 2 known winner wallets (2 early)",
                         "dev_history": "Dev: first pump.fun launch seen, dev sold 100% of launch buy"}}
        rec = {"mint": MINT, "symbol": "T", "flipped_at": "2026-11-07T16:00:00+00:00", "first_wait": "2026-11-07T15:00:00+00:00",
               "fdv_at_flip": 110000, "liq_at_flip": 40000, "gate": {"action": "BUY", "action_reasons": []}}
        body = ew.issue_body(rec, p)
        self.assertIn("Smart money: 2 known winner wallets", body)
        self.assertIn("Dev: first pump.fun launch seen", body)

    def test_smart_wallets_file_shape(self):
        p = os.path.join(REPO, "data", "smart_wallets.json")
        if not os.path.exists(p): self.skipTest("no smart_wallets.json")
        j = json.load(open(p))
        for w in j["wallets"]:
            self.assertGreaterEqual(w["independent_wins"], 2); self.assertEqual(w["rugs"], [])

if __name__ == "__main__":
    unittest.main()
