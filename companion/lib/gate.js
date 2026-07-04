// C1 heuristic gate: decide whether a session is worth an auto-capture run.
// Cheap and pre-LLM — most trivial sessions cost nothing because they never
// reach the `claude -p` spawn.
export function shouldCapture({ transcript, minChars }) {
  if (transcript.charCount >= minChars) return true;
  if (transcript.fileEdits > 0 && transcript.charCount > 0) return true;
  return false;
}
