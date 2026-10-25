import { homedir } from "node:os";
import { join, resolve } from "node:path";

// Single filesystem root per platform. Everything Archivist persists lives
// under it; nothing is ever written to the working directory, to the root of
// the home directory, or next to the sources.
//
//   Windows   %LOCALAPPDATA%\Archivist            cache: ...\Archivist\Cache
//   macOS     ~/Library/Application Support/Archivist   cache: ~/Library/Caches/Archivist
//   Linux     $XDG_DATA_HOME/archivist            cache: $XDG_CACHE_HOME/archivist
//             (fallback ~/.local/share/archivist)        (fallback ~/.cache/archivist)
//
// ARCHIVIST_HOME is the ONLY override: it relocates the whole tree (cache
// included) so a single variable is enough to sandbox or relocate everything.
export const TEMP_PREFIX = "archivist-";

// A shell does not expand `~` inside an env var, and the Python side expands it
// too: without this, `ARCHIVIST_HOME=~/arc` would create a literal `~` directory
// in the working directory and put the two roots in different places.
function expandHome(value, home) {
  if (value === "~") return resolve(home);
  if (value.startsWith("~/") || value.startsWith("~\\")) {
    return resolve(join(home, value.slice(2)));
  }
  return resolve(value);
}

export function resolvePaths({ env = process.env, home = homedir(), platform = process.platform } = {}) {
  const override = (env.ARCHIVIST_HOME || "").trim();
  let data;
  let cache;

  if (override) {
    data = expandHome(override, home);
    cache = join(data, "cache");
  } else if (platform === "win32") {
    const localAppData = env.LOCALAPPDATA || join(home, "AppData", "Local");
    data = join(localAppData, "Archivist");
    cache = join(data, "Cache");
  } else if (platform === "darwin") {
    data = join(home, "Library", "Application Support", "Archivist");
    cache = join(home, "Library", "Caches", "Archivist");
  } else {
    const dataHome = env.XDG_DATA_HOME || join(home, ".local", "share");
    const cacheHome = env.XDG_CACHE_HOME || join(home, ".cache");
    data = join(dataHome, "archivist");
    cache = join(cacheHome, "archivist");
  }

  return {
    data,
    cache,
    // The companion prunes with a size cap, and on Windows `cache` is the same
    // directory the server-side app pins fastembed/tiktoken into. Own a
    // subdirectory so a 64 MB cap can never evict someone else's 130 MB model.
    companionCache: join(cache, "companion"),
    tmp: join(data, "tmp"),
    logs: join(data, "logs"),
    configFile: join(data, "companion.json"),
    logFile: join(data, "logs", "companion.log"),
    // Pre-1.1 location. Read as a fallback and migrated on the next write, so
    // existing installs keep working without manual intervention.
    legacyConfigFile: join(home, ".archivist-companion.json"),
  };
}

// Human-readable inventory used by `archivist paths` and the README.
export function describePaths(paths) {
  return [
    { label: "root", path: paths.data, note: "everything Archivist persists" },
    { label: "config", path: paths.configFile, note: "server URL + API key (chmod 600)" },
    { label: "cache", path: paths.companionCache, note: "pruned by TTL and size cap" },
    { label: "temp", path: paths.tmp, note: `${TEMP_PREFIX}* dirs, garbage-collected at startup` },
    { label: "logs", path: paths.logs, note: "only when ARCHIVIST_LOG_FILE=1; rotated" },
  ];
}
