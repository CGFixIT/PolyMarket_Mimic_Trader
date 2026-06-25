# /optimizer — Codebase Quality Audit

Run a comprehensive code quality audit of the PolyMarket_Mimic_Trader codebase and produce a prioritized findings report.

## What this does

1. **Static analysis** — Run `ruff check .` and report any lint issues
2. **Type coverage** — Run `mypy polymarket_copier/` and surface type errors
3. **Test health** — Run `pytest -v --tb=short` and report failures or slow tests
4. **Code smell scan** — Search for TODOs, FIXMEs, hardcoded values, silent exception swallows, and missing error context in HTTP calls
5. **Async safety** — Check for unawaited coroutines, missing `await`, and bare `except` blocks
6. **Memory / performance** — Flag unbounded collections, O(n²) patterns, or per-tick DB writes
7. **Security** — Check for secrets in code, command injection risks, and unvalidated external data

## Output format

Produce a ranked findings table:

| # | Severity | File:Line | Finding | Suggested Fix |
|---|----------|-----------|---------|---------------|
| 1 | HIGH | ... | ... | ... |

Then summarize: total issues found, estimated fix time, and which are safe to batch into a single PR.

## Usage

```
/optimizer
```

Optional: `/optimizer --focus=async` to limit scan to async safety checks only.
