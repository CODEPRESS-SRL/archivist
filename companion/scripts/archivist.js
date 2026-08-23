#!/usr/bin/env node
import { run } from "../lib/cli.js";

// A broken pipe (`archivist paths | head`) or any unexpected error must exit
// with a status, not an unhandled-rejection stack trace.
run({ argv: process.argv.slice(2) }).then(
  (code) => process.exit(code || 0),
  (err) => {
    if (err && err.code !== "EPIPE") process.stderr.write(`archivist: ${err.message || err}
`);
    process.exit(1);
  }
);
