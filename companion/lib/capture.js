import { isConfigured } from "./config.js";
import { shouldCapture } from "./gate.js";
import { PROTOCOL } from "./protocol.js";
import { runHeadless } from "./headless.js";

const MAX_TRANSCRIPT_CHARS = 12000;

export function buildCapturePrompt(slug, transcriptText, protocol) {
  const clipped = transcriptText.length > MAX_TRANSCRIPT_CHARS
    ? transcriptText.slice(-MAX_TRANSCRIPT_CHARS)
    : transcriptText;
  return `${protocol}

The project slug for this session is "${slug}".

Below is a transcript of a coding session. Decide whether anything hard-won and
team-relevant is worth saving to shared memory: a non-obvious decision, a fix and
its root cause, a convention, a gotcha. Save at most 3 concepts. If nothing
qualifies, exit without writing anything.

--- SESSION TRANSCRIPT ---
${clipped}
--- END TRANSCRIPT ---`;
}

// C1 orchestration. Pure/injectable: `headless` defaults to the real spawner.
export async function runCapture({ cfg, slug, transcript, headless = runHeadless }) {
  if (!isConfigured(cfg) || cfg.capture === false) return { skipped: "disabled" };
  if (!shouldCapture({ transcript, minChars: cfg.gateMinChars })) return { skipped: "gate" };
  const prompt = buildCapturePrompt(slug, transcript.text, PROTOCOL);
  await headless({
    cfg,
    prompt,
    allowedTools: "mcp__archivist",
    model: cfg.captureModel,
    maxTurns: cfg.captureMaxTurns,
    detached: true,
  });
  return { started: true };
}
