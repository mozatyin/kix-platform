# i18n pre-commit hook — spec (not yet wired)

Lightweight hook: runs `i18n_lint.py --severity p0` against staged files
only, so the local commit loop stays fast (< 1 s on typical PR-sized
diffs).

## `.pre-commit-config.yaml` snippet

Add to the repo root (or merge into an existing `.pre-commit-config.yaml`):

```yaml
repos:
  - repo: local
    hooks:
      - id: i18n-lint-p0
        name: "i18n lint (P0 only)"
        entry: python -m scripts.i18n_lint --severity p0 --quiet --files
        language: system
        pass_filenames: true
        types_or: [python, html]
        # Pre-commit invokes us once with the staged file list.
        # `--files` consumes them and lints only those paths.
```

## Install

```bash
pip install pre-commit
pre-commit install
```

## Behavior

* Runs on every `git commit`.
* Scans only staged `.py` and `.html` files.
* Fails the commit on any P0 violation (R-001, R-002, R-007).
* P1/P2 violations are deferred to CI — they do not block the commit
  so developers can iterate without local noise.

## Skipping

For genuine exceptions, prefer adding to `scripts/i18n_lint_ignore.txt`.
The escape hatch `git commit --no-verify` exists but should be flagged
in code review.

## Performance budget

* `i18n_lint.py --files <N>` is O(N) in source bytes; no LLM calls.
* Empirically < 0.5 s for typical 10-file PRs.
* If hook latency exceeds 2 s on a clean checkout, profile before
  expanding the scanned rules.
