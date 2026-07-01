# Can PolyMarket_Mimic_Trader Actually Make Money? — Consolidated Findings

**Date:** 2026-06-30
**Question:** *If this Polymarket Python copy-trading bot ran locally with real money attached, could it realistically be net-profitable?*
**Method:** ~3 min maintainer code read of `origin/main` (core modules, config, planning docs, 47+ PR progression) followed by three parallel analyses — (A) a product/strategy brainstorm, (B) a scientific audit of the assumptions embedded in the code/logic/plan/PR history, and (C) cited deep web/academic/market/financial research. This document consolidates and reconciles all three.

---

## TL;DR — Bottom line

**Conditional NO.** As written, run locally with real USDC, this bot is **unlikely to be net-profitable**, and for a US-based operator it **probably cannot legally place a real trade against the venue it targets** without a non-trivial port.

The crucial distinction every analysis reached independently:

> **The engineering is genuinely strong. The edge is structurally weak — not because the code is wrong, but because the *signal it consumes is public, delayed, and decays faster than an 8-second poller can act on it.***

The risk machinery (range-relative TP/SL, exposure caps, daily-loss breaker, TOCTOU/exit locks, fail-closed gating, fill reconciliation) is well above hobby-grade and would **not** be the reason it loses money. It is excellent risk plumbing around a signal that is too slow and too costly to extract alpha from.

**Most likely real-money outcome:** a slow bleed (≈ break-even minus friction), with a ~15–25% chance of small sustained profit *if* it could trade live at scale — and even in that winning tail the magnitude is trivial (~$10–30/month on a $500 bankroll).

---

## 1. The edge thesis and why it doesn't survive copying

**What the code assumes:** the README states "top performers consistently outperform," and `tracker.py` selects "smart money" by scoring Sharpe-proxy × consistency × recency (weighted sum 40/35/25), dual-window (all-time **and** 30d), ROI-based, expectancy-gated. The premise: leaderboard-derived skill is real and the bot can mirror it.

**What the evidence says (reconciled across B and C):**

