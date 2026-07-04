import { test } from "node:test";
import assert from "node:assert/strict";
import { finishSilently } from "../lib/finish.js";

test("finishSilently sets exitCode and schedules an unref'd watchdog that exits", () => {
  const prev = process.exitCode;
  let unrefCalled = false;
  let scheduled = null;
  const fakeTimer = {
    unref: () => {
      unrefCalled = true;
    },
  };
  const schedule = (cb, ms) => {
    scheduled = { cb, ms };
    return fakeTimer;
  };
  let exitedWith = null;
  const exit = (c) => {
    exitedWith = c;
  };

  finishSilently(0, { exit, schedule, graceMs: 1234 });

  assert.equal(process.exitCode, 0);
  assert.equal(scheduled.ms, 1234);
  assert.equal(unrefCalled, true, "watchdog must be unref'd so it never keeps the loop alive");

  // The watchdog is the hang fallback: firing it force-exits with the same code.
  scheduled.cb();
  assert.equal(exitedWith, 0);

  process.exitCode = prev;
});

test("finishSilently tolerates a timer without unref (never throws)", () => {
  const prev = process.exitCode;
  assert.doesNotThrow(() =>
    finishSilently(0, { exit: () => {}, schedule: () => ({}), graceMs: 1 }),
  );
  process.exitCode = prev;
});
