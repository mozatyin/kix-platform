# CI / CD Guide

This repo uses GitHub Actions for all CI/CD. Workflows live in
`.github/workflows/`. Developer-side tooling is in `.pre-commit-config.yaml`
and `Makefile`.

## Workflow map

| Workflow                             | Trigger                       | Purpose                                              |
| ------------------------------------ | ----------------------------- | ---------------------------------------------------- |
| `ci.yml`                             | PR, push to `main`            | lint / test / security / secrets / i18n / bible      |
| `deploy-staging.yml`                 | push to `main`                | build image, deploy staging, smoke, Slack notify     |
| `deploy-production.yml`              | tag `v*.*.*`                  | approval gate, canary 5 -> 25 -> 100, auto-rollback  |
| `scheduled.yml`                      | cron (nightly/weekly/monthly) | dep scans, full E2E, perf, license, compliance       |

## Required secrets

Configure these in GitHub repo settings -> Secrets and variables -> Actions.

| Secret                  | Used by                       | Notes                                  |
| ----------------------- | ----------------------------- | -------------------------------------- |
| `GITLEAKS_LICENSE`      | `ci.yml` secrets-scan         | org license (optional)                 |
| `SLACK_WEBHOOK_URL`     | `deploy-staging.yml` notify   | incoming webhook                       |
| `AWS_ROLE_ARN`          | deploy-*                      | OIDC role for cloud deploys            |
| `KUBECONFIG_STAGING`    | `deploy-staging.yml`          | base64 kubeconfig                      |
| `KUBECONFIG_PROD`       | `deploy-production.yml`       | base64 kubeconfig                      |
| `PAGERDUTY_TOKEN`       | rollback job                  | trigger on prod rollback               |
| `DATADOG_API_KEY`       | canary jobs                   | error-rate query                       |
| `COMPLIANCE_API_KEY`    | monthly compliance scan       | optional                               |

All secrets are referenced with `# TODO: configure ${SECRET}` markers inside
the workflow files for easy grepping.

## How CI works (PR flow)

1. Open PR -> `ci.yml` runs.
2. Jobs run in parallel where independent (`lint`, `security-scan`,
   `secrets-scan`, `i18n-lint`, `bible-check`). `test` depends on `lint` to
   fail fast on style issues.
3. `ci-summary` posts a status comment on the PR.
4. Merge to `main` triggers `deploy-staging.yml`.
5. Tag `vX.Y.Z` triggers `deploy-production.yml` (manual approval required).

## Adding a new check

1. Add the script under `scripts/` (or inline in the workflow if tiny).
2. Add a new job in `.github/workflows/ci.yml`. Keep it parallel; only add
   `needs:` if it genuinely depends on a prior job.
3. Add it to `needs:` of `ci-summary`.
4. If the check is enforceable locally, add it to `.pre-commit-config.yaml`
   and a `make` target.
5. If it should block merge, add it to **required status checks** in branch
   protection (see below).

## Debugging a failed CI

- Check the failing job's logs first; group lines with the `##[group]`
  collapsed sections.
- Reproduce locally with the matching `make` target (`make lint`,
  `make test`, `make security`).
- For flaky tests, rerun the single job from the Actions UI; if it flakes
  again, file a `[bug]` issue tagged `flaky-test`.
- For `secrets-scan` false positives, add the path to `.gitleaksignore`.
- For `i18n-lint`, run `python scripts/i18n_lint.py` locally and consult
  `scripts/i18n_README.md`.

## Branch protection (enable on GitHub)

See [`docs/branch-protection.md`](./branch-protection.md).

## Local mirror

```bash
make install        # one-time
make lint test      # fast pre-push gate
pre-commit run -a   # everything pre-commit covers
```