- **Skill is real and unusually persistent here.** A new full-history academic study (Gómez-Cram, Guo, Jensen, Kung — *"Prediction Market Accuracy: Crowd Wisdom or Informed Minority?"*, SSRN #6617059, Apr 2026; 98,906 events / 210k markets / $13.76B volume) finds ~**3.14% of accounts are "skilled winners"** whose order flow predicts outcomes, and crucially **44% of in-sample skilled accounts stay skilled out-of-sample — vs. only 10% for mutual-fund managers.** So "smart money" as a category is validated, and the bot's instinct to filter for it is correct.
- **But raw leaderboard rank is mostly luck.** Only **~12% of top *earners* overlap with the genuinely skilled** group; ~60% of "lucky winners" become losers on a fresh sample; ~40% top-20 rank retention at 90 days. A naive raw-PnL mirror chases luck and one-off event whales (e.g. the 2024 "Théo" $80–85M Trump bet). **The bot's Sharpe/consistency/recency/dual-window scoring is the right partial fix** — this is the strongest part of the design.
- **The skill is, however, a *speed* edge that a copier cannot inherit.** The same study: skilled traders earn alpha by *being first to react to public news* and *arbitraging fast*. The reporting explicitly notes this edge "would be difficult to replicate by observing their trades after execution." Insider-style edges (the study flagged ~1,950 suspected insider accounts moving prices 7–12× harder per dollar) are **uncopyable** — the insider closes on resolution using information you never had.

**The copy mechanism strips the edge two ways:**

1. **Adverse selection / latency (you buy post-impact).** The signal arrives only after: on-chain settle + Data-API index (~1–3s) → 8s poll (+≤2s jitter) → decision/fetch/revalidate (~0.1–0.5s) → order. Realistic **signal age at fill ≈ 2–13s**. By then the whale's own order has already moved the book, so the bot systematically buys the *post-impact* price — eating the whale's slippage without the whale's timing. The `max_trade_age_seconds: 12` staleness gate is nearly self-defeating: tighten it and you starve the bot; loosen it and you worsen adverse selection. An 8s REST poller sits **at the back of the queue** behind anyone running a mempool/indexer watcher on the same *public* wallets.
2. **Holding-period mismatch (you exit before the edge realizes).** A large share of genuine Polymarket alpha is *buy-mispriced-token-and-hold-to-resolution* — `tracker.py` even handles redemption records for exactly this. But this bot **cannot hold to resolution**: `resolution_blackout_hours: 24` force-exits before resolve and `time_exit_hours: 48` ejects stale positions. It copies the *entry* but not the *strategy*, converting a slow-resolving value bet into a noisy short-term mean-reversion bet the source never intended.

---

## 2. Costs — no longer a near-frictionless venue

**Reconciled fee picture (the A/B/C discrepancy resolved):** Polymarket was historically **0% fees**; that changed in 2026. The real fee follows a **concave `Θ·C·p·(1−p)` form — it peaks near p=0.50 and shrinks toward ~zero at the price extremes**, with **makers paying ~0 (and earning rebates)** and some categories (geopolitics/world events) **fee-free**:

- **Polymarket US (regulated DCM):** taker `Θ=0.05`, max ≈ **$1.25 / 100 contracts at $0.50**, maker rebate ≈ −$0.31/100. Effective from 3pm ET Apr 3, 2026.
- **Global crypto CLOB (the venue the bot's endpoints target):** category taker caps ≈ **$0.75 sports / $1.00 politics-finance-tech / $1.25 economics-culture-weather / $1.80 crypto** per 100 shares; geopolitics free; sells not charged taker.

**Two errors in the bot's cost model:**

- **Curve shape is inverted (the substantive error, per B).** `config.py`'s flat `paper_taker_fee_pct: 0.02` / `round_trip_fee_pct: 0.045` drives the H5 post-fee-edge gate and the H7 entry-price band, whose *mental model* is "extremes are expensive after fees." **Reality is the opposite** — extremes are nearly fee-free; the *middle* (~$0.50) is where fees bite. So the bot **rejects profitable extreme-price copies and accepts marginal mid-price copies it shouldn't.**
- **Magnitude is roughly right only at mid-price (per C).** The flat 2% is in the right ballpark near $0.50 and *overstates* fees near the extremes (by tens of ×). Not catastrophic for fee magnitude — but the *real* cost driver is **spread + slippage**, because the bot is always a **taker** (FOK entries / FAK exits cross the book, never rest as maker, so it pays full taker and earns zero rebate).

**Full round-trip friction for a spread-crossing taker:** fees (~1.25–2.5% at mid) **+ spread** (often <1¢ on the most liquid markets but ~5¢ mid-tier, 10¢+ thin — Kaiko Feb 2026 found single Deribit BTC strikes exceed *total* Polymarket depth by 20–40×) **+ slippage**. Realistic **≈ 3–6%+ round-trip**, more on thin books. A small, decayed copy edge struggles to clear that on every trade. (Gas on Polygon is subsidized via relayers → ~0 for the user; the one cost the bot can ignore.)

---

## 3. Capital & realistic ROI — the dollar edge is trivial

With **$500 bankroll** and `max_trade_pct: 0.02` → **$10 max position**; per-trader 5% = $25; per-market 8% = $40; total-exposure 30% = $150; max 10 concurrent. At full deployment ≈ **$100–150 at risk**. Modeling ~100 round-trips/month at ~$10 (after the dense gate stack: stale / blackout / volume≥$5k / price-band / post-fee / exposure):

| Scenario | Net edge / round-trip (after ~3.5% cost) | Monthly P&L | Monthly ROI on $500 |
|---|---|---|---|
| Optimistic | +1.5% | +$15 | **+3.0%** |
| Base | ~0% (edge ≈ cost) | ~$0 ± noise | **~0%** |
| Pessimistic | −2% (adverse selection > edge) | −$20 | **−4.0%** |

Even the optimistic case is **~$15/month** — dwarfed by the operational overhead (tmux babysitting, WS-reconnect monitoring, circuit-breaker alerting, paper≠live divergence). **The strategy does not scale cleanly either:** the bot's own M11 sqrt-impact slippage model kicks in above $500 notional, and larger orders move thin prediction-market books against you. High variance on ~100 tiny bets means a positive *month* proves nothing.

---

## 4. Code-level assumptions that need correction (audit highlights)

| # | Assumption in code | Verdict | Correction / note |
|---|---|---|---|
| 4.1 | Flat 2% / 4.5% fees; extremes are fee-expensive (H7 band, low-entry TP taper) | **Inverted** | Replace with real `feeRate·p·(1−p)` curve keyed by category (geopolitics=0); re-derive H5/H7. Highest-leverage, smallest-effort fix. |
| 4.2 | Observed `win_rate` / `mean_roi` reflect trader skill | **Biased UP (acknowledged in `tracker.py`)** | Winning held-to-resolution positions emit a redeem record (counted); losers expiring worthless emit none (uncounted) → win-rate and ROI both inflated. This contaminates selection **and** the Kelly edge seed (H18). Real fix needs on-chain ledger reconstruction of worthless expiries. |
| 4.3 | ROI→edge→Kelly chain (`sizing.py`: `edge=mean_roi·price`, `p=price+edge`) | **Math sound, input poisoned** | Algebra is correct and safety rails (0.5 shrink, 0.20 cap, 0.25 fractional, 2% hard cap, time-decay) are genuinely conservative — but they blunt variance, not the *directional* biases (4.2 + selection). Net: Kelly will oversize whenever the inflated edge isn't real; the 2% hard cap is what actually saves it, making Kelly mostly decorative. Correctly **off by default**. |
| 4.4 | Range-relative TP/SL (40% upside / 25% downside) + trailing + time-exit + source-exit | **Under-examined; likely EV-destructive** | The 40/25 fractions are arbitrary and uncorrelated with *why the whale entered*. Four competing exit logics override the pure mirror, the classic recipe for **negative skew** (keep the fast stop-out losses, truncate the winners via early TP). Decide empirically (backtest) between pure-mirror and a *trader-derived* exit. |
| 4.5 | "30 days positive paper PnL" = go-live gate (`next_steps.md`) | **Non-predictive** | Paper mode uses a **synthetic order book** (fixed bid 0.50/ask 0.51), **always-full FOK fills**, and the **wrong (flat) fee curve**. It never models no-fill/partial-fill (which systematically removes the best live trades), thin-book rejection, or market impact. Green paper PnL is necessary-but-wildly-insufficient — arguably not even necessary. |
| 4.6 | Self-measurement can validate the strategy | **It cannot, as built** | Both the tracker signal (4.2) and the bot's own copy win-rate (shaped by *its* TP/SL, not the trader's edge) are circular. Only an external ground truth — an offline backtest on held-out history — can break the loop. |
| 4.7 | **Config drift (CONFIRMED latent mis-config):** `config.yaml` ships `trailing_stop_fraction: 0.15` and `half_life_days: 14`, overriding the H1/L4 "fixes" | **Confirmed** | `config.py` Pydantic models default to the post-fix values (`trailing_stop_fraction: float = 0.40  # H1: loosened (was 0.15)`, `half_life_days: float = 7.0`) and the YAML keys match the field names exactly, so the YAML **overrides the fixes at load time**. With the new trailing formula, `0.15` gives a *tight* trail the H1 comment says "exits on ~7% pullback which Polymarket tokens hit on normal noise"; `14d` half-life lets dormant traders retain score. The shipped config silently runs the **pre-fix** behavior. A one-line config fix. |

---

## 5. Regulatory, access & security (real-world blockers)

- **US access (reconciled):** The blanket blocker eased in 2025 — DOJ/CFTC ended their investigations (Jul 2025), Polymarket acquired the **CFTC-licensed QCEX** ($112M) and launched **Polymarket US** (DCM) with intermediated US access approved (CFTC amended Order of Designation, Nov 25, 2025); **ICE invested up to ~$2.6B**. So a US person *can* now legally trade real money — but typically via the **regulated DCM**, in permitted states (reported exclusions include AZ, IL, MA, MD, MI, MT, NJ, NV, OH), with **Kalshi the cleaner regulated alternative** (bank funding, no wallet).
- **Venue mismatch (the real code blocker):** the bot hardcodes the **international crypto CLOB** (`clob.polymarket.com`, `data-api.polymarket.com`, `ws-subscriptions-clob...`), which **still geoblocks US IPs and checks before every order submission**. Polymarket US is a *separate exchange* with its own books, listed markets, fee math, and auth. Copying an international-leaderboard whale and routing the order to the US DCM is a venue mismatch — the same token may not even exist. **Adapting this bot for legal US real-money use is a non-trivial rewrite, not a config change**, and whether automated trading on the targeted CLOB is permitted for a US person is an open compliance question.
- **⚠️ Security (ecosystem-level):** StepSecurity documented a hijacked verified GitHub org distributing **20+ malicious Polymarket copy-bot repos** that **read the private key from `.env` and exfiltrate it** (typosquatted npm dep, SSH backdoor); related phishing losses ~$3M. *This repo is your own and not implicated*, but it underscores: **never run an off-the-shelf Polymarket bot with a funded key**, and keep `POLY_PRIVATE_KEY` on a low-balance, single-purpose wallet.

---

## 6. Where the three analyses agreed, and where they updated each other

- **Unanimous:** code quality high; edge weak after latency + cost; paper-mode go-live gate is misleading; do not deploy real money without an offline backtest first.
- **C updated B on persistence:** B leaned "leaderboard edge is mean-reverting"; C's primary-source Yale study shows *true skill* persists unusually well (44% OOS) once luck is filtered — so the premise isn't dead, the **copyability** is the problem. The bot's skill-scoring is vindicated as the right instinct.
- **C tempered A/B on fees:** the flat 2% is closer to reality at mid-price than "totally wrong" implies; the durable criticisms are the **inverted curve shape** and that **spread+slippage (not fees) is the dominant taker cost.**
- **Honest uncertainty:** the widely-cited "7.6% of wallets profitable" is a secondary (Dune/Medium) figure; vendor persistence numbers are unaudited; the Yale paper is an Apr-2026 working paper (not yet peer-reviewed) though corroborated by Solidus Labs / WSJ reporting (<1% of wallets take ~half the profits).

---

## 7. The highest-leverage UNRESOLVED problems ("not fully optimized")

Ranked by leverage ÷ effort:

1. **Fix the fee model** → real `feeRate·p·(1−p)` curve by category (geopolitics=0); re-derive the H5/H7 gates against it. Cheapest, most clearly-correct fix.
2. **Build the offline backtest harness (F6) and make it the real go-live gate** — measure the *selected cohort's forward ROI* net of the real fee curve, and A/B *pure-mirror vs. TP/SL-overlaid* exits on held-out data. Nothing else can be validated without this.
3. **Debias trader metrics** by reconstructing worthless-expiry losses from on-chain resolution + unredeemed holdings, so win-rate / mean-ROI / Kelly-seed stop being systematically inflated.
4. **Resolve the exit-logic conflict** — empirically choose pure-mirror vs. a *trader-derived* exit; stop trailing/time/TP overlays from truncating copied wins. Fix the §4.7 config drift.
5. **Measure post-copy price drift** at the realistic 2–13s fill age to confirm the signal is still alive; gate copies to regimes where the whale's edge persists for hours, not seconds. (And to compete at all, replace REST polling with a sub-second indexer/mempool watcher.)

---

## 8. Decision points for you (further direction)

Pick any combination — these are the natural next moves:

- **(a) Validate before risking a cent** — build the backtest harness (#2) and the de-biased metrics (#3); treat real-money go-live as gated on *that*, not paper PnL. *(Recommended first step.)*
- **(b) Cheap correctness fixes now** — land the fee-curve fix (#1) and the config-drift fix (§4.7) as small PRs regardless of the strategic question.
- **(c) Re-architect the edge** — sub-second detection + maker-rebate redesign + hold-to-resolution alignment (a different, more defensible bot).
- **(d) Solve access** — scope a Polymarket-US (or Kalshi) port; confirm whether automated trading is even permitted for your state/venue.
- **(e) Treat it as a learning artifact** — keep it paper-only; it's an excellent codebase to study, a poor one to fund.

My recommendation: **(a) + (b)** — do the two cheap correctness fixes, then build the backtest as the honest arbiter, *before* any real capital. Tell me which you want and I'll start.

---

*Prepared by consolidating three parallel analyses against the `origin/main` codebase. Not financial or legal advice. The bot's `2% fee` and `clob.polymarket.com` assumptions are materially out of date as of mid-2026; verify all venue/fee/regulatory specifics against current primary sources before acting.*
