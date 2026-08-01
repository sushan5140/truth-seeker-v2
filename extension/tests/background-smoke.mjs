let messageListener;

globalThis.chrome = {
  runtime: {
    getManifest: () => ({ version: "1.5.0" }),
    onInstalled: { addListener() {} },
    onMessage: { addListener(listener) { messageListener = listener; } },
  },
  storage: { local: { get: async () => ({}), set: async () => {} } },
  tabs: { query: async () => [], reload: async () => {} },
};

await import("../background.js");
if (typeof messageListener !== "function") throw new Error("Background listener was not registered.");

function send(message, timeout = 12_000) {
  return new Promise((resolve, reject) => {
    const open = messageListener(message, {}, resolve);
    if (!open) reject(new Error("Worker closed the response channel."));
    setTimeout(() => reject(new Error("Worker response timed out.")), timeout);
  });
}

const diagnostic = await send({ type: "RUN_BACKEND_SELF_TEST" }, 25_000);
if (!diagnostic.ok || !diagnostic.health127.ok || !diagnostic.healthLocalhost.ok || !diagnostic.toneEndpoint.ok) {
  throw new Error(`Background self-test failed: ${JSON.stringify(diagnostic)}`);
}
const tone = await send({ type: "CHECK_TONE", text: "This idea is stupid and completely useless.", sensitivity: "balanced" });
if (!tone.ok || !tone.data?.provider) throw new Error(`Tone request failed: ${JSON.stringify(tone)}`);
console.log(JSON.stringify({ ok: true, diagnostic, intervention: tone.data.intervention_needed }));
