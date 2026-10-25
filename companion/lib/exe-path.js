const CWD_LOOKUP_GUARD = "NoDefaultCurrentDirectoryInExePath";

/**
 * Returns a copy of `env` with `NoDefaultCurrentDirectoryInExePath=1`, so a
 * Windows child (e.g. `cmd.exe` resolving `claude.cmd`) never resolves an
 * executable from its current directory. Harmless on other platforms.
 *
 * @param {NodeJS.ProcessEnv} [env] Base environment, `process.env` by default.
 * @returns {NodeJS.ProcessEnv}
 */
export function pathOnlyEnv(env = process.env) {
  return { ...env, [CWD_LOOKUP_GUARD]: "1" };
}

/**
 * Runs `spawnCall` with `NoDefaultCurrentDirectoryInExePath=1` set in this
 * process's own environment, then restores the previous value. On Windows,
 * libuv resolves the executable of `spawn`/`spawnSync`/`execFileSync` against
 * the parent's environment and searches the current directory before PATH
 * unless the variable is set there; setting it only in the child's `env`
 * does not stop that lookup. The executable is resolved synchronously inside
 * the spawn call, so the guard only needs to cover the call itself.
 *
 * @template T
 * @param {() => T} spawnCall Performs the spawn and returns its result.
 * @param {NodeJS.ProcessEnv} [processEnv] Environment to guard, `process.env` by default.
 * @returns {T}
 */
export function resolveFromPathOnly(spawnCall, processEnv = process.env) {
  const previous = processEnv[CWD_LOOKUP_GUARD];
  processEnv[CWD_LOOKUP_GUARD] = "1";
  try {
    return spawnCall();
  } finally {
    if (previous === undefined) delete processEnv[CWD_LOOKUP_GUARD];
    else processEnv[CWD_LOOKUP_GUARD] = previous;
  }
}
