## What does this change?

<!-- A short description, and the issue it closes (e.g. "Closes #12"). -->

## Why?

<!-- The problem this solves. If it changes behaviour, say what used to happen. -->

## Checklist

- [ ] The quality gate passes locally (`ruff` → `ty` → `pytest` with the coverage floor).
- [ ] Tests cover the change (a regression test if this is a bug fix).
- [ ] `ARCHITECTURE.md`, `README.md`, and `docs/` are updated if the change affects them.
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`.
- [ ] All HTTP goes through `http.make_session()`.
- [ ] No per-platform access recipes, credentials, or throttle parameters are added to the public
      core (those belong in a private plugin overlay).

## Notes for the reviewer

<!-- Anything you want a second pair of eyes on. -->
