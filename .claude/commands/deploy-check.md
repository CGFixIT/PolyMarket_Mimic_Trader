# /deploy-check — Pre-Live Deployment Readiness Checklist

Verify all requirements before switching from paper mode to live trading.

## What this does

1. **Environment** — Check `.env` exists and contains `POLY_PRIVATE_KEY`, `BANKROLL`
2. **Config** — Validate `config.yaml` passes Pydantic validation; warn on any defaults that may be too aggressive for first live run
3. **Test suite** — Run `pytest -x -q` and confirm all tests pass
4. **Lint** — Run `ruff check . && ruff format --check .`
5. **Type check** — Run `mypy polymarket_copier/ --ignore-missing-imports`
6. **Paper mode baseline** — Remind user to review paper PnL before going live
7. **Risk parameters review** — Print current values for all risk parameters with a green/yellow/red assessment:
   - `max_trade_pct` ≤ 0.02 = green, ≤ 0.05 = yellow, >0.05 = red
   - `daily_loss_limit_pct` ≤ 0.05 = green, etc.
8. **Connectivity** — Optionally test API reachability (Data API, Gamma API, CLOB) with a single no-auth GET

## Output

A pass/fail checklist:
```
✅ .env present with required keys
✅ config.yaml validates
✅ All 453 tests pass
✅ Lint clean
⚠️  max_trade_pct = 0.03 (consider 0.02 for first live run)
✅ Risk parameters within safe range
```

## Usage

```
/deploy-check
```
