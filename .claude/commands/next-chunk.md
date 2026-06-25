# /next-chunk — Implement the Next Improvement

Find the highest-priority unimplemented item from `next_steps.md` and implement it as a clean PR.

## What this does

1. Read `next_steps.md` to identify the next unimplemented item (Tier A first, then B, then C)
2. Check `git log origin/main --oneline` to confirm it hasn't already been merged
3. Create a new branch: `git checkout -b feat/<short-name> origin/main`
4. Implement the improvement with tests
5. Run the full test suite + lint
6. Commit with a clear message
7. Push and open a draft PR

## Usage

```
/next-chunk
```

Optional: `/next-chunk F3` to implement a specific item by ID (e.g., F3 = Graduated position sizing).

## Notes

- Always branches from `origin/main` to avoid conflicts with in-flight PRs
- Creates a draft PR so you can review before merging
- If the item is large, proposes a scoped subset and asks before implementing
