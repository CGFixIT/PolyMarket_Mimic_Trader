# agents.md

Repository: `PolyMarket_Mimic_Trader`  
Stack: Python 3.10+, async trading bot

Behavioral baseline for this project:

- Use async-safe patterns; avoid blocking calls in event loops.
- Preserve existing safety rules in `CLAUDE.md` (especially risk handling and order behavior).
- Do not alter trading/math assumptions without explicit follow-up.
- Prefer typed structures, small diffs, and explicit rollback points for behavior changes.
- Never hardcode secrets. Keep `.env` local and out-of-repo.
- Use parameterized SQL (or ORM-safe APIs) when touching DB write/query code.
- Keep changes minimal and production-safe by default.

Frequently used commands:

- `pip install -r requirements.txt`
- `pytest -v`
- `pytest -v -m "not integration"`
- `ruff check .`
- `python -m polymarket_copier.main --mode paper --config config.yaml`

Code map to inspect first on follow-up:
- `polymarket_copier/main.py`
- `polymarket_copier/core/copier.py`
- `polymarket_copier/core/risk_manager.py`
- `polymarket_copier/core/portfolio.py`
- `polymarket_copier/api/*.py`
- `config.yaml`
- `tests/`
