# Filter Desk rules changelog

`FILTER_VERSION` lives at the top of `scanner/filter_desk.py`. **Bump it on any rule or threshold change**
and add an entry here. Since r8 every check row in `data/checks_log.jsonl` carries `metrics.rules_version`
(plus `metrics.warn_tags`, and `metrics.pass_tier` on PASS). `scanner/outcomes.py` stores `rules_version`,
`pass_tier` and `warns_at_entry` on every enrolled token, so outcomes can be split by rules version (A/B).

Versions r1–r7 predate the version string. `outcomes.py` infers them from the check timestamp
(`rules_version` ends in `-inferred`). The boundaries are approximate, to within a few minutes.

| Version | Date (UTC) | What changed | Why |
|---|---|---|---|
| r1-v1-loose | 2026-10-18 ≤20:28 | First filter: RugCheck summary only ("No hard fails detected") | bootstrap |
| r2 | 2026-10-18 ~22:30 | Strict RugCheck full report + DexScreener: score, LP lock, danger/warn risks, authorities, top10/top1, insiders, creator balance, liquidity, age, volume, FDV/liq | v1 passed too many rugs (54% rug/dead) |
| r3 | 2026-10-18 ~22:31–22:48 | + h24 lopsided buys/sells KILL (sells/buys outside 0.5–4), h1 zero-sell KILL, Jupiter $100 sell quote | fake-volume / honeypot launches (this would have caught Vader) |
| r4 | 2026-10-18 ~22:56 | + Helius on-chain cross-check (authorities, Token-2022 traps, holders, AMM-PDA exclusion, holder mismatch), uniform cluster = KILL | RugCheck data is sometimes stale or wrong |
| r5 | 2026-10-18 ~23:26 | Uniform cluster is no longer an automatic KILL. It goes to bundle analysis: KILL only if the cluster holds ≥5% **and** sits at ≥3x unrealized (or entry unknown). Otherwise it is a BUNDLE WARN. RugCheck outage becomes a warn instead of a kill. Dead-pump OHLCV check added. | startupSI/MOMO were killed by tiny 2% clusters |
| r6 | 2026-10-19 ~00:07 | + funding-trace (Bubblemaps-style) KILL ≥15% linked / ≥8% largest, WARN ≥5% | sybil clusters |
| r7 | 2026-10-19 ~00:56 | + GMGN-style same-slot bundler KILL ≥20% / WARN ≥10%, bundled-launch WARN; stage A/B pipeline, caches | GMGN bundler % |
| **r8-2026-10-19** | 2026-10-19 | (1) **Lopsided age gate**: a buy-heavy h24 flow (sells/buys < 0.5) becomes a `LOPSIDED WARN` instead of a KILL when pair age ≥ 4h **and** sells/buys ≥ 0.38. Young pairs and extreme ratios still KILL, and so does the sell-heavy side (>4). (2) `FILTER_VERSION` is logged on every check; `warn_tags` on every check; `pass_tier` (clean / warn / degraded / degraded+warn) on every PASS. (3) Verdict caches (trace/bundler) are keyed by version, so no verdict cached under older rules gets reused. (4) Outcomes: entry price = FDV logged at check ÷ supply (was: close of the hourly candle containing the pass). Peak multiple floored at 1.0x. | See `docs/RULE_AUDIT_2026-10-19.md`. startupSI was killed 7× at 5–9h old with sells/buys 0.40–0.47 and then ran ~20x. Every lopsided-killed rug had a pair younger than 4h or sells/buys ≤ 0.31 (Vader 0.25–0.30, CATONICE 0.27–0.31, Piccolo 0.22, SOLCAT 0.10). The old outcome entry price showed Vader as a 3.4x "peak" when it never got above entry. |

## How to bump
1. Change rules in `scanner/filter_desk.py`.
2. Set `FILTER_VERSION = "r9-YYYY-MM-DD"`.
3. Add a row above with what changed and why, citing data where possible.
4. Compare later: `outcomes.json` → `tokens[*].rules_version`.
