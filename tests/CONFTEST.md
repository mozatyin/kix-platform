# tests/conftest.py — test-isolation contract

This document describes how the KiX test suite stays isolated across
~1,200 tests sharing one Redis DB and one in-process FastAPI app.
Read this **before** adding a new test that touches Redis, env vars,
or any other process-wide state — most of the historical flakiness in
this suite came from people not knowing the rules below.

## What the fixtures do

| Fixture | Scope | Autouse | Job |
|---|---|---|---|
| `redis_pool` | session | yes (via `_redis_session_clean`) | Spin up the Redis async pool once for the whole session. |
| `_redis_session_clean` | session | **yes** | `FLUSHDB` once at session start, and bootstrap the session event loop that sync tests in `test_audit_log_pg.py` silently depend on (see below). |
| `_strict_isolation` | function | **yes** (async tests only) | Drain pending fire-and-forget tasks from the previous test, then `FLUSHDB`. Default ON; opt out with `--allow-pollution`. |
| `_env_isolation` | function | **yes** | Snapshot `os.environ` before each test, restore it after. Backstop for tests that mutate env vars without `monkeypatch.setenv`. |
| `client` | function | no | Per-test `httpx.AsyncClient` bound to the ASGI app. **Also runs the drain + flush** as defense-in-depth. |
| `clean_redis` | function | no | Per-test Redis handle with `FLUSHDB` baked in. **Also runs the drain.** |

## CLI flags

```bash
# Default — strict isolation, flush before every async test.
pytest tests/

# Disable the autouse flush. Use ONLY for local debugging when you
# need to inspect state left behind by a previous test. CI must never
# pass this.
pytest tests/ --allow-pollution

# Explicit alias for the default behaviour. Useful in CI scripts that
# want to document "yes, we intend strict mode" without depending on
# the default.
pytest tests/ --strict-isolation
```

## Required cleanup discipline for new tests

1. **Always prefer `monkeypatch.setenv`** over direct `os.environ[…] = …`.
   The autouse `_env_isolation` will save you if you forget, but
   `monkeypatch.setenv` is self-documenting and survives stricter
   future enforcement (e.g. asserting the env was unchanged).

2. **Always use a unique brand_id / user_id / campaign_id per test.**
   Helpers like `_new_brand_id("prefix")` exist in
   `tests/test_e2e_alpha_flow.py` and follow the
   `prefix_<uuid.uuid4().hex[:10]>` pattern. Fixed ids like
   `"brand_test"` are *fine because Redis is flushed*, but the moment
   you write to a downstream system (Postgres, an external API) the
   collision will bite.

3. **Don't spawn fire-and-forget tasks without a way to drain them.**
   The autouse fixture waits up to 200 ms for pending tasks before
   each test. If your route spawns a task that runs longer, its late
   writes can land *after* the next test's `FLUSHDB` and look like
   pollution. Either:
   * `await` the task before returning from the test, or
   * make the task short enough to finish in <200 ms, or
   * gate the task behind an env var that's off in tests.

4. **Don't monkey-patch a global `app.*` module attribute without a
   try/finally restore.** A handful of fixtures swap
   `app.database.async_session_factory` for an in-memory aiosqlite
   engine. They all restore it on teardown — yours must too.

## Known pollution sources (and where they live)

| Source | Symptom | Fix shipped |
|---|---|---|
| `test_audit_log_pg::test_router_requires_admin_token` rewrites `KIX_ADMIN_TOKEN` to `"test-token-xyz"` via raw `os.environ[...] = ...`. | `test_email_templates` admin endpoints later return 403 because their fallback token is `"admin-dev-token"`. | `_env_isolation` autouse fixture in `conftest.py` + explicit save/restore added to the offending test. |
| Missing `landing/i18n/locales/<locale>/legal.json` for seven SEA + RTL locales. | `test_phase2_sea_locales` reports `missing namespaces: {'legal.json'}` standalone *and* in the full suite. | Stub `legal.json` files added per locale (mirror `en-SG` verbatim, matching the Wave-B English-echo convention) + each locale's `_translation_status.json` gained a `legal` namespace entry. |
| `test_e2e_alpha_flow::_stub_audit_factory` swaps `app.database.async_session_factory` for an in-memory aiosqlite engine per-test; the engine is disposed on teardown. | Late fire-and-forget audit writes from earlier-in-the-test routes hit a disposed engine, raising silently and breaking the next test that hit the same factory. | Drain pending background tasks before restoring the factory; same drain pattern lives in the autouse `_strict_isolation` fixture. |
| Multiple routes write to Redis via fire-and-forget (`asyncio.create_task(...)`) — including audit-log mirroring and attribution scoring. | Tests that pass alone but fail intermittently in the suite with stale state. | `_drain_background_tasks()` (sleep-twice + bounded `asyncio.wait`) called at the start of every async test setup. |

## Why `_redis_session_clean` is autouse

The `tests/test_audit_log_pg.py` file mixes async tests (handled by
`pytest-asyncio` auto-mode) with **sync** tests that call:

```python
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
```

On Python 3.12+, `asyncio.get_event_loop()` raises `RuntimeError` when
no loop is currently registered for the main thread. The
session-scoped autouse `_redis_session_clean` fixture forces
`pytest-asyncio` to set up a session loop at collection time; sync
tests in that file then transparently inherit that loop.

If you remove the `autouse=True` from `_redis_session_clean`, expect
13 `RuntimeError: There is no current event loop in thread`
errors in `test_audit_log_pg` the next time the suite runs after
another file's async tests. (Yes, the real fix is to rewrite `_run`
to use `asyncio.run` — but that's an app-side change tracked
separately.)

## What this conftest cannot fix

Some flakes survive even with strict isolation:

* **Performance assertions** (`test_perf_partitioned_vs_full_scan`,
  `test_performance_100_concurrent_transfers`) — timing-sensitive on
  shared CI hardware. Mark them `@pytest.mark.slow` and skip in CI if
  they bite.
* **Real async races in app code** — e.g. a route that writes to
  Redis from a fire-and-forget task that outlives the 200 ms drain
  window. These need fixing in `app/`, not in conftest.
* **Pre-existing test bugs** (`test_voucher_expiration` hard-codes a
  past timestamp the validator rejects). The conftest can't fix
  logic bugs in the tests themselves.

If your test is in one of these buckets, please add a focused
follow-up rather than expanding conftest until it becomes
unmaintainable.
