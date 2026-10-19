# Filter Desk rule audit, 2026-10-19

Data used: `data/checks_log.jsonl` (646 rows, 2026-10-18 13:24 → 2026-10-19 08:24 MST) and `data/outcomes.json`,
which I re-ran at ~09:20 MST with the corrected entry price (see Fix 0). Current mcaps come from DexScreener at ~09:10 MST.
Times below are MST (UTC-7). Sample sizes are **tiny** (9 strict passes, 27 control kills, under 24h of
history), so read every rate as a direction, not a statistic.

## Fix 0: outcome multiples were wrong for fast movers (fixed in r8)
`outcomes.py` took the entry price as the **close of the hourly candle that contained the pass**. For tokens
that move a lot inside an hour, that skews every multiple:
- **Vader** showed *peak 3.45x* but never traded above entry. It fell from $1.0M to ~$170k within the hour. True peak is 1.0x.
- **startupSI** showed 5.7x but the true figure is **23.5x peak / 21.3x now** ($1.52M → $32.3M).
- **CATGPT** (control) showed 164x. True figure is 1.15x.

The fix: entry price = FDV logged at check time ÷ supply (DexScreener `fdv/priceUsd`). Peak is floored at 1.0x.
Every number below uses the corrected method.

## Summary: groups (from outcomes.json)

| Group | n | Hit 2x (peak ≥2x) | Rugged/dead | Rugged | Median peak | Median now |
|---|---|---|---|---|---|---|
| **Strict passes (r2–r7)** | 9 | **44.4%** | **11.1%** | 11.1% | 1.26x | 0.91x |
| Kill control (random 5/scan) | 27 | 22.2% | 37.0% | 37.0% | 1.24x | 0.77x |
| Old v1 loose passes | 13 | 30.8% | 61.5% | 46.2% | 1.65x | 0.53x |
| Passes, clean at entry | 7 | 42.9% | 14.3% | | 1.26x | |
| Passes, never warned on any PASS check | 5 | 40.0% | 20.0% | | 1.26x | |
| Passes, warned on some PASS check | 4 | 50.0% | 0% | | 1.91x | |
| Passes made while RugCheck was down ("degraded") | 5 | 60.0% | 0% | | 2.57x | |

The strict filter is about **2x better on hit rate** and about **3x lower on rug/dead** than the random-kill control.
That matches the design intent. (At push time, ~09:35 MST, a new scan had grown the control group to n=31: 25.8% hit 2x, 38.7% rug/dead. The direction is the same. The live numbers are on the dashboard.) Warned passes have not done worse than clean ones (n=4 vs 5, so no conclusion).

## Case studies

