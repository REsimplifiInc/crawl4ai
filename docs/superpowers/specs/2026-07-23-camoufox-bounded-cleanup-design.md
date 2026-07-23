# Camoufox Bounded Cleanup Design

## Problem

The RESimplifi Crawl4AI Camoufox runtime delegates context and browser teardown
to Playwright and `AsyncCamoufox` without a bound. After a navigation failure,
one of those close operations can remain pending indefinitely. A caller such as
`data-engine` then cannot return to its Kafka poll loop or commit/retry the
message.

The production evidence establishes that the process remained connected to the
Playwright/Camoufox subprocess tree after `NS_ERROR_NET_RESET`. It does not
justify putting Camoufox-specific process termination in the caller.

## Ownership

The fix belongs in `REsimplifiInc/crawl4ai`, where the custom Camoufox adapter
was introduced. Callers should receive a crawler whose lifecycle is bounded
without knowing how Camoufox, Playwright, Firefox, or Xvfb are shut down.

## Design

- Require `camoufox[geoip]>=0.5.4,<0.6` for the `camoufox` and `all` extras.
  Version 0.5.4 ensures browser-close failures still run Playwright and virtual
  display finalizers.
- Add a focused timeout helper inside `BrowserManager` for Camoufox-owned
  cleanup awaitables.
- Apply the helper when closing Camoufox browser contexts and the Camoufox
  runtime backend.
- If a page or context close exceeds the bound, log a warning, request
  cancellation, and continue the broader teardown. Do not wait indefinitely
  for a cleanup task that ignores cancellation.
- Before runtime-backend close, capture the Playwright driver handle and the
  optional virtual display owned by that `AsyncCamoufox` context manager.
- If runtime-backend close times out, enumerate the driver's current
  Camoufox/Firefox descendants before signaling the owned process tree.
- If runtime-backend close exceeds the bound, terminate the captured process
  tree, allow a short grace period, kill survivors, wait for the driver, and
  verify that no owned descendants remain. Stop the virtual display and
  consume the expected Playwright transport error from forced shutdown.
- Fail cleanup explicitly if the owned driver handle is unavailable or any
  process survives escalation; never clear references and silently report a
  successful close in that state.
- Clear backend and manager references in `finally` paths so repeated close
  calls are safe after a timeout.
- Keep Playwright runtime behavior unchanged.

## Error Handling

Cleanup timeout is recovery behavior, not a successful close signal. Crawl4AI
logs the resource and timeout and continues from narrow resources toward the
runtime backend. A backend timeout escalates only against subprocesses owned by
that Camoufox context manager; Crawl4AI does not terminate the caller process
or inspect unrelated system processes. Once the owned process tree is reaped,
control returns to the caller so its existing retry/DLQ policy can continue.

## Testing

Unit tests use fake Camoufox contexts and context managers whose close methods
wait forever. Tests prove:

1. A hung Camoufox context close does not prevent backend teardown.
2. A hung Camoufox backend exit does not block `BrowserManager.close()`.
3. A close operation that ignores task cancellation still reaches process
   escalation.
4. Descendants that ignore graceful termination are killed and disappear
   before cleanup returns.
5. The virtual display is stopped and the expected Playwright transport error
   is consumed after forced shutdown.
6. Normal Camoufox close never invokes force termination.
7. Missing process ownership fails cleanup explicitly.
8. Manager and backend references are cleared after either timeout.
9. Existing dedicated and persistent Camoufox cleanup behavior remains green.

After the fork PR merges, `data-engine` will pin the merge commit, remove its
cleanup watchdog and forced process exit, regenerate `uv.lock`, run focused
scrape-worker tests, and run `just ci`.
