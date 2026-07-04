// Exit a fail-silent hook/CLI process cleanly.
//
// Forcing `process.exit(0)` while an undici keep-alive socket — left over from
// an MCP call over the streamable-HTTP client — is still tearing down trips a
// libuv assertion on Windows:
//   Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), src\win\async.c
// The process aborts with exit 127 AFTER its work is done, so a hook's stdout is
// produced but the non-zero exit makes the host treat the hook as failed (a
// SessionStart index would be discarded; a sweep cron would log a hard failure).
// Letting the event loop drain naturally exits 0 promptly (~200ms); an unref'd
// watchdog still guarantees termination if anything ever keeps the loop alive,
// so we never trade the crash for a hang. `exit`/`schedule` are injectable for
// tests.
export function finishSilently(
  code = 0,
  { exit = process.exit, schedule = setTimeout, graceMs = 3000 } = {},
) {
  process.exitCode = code;
  const timer = schedule(() => exit(code), graceMs);
  if (timer && typeof timer.unref === "function") timer.unref();
  return timer;
}