| Token | Entry mcap | Peak | Now | Status | First PASS (rules) | Later rechecks |
|---|---|---|---|---|---|---|
| **startupSI** `EKosQw4o…pump` | $1.52M (15:48 Oct 18) | **23.5x** | **21.3x** ($32M) | WINNER | r3, clean, sells/buys 0.60, 4.2h old | 15:52–16:04: *uniform holder cluster 19* KILL (r4 rule; under r5+ the bundle analysis gives 2.15% at 2.8x, a BUNDLE WARN). 16:38 → 08:24: **`lopsided h24 buys/sells` KILL ×7** at 5.1–20.7h old, sells/buys 0.40–0.47, FDV $4.3M → $25M. 08:24 also `FDV/liq 53 > 50`. |
| **WEFT** `HqmFi85a…6Ax8` | $996k (16:42) | 3.68x | 2.16x | WINNER | r5, RugCheck down, LP unverified (meteora) | 17:36, 18:00, 20:53: **bundlers 25–29% ≥20% KILL**. When RugCheck was up: `LP locked 39–48%`, `graph insiders 9`, `insider networks`. Under today's rules **it would be killed**. |
| **Vader** `BxaCEL9…cpump` (user rugged, −60%) | $1.01M (15:30) | 1.0x | 0.00x | RUGGED | **r2** (no flow rules yet), 0.9h old, sells/buys **0.31** | 15:49–16:44: **lopsided KILL ×4** (sells/buys 0.25–0.30, 1.2–2.1h old) plus uniform cluster 13–19. **The r3 lopsided rule catches it**; the r8 gate does not change that (pair < 4h and ratio < 0.38). |
| **PIERRE** `D3S6J9s1…pump` | $933k (17:56) | 2.57x | 0.96x | WINNER → round-tripped | r7, RugCheck down; bundler 0.0% at that check | 20:23, 20:53: PASS + `BUNDLER WARN 18.1%`, `bundled launch 87%`. 08:24: `graph insiders 20` KILL (RugCheck back). Peaked ~$2.4M, then gave it all back (the user's −50% hour). |
| PIERRE (v1) `2Cc5B2ow…` | $263k | 1.35x | 0.01x | RUGGED | v1 loose | n/a: a different token with the same ticker |
| **Doomsday** `GaPbQwLD…pump` | $415k (20:23) | 3.41x | 2.93x | WINNER (open) | r7, RugCheck down, `FUNDING WARN 5.7%`, `BUNDLE WARN 18 wallets 8.47% @1.37x`, bundler 2.9% | Before the pass (15:48–16:31, 1.1–1.8h old): **lopsided KILL ×4** (0.33–0.49). 08:24: **`bundle: cluster of 18 holds 8.5% at 3.6x` KILL** (≥5% held, ≥3x in profit). |
| **MOMO** `FhTRVAkQ…pump` (user +20% cut) | $117k (13:30) | 1.20x | 0.75x | open | r2, clean | 16:04: uniform cluster 9 KILL (old rule); 16:22–16:37: single holder 14% KILL; 16:44–17:56: PASS with `BUNDLE WARN 12.75% @1.0–1.15x` + `BUNDLER WARN 16.5%`. The user's +20% exit was near the 1.2x peak. |
| ZeroFee | $139k | 1.25x | 0.85x | open | r6, RugCheck down, BUNDLE WARN 14.3% | BUNDLER WARN 17.7%; later dead pump KILL |
| godspeed | $133k | 1.26x | 0.83x | open | r7, RugCheck down | graph insiders 26–36 KILL whenever RugCheck was up; dead pump |
| blocus | $43k | 1.10x | 0.91x | open | r4 | single holder 23% KILL, dead pump −93% |

### Kill control by first kill reason (random sample, corrected multiples)
- **Missing DEX liquidity** (5): 5/5 rugged. **Rule helps.**
- **Liquidity < $15k** (5): 2 "winners" (Jeff 9.7x on a $2.4k cap; AMERICA 2.5x then 0.30x). Micro-cap noise; 0 tradeable winners.
- **Graph insiders** (4): 0 winners, 1 rugged, 3 flat or down. **Rule looks fine.**
- **Lopsided** (1, CATONICE, $1.07M, 8h old, sells/buys 0.27–0.31): **rugged to 0**. Shows that age alone must not unlock the rule.
- **Pair age < 30m** (1, CATGPT $287k): rugged.
- **Missing RugCheck report** (3): 1 rugged (UV), 2 flat. These come from outages, not risk signals; r5 already turned RC outage into a warn.
- **Dead pump** (2), uniform (1), top10 (1), high score (1): no winners.

## Which rules helped vs. which cost winners

**Helped (caught rugs):**
- **Lopsided h24 flow** caught Vader (user −60%) on every recheck, plus CATONICE, Piccolo and SOLCAT (all rugged −95% or worse). Vader's PASS came from r2, before the rule existed.
- **Missing DEX liquidity** and **pair age < 30m**: every control kill on these rugged or went to zero.
- **Graph insiders**: no control winners.

**Cost winners or would have:**
- **Lopsided on aged pairs**: startupSI was killed 7 times between 5h and 21h old while running from $4.3M to $25M. **Fixed in r8** (gate below).
- **Bundlers ≥20%**: WEFT (25–29%) is a 3.7x winner that today's rules would kill. That is one case against zero rugs observed in the 20–30% band. **Not changed** (see below).
- **Uniform cluster as a direct KILL** (r4 only) hit startupSI, MOMO, ZeroFee and Doomsday. **Already fixed in r5**: the cluster now only routes to bundle analysis.
- **Bundle ≥5% at ≥3x** killed Doomsday on recheck at 3.6x. That is an "entering now is risky" signal, not a bad pass. Keep it.
- **RugCheck insiders and LP lock**: WEFT, PIERRE and godspeed passed *only* because RugCheck was down. With RugCheck up, all three are KILLs. They went 3.7x, 2.6x (round-trip) and 1.3x. Mixed; see "not yet".

## Implemented (r8-2026-10-19, see docs/RULES_CHANGELOG.md)
1. **Lopsided age and ratio gate** (`scanner/filter_desk.py`):
   `LOPSIDED_SOFT_MIN_AGE_H = 4.0`, `LOPSIDED_SOFT_MIN_RATIO = 0.38`. If h24 sells/buys < 0.5 **and** the pair is ≥4h old
   **and** sells/buys ≥ 0.38, the result is a `LOPSIDED WARN` and not a KILL. Everything else is unchanged.
   Replayed over every lopsided kill in the log:
   - **Flips to WARN:** startupSI (→ 21x), DOGESTER (6.6–11h, 0.41–0.44, now +10%), SI276 (292h, 0.40, flat). No rug flips.
   - **Stays KILL:** Vader (<4h, ≤0.30), CATONICE (8h but 0.27–0.31), Piccolo, SOLCAT, CATGPT (<1h), early Doomsday (<2h), USOE (0.22), and others.
   - The FDV threshold idea was rejected: CATONICE rugged at $1.0–1.3M FDV, so FDV does not separate the cases. The ratio floor does.
2. **Uniform cluster**: confirmed there is no direct KILL since r5. `bundle_verdict` kills only if `held_pct ≥ 5` **and**
   (`multiple ≥ 3` or entry unknown). startupSI's 2.15% → BUNDLE WARN. No change was needed.
3. **Bundler KILL stays at ≥20%; the WARN band 10–20% is now visible.** The dashboard colours BUNDLER/BUNDLE/FUNDING/LOPSIDED
   warnings amber in the main table, and the outcomes table shows warns at pass plus the max bundler % seen later.
4. **Pass tagging**: every PASS logs `pass_tier` ∈ {clean, warn, degraded, degraded+warn} and `warn_tags`.
   Outcomes store `pass_tier`, `warns_at_entry`, `warn_any_pass` and `rules_version`, so clean-vs-warn and A/B segments are automatic.
5. **Outcome entry-price fix** (Fix 0) and peak floored at 1.0x.
6. Verdict caches are keyed by `FILTER_VERSION`, so a rule bump never reuses stale cached verdicts.

## Recommended next (not implemented, need more data)
- **Bundler threshold**: keep 20%. Revisit once ≥10 tokens with 10–30% bundlers are tracked. Today the evidence is WEFT 3.7x (25–29%), PIERRE 2.6x round-trip (18%), MOMO 1.2x (16.5%) and ZeroFee 1.25x (17.7%). Warn-band passes peak but tend to fade (median now ~0.9x), which suggests "take profit early" rather than "kill". The bundler % is also **unstable**: PIERRE measured 0.0% at 17:56 and 18.1% at 20:23. Investigate the measurement before tuning.
- **Track every kill of selected rules, not just 5 random**: add a `kill_rule_watch` group for all bundler ≥20%, lopsided and graph-insider kills with FDV ≥ $250k. This is the cheapest way to get the data the bundler and insider decisions need.
- **FDV/liq > 50**: this hit startupSI at $25M FDV (ratio 53). One data point. Candidate rule: WARN rather than KILL when FDV ≥ $10M and liq ≥ $250k. Wait for more large-cap runners.
- **RugCheck-down passes** were 5 of 9 strict passes last night, including 3 that RugCheck insiders would have killed. Results were good (60% 2x, 0 rugs), but these are not "strict" passes. Fix the RugCheck availability issue first, then compare `degraded` vs normal passes by `pass_tier`.
- **Transient Helius `missing data` kills** (WEFT, ZeroFee: "on-chain holder owners / owner programs") are fail-closed by design. Consider one retry in `onchain_checks` before giving up. Low risk, but it is a behaviour change, so it was left for later.

## What NOT to change yet
- Do not loosen lopsided for young pairs (<4h) or for sells/buys < 0.38. Every rug in this sample sits there.
- Do not drop or raise the bundler KILL (1 winner vs 0 rugs is not evidence).
- Do not relax graph-insider / insider-network kills (0 winners in control; the RugCheck-down passes are confounded).
- Do not relax the bundle ≥5% @ ≥3x rule. It fires on rechecks of runners and means "late entry is risky", which is correct.
- Do not change liquidity, age or volume minimums. Micro-cap "winners" in the control group are not tradeable.

## Addendum ~12:30 MST: Doomsday post-mortem (r9, WARN only)

**What happened.** Doomsday `GaPbQwLDPjSXGR9JxxG1aJLNthuxG58dM2XukBi8pump` was a graduated pump.fun token on PumpSwap (pool `5u57Wo…`).
At ~11:51 MST Oct 19 it had FDV ~$210k and liq ~$41k. By 12:00 MST it had FDV ~$2.9k and pool ~$1.5–3.3k.
RugCheck now reports `lpLockedPct 100`, `lpCurrentSupply 0` (**LP burned**), mint and freeze authority null, metadata immutable, creator balance 0.
The pool holds **86.5% of supply** against ~7 SOL. **Nobody pulled the LP.** About 80% of the supply was sold into the pool within minutes.
That supply sat in many small wallets: at pass time the largest non-LP holder had 0.54%, top10 was 4.8%, and there were ~2,955 holders.
The visible 18-wallet bundle (8.5%) is too small by itself to explain a −99% move.

**Its checks (times MST).**
- 15:48–16:31 Oct 18: KILL ×4. Reasons: `lopsided h24` (0.33–0.49 at 1.1–1.8h old), RugCheck missing, uniform cluster 18.
- **20:23 and 20:53 Oct 18: PASS** (r7). WARNs: `rugcheck unavailable`, `FUNDING WARN: 4 clusters hold 5.7% (largest 2.5%)`, `BUNDLE WARN: 18 wallets hold 8.47% (sold 10%), 1.37x/1.91x`. Other metrics: lp_status burned (PumpSwap), liq $56k/$67k, FDV $415k/$579k (FDV/liq 7.4/8.7), pair age 5.5h/6.1h, on-chain top10 4.84%, top holder 0.54%, Token-2022 with metadata extensions only (no fee, hook or delegate), authorities null, bundler (same-slot) 2.9%, sell impact 1.6%/0.7%, h1 buys/sells 2753/4165 then 3065/597.
- 08:24 and 09:07 Oct 19: KILL by `bundle: cluster of 18 holds 8.5% at 3.6x / 4.1x` (FDV $1.1–1.2M, liq $96–101k, RugCheck back: score 1, LP 100%, insiders 0, creator 0%).
- The GMGN tags the user saw (DEV 25 = serial deployer, "Renames 2") are not collected by the scanner, so they cannot be replayed.

**Signals tested against the log and outcomes (liq ≥ $15k unless noted).**
| Signal at check time | Rugs caught | Winners hit | Verdict |
|---|---|---|---|
| Flat distribution: top10 < 7% and top1 < 1% | 6 rugged to ~0 (Doomsday, Vader, CATONICE, Piccolo, 3oUzvv, PIERRE-v1). 3 more fell 60–97% | **startupSI 24x** (identical numbers to Vader) | **WARN only** (r9 `FLAT WARN`) |
| Bundle held ≥ 5% regardless of multiple (drop the ≥3x condition) | 1 tracked pass (Doomsday). Note it also peaked 3.4x after the pass | 0 ≥2x winners. Kills MOMO (1.2x) and ZeroFee (1.25x) | Not adopted: n=1, and the bundle was not the wallets that dumped |
| Funding WARN ≥ 5% linked | Doomsday only | n/a | Too thin |
| LP lock / burn | none (LP burned) | n/a | Cannot catch this mechanism |
| Uniform cluster ≥ 18 | Doomsday, Vader, CATONICE, MINTER | startupSI (19) | Same problem as flat distribution |
No wallet overlap was found between Doomsday's holders or cluster and any other logged token.

**Decision.** There is no new KILL. Every clean discriminator also fires on startupSI.
The flat-distribution profile is a strong **exit-risk** marker (9 of 10 went to ~0 or −60% or worse), so it is surfaced as a WARN.
The rule for a FLAT WARN pass: small size, take profits into strength, and do not hold through a sell-heavy hour.

**Outcomes.** `outcomes.py` marks RUGGED when liq < $1,000 **or** the current multiple is ≤ 0.10.
Doomsday now has liq ~$3.3k on DexScreener's largest pair, so the liquidity test alone would not fire.
Its multiple is 0.007x vs the $415k entry, so the next hourly run marks it **RUGGED**. No fix was needed.

**PIERRE.** Not a rug as of 12:15 MST: liquidity $82k (PumpSwap) plus about $88k on Meteora, and −48% over 6h. Its top10 is about 20%, so it is not flat.
