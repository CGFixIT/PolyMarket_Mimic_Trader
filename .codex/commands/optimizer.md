# Optimizer

Use this repo-local workflow when the user asks to optimize, audit, harden, or review code quality.

Scope:

- `ruff check .`
- `python -m mypy polymarket_copier`
- `pytest -v --tb=short`
- targeted grep for TODO/FIXME, bare `except`, silent HTTP failures, async misuse, and obvious performance leaks

Output:

- Ranked findings first
- File and line references
- Minimal safe fixes, not broad rewrites

Notes:

- This mirrors the existing `.claude/commands/optimizer.md` workflow so Codex and Claude use the same audit shape.
- For over-engineering cleanup, pair this with the Ponytail skill instead of inventing new abstractions.
