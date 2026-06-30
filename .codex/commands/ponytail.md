# Ponytail

Use Ponytail when the user asks for the simplest, laziest, shortest, or most minimal solution.

Repo-specific rules:

- Do not simplify away risk controls, exposure accounting, async correctness, or secret handling.
- Prefer fixing shared choke points in `core/` and `api/` instead of scattering guards across callers.
- Reuse existing stdlib and installed tooling before adding dependencies or helper layers.
- Leave one small verification step behind for non-trivial logic.

Typical use here:

- delete duplicate helpers
- remove speculative abstractions
- replace custom glue with stdlib
- keep async trading paths boring and explicit
