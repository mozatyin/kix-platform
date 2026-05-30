# Branch Protection Requirements

These settings should be enabled on GitHub for `main` (and any release
branches). They are documented here so that ops can reproduce the config and
audit drift.

## `main` branch protection rules

GitHub Settings -> Branches -> Add rule -> Branch name pattern: `main`.

### Pull request requirements
- [x] Require a pull request before merging
- [x] Require approvals: **2**
- [x] Dismiss stale pull request approvals when new commits are pushed
- [x] Require review from Code Owners (uses `.github/CODEOWNERS`)
- [x] Require approval of the most recent reviewable push

### Status check requirements
- [x] Require status checks to pass before merging
- [x] Require branches to be up to date before merging
- Required checks (from `ci.yml`):
  - `Lint (ruff + black + mypy)`
  - `Test (pytest + coverage)`
  - `Security (bandit + safety + trivy)`
  - `Secrets (gitleaks + trufflehog)`
  - `i18n Lint`
  - `CI Summary`
  - `Bible drift check` — **advisory only until D2 lands**, then required

### Commit / push restrictions
- [x] Require signed commits
- [x] Require linear history
- [x] Require conversation resolution before merging
- [x] Restrict who can push to matching branches
  - Only: `kix-platform/maintainers`, `kix-platform/devops` (placeholder
    teams — replace with real ones)
- [x] Block force pushes
- [x] Block deletions

### Merge strategy
- [x] Allow squash merging (default)
- [ ] Allow merge commits (disabled)
- [ ] Allow rebase merging (disabled)
- [x] Automatically delete head branches after merge

## Tag protection (`v*.*.*`)

GitHub Settings -> Tags -> New rule -> Pattern: `v*.*.*`.

- [x] Restrict who can create matching tags
  - Only: `kix-platform/release-managers` (placeholder team)
- This gates `deploy-production.yml` which triggers on tag push.

## Environments

GitHub Settings -> Environments.

### `staging`
- Deployment branches: `main` only
- No required reviewers (auto-deploys)
- Secrets: `KUBECONFIG_STAGING`, `SLACK_WEBHOOK_URL`

### `production-approval`
- Required reviewers: **2** from `kix-platform/release-managers`
- Wait timer: 0 minutes
- Deployment branches: tags matching `v*.*.*`

### `production`
- Required reviewers: at least 1 from on-call rotation
- Deployment branches: tags only
- Secrets: `KUBECONFIG_PROD`, `PAGERDUTY_TOKEN`, `DATADOG_API_KEY`

## Auditing

Run `gh api repos/:owner/:repo/branches/main/protection` periodically to
diff against this document. Drift = security incident.
