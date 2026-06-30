# AGENTS.md

Repository: `PolyMarket_Mimic_Trader`  
Stack: Python 3.10+, async trading bot

Start here:

- Read `CLAUDE.md` before changing trading logic, risk rules, or async orchestration.
- Read `.codex/README.md` for Codex-specific workflow notes.
- Use `.codex/commands/optimizer.md` when asked to audit, optimize, or review quality.
- Use Ponytail when the user asks for the simplest/minimal solution or when pruning over-engineering.

Behavioral baseline:

- Preserve existing safety rules in `CLAUDE.md`, especially range-relative TP/SL, exposure rollback, cold-start guards, and the no-retry rule for failed orders.
- Use async-safe patterns. Avoid blocking calls, unbounded polling work, or sync I/O in event-loop paths.
- Do not alter trading math, position sizing, or execution assumptions without explicit follow-up.
- Prefer small diffs at the real choke point instead of patching symptoms in multiple callers.
- Never hardcode secrets. Keep `.env` local and out of repo.
- Use parameterized SQL or safe library APIs when touching SQLite reads or writes.
- Keep changes production-safe by default. Paper mode stays the default unless the user explicitly requests live-mode work.

Frequently used commands:

- `pip install -r requirements.txt`
- `pytest -v`
- `pytest -v -m "not integration"`
- `ruff check .`
- `python -m mypy polymarket_copier`
- `python -m polymarket_copier.main --mode paper --config config.yaml`

Repo facts:

- Line length is 120 (`pyproject.toml`)
- Ruff is configured; tests use `asyncio_mode = "auto"`
- Existing repo-local quality workflow lives in `.claude/commands/optimizer.md`

Code map:
- `polymarket_copier/main.py`
- `polymarket_copier/core/copier.py`
- `polymarket_copier/core/monitor.py`
- `polymarket_copier/core/risk_manager.py`
- `polymarket_copier/core/portfolio.py`
- `polymarket_copier/core/sizing.py`
- `polymarket_copier/api/*.py`
- `polymarket_copier/config.py`
- `config.yaml`
- `tests/`
