# i18n String Extraction Tooling

Production tooling for the **Phase 2** i18n string-extraction wave
described in `/Users/mozat/a-docs/i18n-trinity-strategy.md` § 4.4.
Walks `app/**/*.py` (Python AST) and `landing/**/*.{html,js}` (DOM /
regex), finds user-facing strings, and emits a CSV that Wave-2 agents
hand to translators.

This is **tooling**. By default it does **not** rewrite source code.

---

## Quick start

```bash
# Single-file dry-run, heuristic classifier only
.venv/bin/python -m scripts.i18n_extract \
    --target app/routers/tutorials.py \
    --output i18n_extracted_tutorials.csv

# Full Python sweep with LLM classifier (quota-guarded, costs ~$3)
.venv/bin/python -m scripts.i18n_extract \
    --target app \
    --output i18n_extracted_python.csv \
    --llm

# Landing HTML + JS sweep
.venv/bin/python -m scripts.i18n_extract_html \
    --target landing \
    --output i18n_extracted_landing.csv \
    --llm

# Run the unit tests (10 tests, no Redis, no LLM, no app boot needed)
.venv/bin/python -m pytest tests/test_i18n_extract.py -v
```

---

## CLI flags

Both scripts share the same surface:

| Flag          | Default                            | Meaning                                                                                                  |
| ------------- | ---------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `--target`    | `app` / `landing`                  | File or directory to walk                                                                                |
| `--output`    | `i18n_extracted_report.csv`        | CSV output path (relative paths are resolved from the repo root)                                         |
| `--llm`       | off                                | Use the Claude Haiku classifier for `is_user_facing` + `proposed_key`. Quota-guarded via `llm_quota_monitor`. |
| `--apply`     | off (and a stub — Phase 2)         | Reserved flag for the future source-rewriter. Currently emits a warning and runs the normal dry-run.    |
| `--verbose`   | off                                | DEBUG logging                                                                                            |

`--apply` will be implemented in the Wave-2 PR that lands the actual
`t("key", vars)` rewrite. Keeping it as a stub lets us version the CSV
artifacts before any code change.

---

## CSV schema

```
file,line,original,category,proposed_key,is_user_facing
```

| Column            | Meaning                                                                                                  |
| ----------------- | -------------------------------------------------------------------------------------------------------- |
| `file`            | Repo-relative source path                                                                                |
| `line`            | 1-indexed line number where the string starts                                                            |
| `original`        | Verbatim string. F-string placeholders become ICU-style `{name}`.                                       |
| `category`        | One of `error`, `notification`, `button`, `label`, `description`, `comment`, `log`, `other`             |
| `proposed_key`    | snake.case dotted key, e.g. `wallet.insufficient_balance`. Collisions are auto-disambiguated with `_2…`. |
| `is_user_facing`  | `yes` (extract), `no` (skip — internal/log/SQL), or `comment` (developer comment text)                  |

Wave-2 agents should filter `is_user_facing == "yes"` and treat the
remaining rows as the **extraction work-list**.

---

## What gets skipped automatically

* Python module/class/function docstrings.
* `logger.*("template", ...)` first-argument format templates.
* Dict keys (almost always identifiers).
* SQL, regex literals, snake_case / SCREAMING_SNAKE identifiers, URLs.
* Lines annotated with `# noqa: i18n` or `# i18n-ignore` (either on the
  same line or on the line above).
* HTML `<script>`, `<style>`, `<noscript>`, `<code>`, `<pre>`,
  `<template>` blocks.
* JS comments (`//` and `/* */`).
* `vendor/`, `node_modules/`, `assets/gdpr-exports/`, `__pycache__/`,
  `migrations/`, `alembic/`, `.venv/`.

---

## How the classifier works

1. **Heuristic-only mode** (`--llm` off): pure-Python rules in
   `scripts/i18n_prompts.py::heuristic_classify`. Fast, free, no
   network. Good enough for ~80% of CJK-only strings.
2. **LLM mode** (`--llm` on): Claude Haiku 4.5 with the 7-shot prompt in
   `scripts/i18n_prompts.py::SYSTEM_PROMPT` + `FEW_SHOT`. JSON-only
   output. Falls back to the heuristic on:
   * missing `ANTHROPIC_API_KEY`,
   * HTTP non-200 / parse failure,
   * `llm_quota_monitor` indicating the global pause flag is set
     (95%-usage circuit breaker — see `MEMORY.md → LLM Quota Guard`).

Every LLM call awaits `scripts.llm_quota_monitor.wait_if_paused()` with
a 1-hour ceiling. If quota is paused for longer, the script raises and
exits non-zero — re-run later.

Total LLM cost for a full sweep (~3,000 strings × Haiku):

```
3000 input ≈ 350 tok ea ≈ 1.05M tok  → ≈ $1
3000 output ≈ 60 tok ea ≈ 180k tok    → ≈ $0.90
                                       ─────
                                       ~$2 total
```

---

## How translators consume the CSV

1. Sort by `proposed_key` so semantically-related strings batch.
2. Filter `is_user_facing == "yes"`.
3. Append translations into the relevant Fluent (`.ftl`) catalog under
   `app/i18n/catalogs/`:
   * `en-SG.ftl`, `en-US.ftl`, `zh-Hans-CN.ftl`, `zh-Hans-SG.ftl`.
4. PR the catalog change. Pseudo-loc CI gate (Phase-2 work) will block
   merges with missing keys.

---

## Files in this toolchain

| File                                | Purpose                                                  |
| ----------------------------------- | -------------------------------------------------------- |
| `scripts/i18n_extract.py`           | Python AST walker + CLI                                  |
| `scripts/i18n_extract_html.py`      | HTML / JS extractor + CLI                                |
| `scripts/i18n_prompts.py`           | LLM prompt template, classifier, heuristic fallback     |
| `tests/test_i18n_extract.py`        | 10 unit tests (fixture-based, no Redis, no LLM)         |
| `i18n_extracted_tutorials_sample.csv` | Reference output from `app/routers/tutorials.py`       |

---

## Known limitations (Phase 2 follow-ups)

* `--apply` source rewriter is a stub. Designing the rewriter requires
  agreement on the `t("key", vars)` API surface (FastAPI dependency vs.
  contextvar) — tracked in the i18n-trinity strategy doc § 4.2.
* HTML extractor uses stdlib `html.parser`; for very malformed pages it
  may miss text inside broken tags. The five `landing/*.html` files are
  well-formed today, so this is acceptable for v1.
* JS extractor is regex-based, so it does not understand JSX. The KiX
  landing pages use vanilla JS, so this is fine; if React is added
  later, swap in `tree-sitter-javascript`.
* The heuristic transliteration table covers ~30 CJK words. Outside
  this list, heuristic-mode keys fall back to hash-suffix
  (`subsystem.s_a1b2c3`). LLM mode is recommended for the production
  sweep.
