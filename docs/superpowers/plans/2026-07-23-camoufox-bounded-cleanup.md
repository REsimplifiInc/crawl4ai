# Camoufox Bounded Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a hung Camoufox context or backend close from indefinitely blocking Crawl4AI callers.

**Architecture:** Keep Camoufox lifecycle behavior inside Crawl4AI's runtime adapter. `BrowserManager` bounds Camoufox-only cleanup operations and continues best-effort teardown, while Camoufox 0.5.4 supplies corrected Playwright/Xvfb finalizers.

**Tech Stack:** Python 3.12, asyncio, Playwright, Camoufox, pytest, pytest-asyncio

---

### Task 1: Prove hung Camoufox cleanup blocks the manager

**Files:**
- Modify: `tests/unit/test_camoufox_runtime.py`

- [ ] **Step 1: Add a fake context and backend that never finish closing**

Add async fakes that set `started` and `cancelled` flags around an
`asyncio.Event().wait()` call.

- [ ] **Step 2: Add manager-level cleanup tests**

Add tests asserting a hung context does not prevent the backend exit and a hung
backend exit does not prevent `BrowserManager.close()` from returning and
clearing references.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
uv run --python 3.12 --extra camoufox --with pytest --with pytest-asyncio \
  pytest tests/unit/test_camoufox_runtime.py -q
```

Expected: the new tests fail by exceeding their test-level timeout because
current cleanup awaits the hung operation indefinitely.

### Task 2: Bound Camoufox cleanup inside Crawl4AI

**Files:**
- Modify: `crawl4ai/browser_manager.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the Camoufox cleanup bound**

Add a Camoufox-only helper that awaits a cleanup coroutine with a fixed timeout,
logs a warning on timeout, and returns so later teardown can continue.

- [ ] **Step 2: Apply it to context and backend close**

Use the helper in the Camoufox branch of `BrowserManager.close()`. Clear browser,
context, and backend references in `finally` blocks.

- [ ] **Step 3: Raise the Camoufox dependency floor**

Set both extras to:

```toml
"camoufox[geoip]>=0.5.4,<0.6"
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run --python 3.12 --extra camoufox --with pytest --with pytest-asyncio \
  pytest tests/unit/test_camoufox_runtime.py -q
```

Expected: all Camoufox runtime tests pass.

- [ ] **Step 5: Run formatting and the broader unit suite**

Run the repository's available formatter/linter, followed by:

```bash
uv run --python 3.12 --extra camoufox --with pytest --with pytest-asyncio \
  pytest tests/unit -q
```

Expected: no failures introduced by the cleanup change.

### Task 3: Publish and consume the fork fix

**Files:**
- Modify in `data-engine`: `pyproject.toml`
- Modify in `data-engine`: `uv.lock`
- Revert in `data-engine`: `apps/scrape-worker/src/scrape_worker/crawl4ai_engine.py`
- Revert in `data-engine`: `libs/core/src/data_engine_core/settings.py`
- Revert in `data-engine`: `tests/test_crawl4ai_runtime.py`

- [ ] **Step 1: Commit, push, and open the Crawl4AI PR**

Push `fix/camoufox-bounded-cleanup`, open a PR against
`REsimplifiInc/crawl4ai:main`, and wait for required checks.

- [ ] **Step 2: Merge the Crawl4AI PR**

Merge only after its remote checks pass, then capture the merge commit SHA.

- [ ] **Step 3: Replace the data-engine workaround with the fork pin**

Remove `_CleanupGuardedCrawler`, `SCRAPE_BROWSER_CLEANUP_TIMEOUT_SECONDS`,
forced `os._exit(1)`, and their tests. Pin Crawl4AI to the merged commit and
regenerate `uv.lock`.

- [ ] **Step 4: Verify data-engine**

Run:

```bash
uv run pytest tests/test_crawl4ai_runtime.py -q
just ci
```

Expected: focused tests and the full branch verification gate pass.

- [ ] **Step 5: Push and verify PR #444**

Push the existing branch, confirm GitHub sees only the fork pin and coordinated
version changes, and wait until required checks report success.
