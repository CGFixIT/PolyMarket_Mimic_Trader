# /pr-sweep — PR & CI Status Sweep

Check all open pull requests for CI status, merge conflicts, and review comments. Auto-fix any failing CI that is tractable.

## What this does

1. List all open PRs via `mcp__github__list_pull_requests`
2. For each open PR, fetch its latest workflow runs via `mcp__github__actions_list`
3. For any failed runs, fetch job logs and diagnose the failure
4. If the failure is a lint/format/test issue that can be fixed in <50 LOC:
   - Checkout the branch, apply the fix, run tests locally, push
5. Report a status table for all PRs

## Output format

| PR | Title | CI | Conflicts | Action Taken |
|----|-------|----|-----------|--------------|
| #67 | fix: ... | ✅ passing | No | None needed |
| #68 | feat: ... | ❌ lint fail | No | Fixed and pushed |

## Usage

```
/pr-sweep
```

Runs in the background; reports results when done.
