# i18n CI integration — spec (not yet wired)

This document specifies *how* to plug the i18n QA tooling
(`scripts/pseudoloc.py`, `scripts/i18n_lint.py`, `scripts/visual_qa.py`)
into GitHub Actions and pre-commit. **The workflows are not yet
committed** — they are described here so a platform engineer can wire
them when the translation pipeline goes live.

---

## 1. PR check — i18n lint (P0 only, blocking)

Run on every PR. Fails the build if any P0 rule fires (R-001, R-002,
R-007). P1/P2 rules are reported but non-blocking.

`.github/workflows/i18n-lint.yml`:

```yaml
name: i18n lint
on:
  pull_request:
    paths:
      - "app/**"
      - "landing/**"
      - "scripts/i18n_*"
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: lint
        run: |
          python -m scripts.i18n_lint \
            --severity p0 \
            --github-annotations \
            --junit reports/i18n-lint.xml
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: i18n-lint-junit
          path: reports/i18n-lint.xml
```

The `--github-annotations` flag emits `::error file=…` lines so the
GitHub UI surfaces each finding inline on the diff.

### Branch protection

In `Settings → Branches → main`, add `i18n lint / lint` to "required
status checks". Merges to `main` are then blocked on P0 violations.

---

## 2. Nightly — pseudoloc + visual QA

Run on `schedule:` and on push to `main`. Produces a screenshot report
that is uploaded as a workflow artifact.

`.github/workflows/i18n-visual-qa.yml`:

```yaml
name: i18n visual QA
on:
  schedule: [{ cron: "0 18 * * *" }]   # 02:00 SGT
  workflow_dispatch:
jobs:
  visual:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install playwright && playwright install chromium
      - name: regenerate pseudo-locales
        run: |
          python -m scripts.pseudoloc \
            --batch app/i18n/catalogs --source en-SG --all-modes
          python -m scripts.pseudoloc \
            --batch landing/i18n/locales --source en-SG --all-modes --format json
      - name: visual QA
        run: |
          python -m scripts.visual_qa \
            --locales en-SG,zh-Hans-SG,xx-AC,xx-LO \
            --output qa_screenshots \
            --json qa_screenshots/findings.json
      - uses: actions/upload-artifact@v4
        with:
          name: qa-screenshots
          path: qa_screenshots/
```

---

## 3. PR comment — screenshot diff

A follow-up workflow that posts a sticky comment with the diff report:

```yaml
- uses: peter-evans/create-or-update-comment@v4
  with:
    issue-number: ${{ github.event.pull_request.number }}
    body-file: qa_screenshots/report.md
```

Generated `report.md` should embed:

* Side-by-side thumbnails for each (page, locale).
* A bullet list of `overflow_lo` / `size_jump` findings.
* Link to full artifact ZIP for engineers to inspect locally.

---

## 4. Branch protection summary

| Check | Source | Blocks merge? |
|---|---|---|
| `i18n lint / lint` | `i18n-lint.yml` | yes (P0 only) |
| `i18n visual QA` | `i18n-visual-qa.yml` | no (nightly artifact) |

---

## 5. Local invocation cheatsheet

```bash
# Lint everything
python -m scripts.i18n_lint --severity all

# Lint only staged files (mimics pre-commit)
python -m scripts.i18n_lint --files $(git diff --cached --name-only)

# Regenerate pseudo-locales
python -m scripts.pseudoloc --batch app/i18n/catalogs --source en-SG --all-modes

# Take screenshots (requires `pip install playwright && playwright install chromium`)
python -m scripts.visual_qa --base-url http://localhost:8000 \
                            --output qa_screenshots
open qa_screenshots/report.html
```
