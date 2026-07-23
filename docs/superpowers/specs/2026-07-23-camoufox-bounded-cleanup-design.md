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
- If a close operation exceeds the bound, log a warning, cancel that operation,
  and continue the remaining teardown rather than blocking forever.
- Clear backend and manager references in `finally` paths so repeated close
  calls are safe after a timeout.
- Keep Playwright runtime behavior unchanged.

## Error Handling

Cleanup timeout is recovery behavior, not a successful close signal. Crawl4AI
logs the resource and timeout, continues best-effort teardown, and returns
control to the caller. The caller can then complete its existing retry/DLQ
policy. Crawl4AI does not terminate the caller process.

## Testing

Unit tests use fake Camoufox contexts and context managers whose close methods
wait forever. Tests prove:

1. A hung Camoufox context close does not prevent backend teardown.
2. A hung Camoufox backend exit does not block `BrowserManager.close()`.
3. Manager and backend references are cleared after either timeout.
4. Existing dedicated and persistent Camoufox cleanup behavior remains green.

After the fork PR merges, `data-engine` will pin the merge commit, remove its
cleanup watchdog and forced process exit, regenerate `uv.lock`, run focused
scrape-worker tests, and run `just ci`.
