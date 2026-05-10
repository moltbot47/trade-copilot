# Contributing to Trade Copilot

> **First time cloning?** Run `pre-commit install` once after cloning. See [Pre-commit hooks](#pre-commit-hooks) below.

This document covers local development hygiene. For architecture and deployment
see [`ARCHITECTURE.md`](ARCHITECTURE.md), [`SETUP.md`](SETUP.md), and
[`STAGING.md`](STAGING.md).

---

## Pre-commit hooks

We use [pre-commit](https://pre-commit.com/) to enforce lint, format, type
checks, and a fast smoke test on every commit. Config lives in
[`.pre-commit-config.yaml`](../.pre-commit-config.yaml) at the repo root.

### Install (one-time, after cloning)

```bash
pip install pre-commit
pre-commit install
```

That registers a Git hook at `.git/hooks/pre-commit`. From now on, every
`git commit` automatically runs the hooks against staged files.

The first commit after install will be slow — pre-commit downloads and caches
each hook's environment. Subsequent commits are fast.

### What runs on commit

| Hook              | What it does                                            | Scope                          |
| ----------------- | ------------------------------------------------------- | ------------------------------ |
| `trailing-whitespace` | Strip trailing whitespace                           | all files                      |
| `end-of-file-fixer`   | Ensure file ends with a single newline              | all files                      |
| `check-yaml`          | Validate YAML syntax                                | all `*.yaml` / `*.yml`         |
| `check-toml`          | Validate TOML syntax                                | all `*.toml`                   |
| `check-merge-conflict`| Block commits with unresolved conflict markers      | all files                      |
| `check-added-large-files` | Block files > 1 MB                              | all files                      |
| `ruff` (lint)         | Lint + autofix                                      | staged `backend/**/*.py`       |
| `ruff-format`         | Format                                              | staged `backend/**/*.py`       |
| `mypy-backend`        | Type-check `backend/app/` via backend venv          | triggered by staged backend py |
| `pytest-smoke`        | Run `test_health.py` + `test_jwt.py` (<5s)          | triggered by staged backend py |

The `mypy-backend` and `pytest-smoke` hooks shell into `backend/venv/` to pick
up the project's installed deps and stubs — make sure the venv exists and is
up to date:

```bash
cd backend
python3.11 -m venv venv
source venv/bin/activate
pip install -e '.[dev]'
```

### Run manually on all files

Useful after editing the hook config, or to clean up a stale branch:

```bash
pre-commit run --all-files
```

To run a single hook:

```bash
pre-commit run ruff --all-files
pre-commit run mypy-backend --all-files
```

### Skipping hooks (emergencies only)

```bash
git commit --no-verify -m "wip: ..."
```

**When this is acceptable:**

- You're committing a WIP branch you haven't pushed yet, and a hook is blocking
  a hot-fix you'll clean up in a follow-up commit.
- A hook is failing for reasons unrelated to your change (e.g., flaky tooling)
  and you've filed a ticket to fix the hook itself.

**When this is NOT acceptable:**

- Pushing to `main` or any branch with an open PR.
- Skipping `mypy-backend` or `pytest-smoke` to "save time" — those failures
  represent real regressions and CI will catch them anyway, just slower.

CI re-runs the same checks (and more) on every push, so `--no-verify` only
defers the pain, it doesn't avoid it.

### Updating hook versions

```bash
pre-commit autoupdate
```

Then commit the updated `rev:` pins. Run `pre-commit run --all-files` to make
sure nothing breaks before pushing.

### Python version

Hooks pin Python 3.11 (`default_language_version: python: python3.11` in the
config). Matches `requires-python = ">=3.11"` in `backend/pyproject.toml`. If
you're on 3.12+ locally that's fine — the hooks just need 3.11 available on
PATH (e.g., via `pyenv` or system Python).

---

## Style & conventions

- **Python:** ruff handles both lint and format. No black, no isort — ruff
  does it all. Config in `backend/pyproject.toml`.
- **Types:** mypy is enforced on `backend/app/`. New code must be typed.
- **Tests:** every new feature ships with at least one test under
  `backend/tests/`. Smoke-critical paths (health, auth) go in
  `test_health.py` / `test_jwt.py` so they run in the pre-commit hook.
- **Commits:** follow the Conventional Commits style already used in
  `git log` — `feat:`, `fix:`, `ci:`, `docs:`, `chore:`, etc.

---

## Reporting security issues

See [`../SECURITY.md`](../SECURITY.md) (if present) or email the maintainer
directly. Don't open a public GitHub issue for security reports.
