let messageListener;
let quickAskFetches = 0;
let lastQuickAskUrl = null;
let lastQuickAskBody = null;

globalThis.chrome = {
  runtime: {
    getManifest: () => ({ version: "2.0.0" }),
    onInstalled: { addListener() {} },
    onMessage: { addListener(listener) { messageListener = listener; } },
  },
  storage: { local: { get: async () => ({}), set: async () => {} } },
  tabs: { query: async () => [], reload: async () => {}, create: async () => {} },
};

const availableResponse = {
  available: true,
  answer: "No, the moon is made of rock and dust, not cheese.",
  sources: ["https://example.org/moon-composition"],
  model: "google/gemma-4-26b-a4b-it:free",
};

const unavailableResponse = {
  available: false,
  answer: "Quick Ask isn't available right now.",
  sources: [],
  model: null,
};

let nextResponse = availableResponse;

globalThis.fetch = async (url, options = {}) => {
  if (url.endsWith("/api/v2/quick-ask")) {
    quickAskFetches += 1;
    lastQuickAskUrl = url;
    lastQuickAskBody = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () => nextResponse };
  }
  return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
};

await import("../background.js");

function send(message) {
  return new Promise((resolve, reject) => {
    const open = messageListener(message, {}, resolve);
    if (!open) reject(new Error("Response channel closed."));
    setTimeout(() => reject(new Error(`Timed out: ${message.type}`)), 2_000);
  });
}

// Too-short text never reaches fetch.
const tooShort = await send({ type: "QUICK_ASK", text: "hi" });
if (tooShort.ok || tooShort.code !== "INVALID_QUICK_ASK") throw new Error("Short text was not rejected.");
if (quickAskFetches !== 0) throw new Error("Short text incorrectly reached the backend.");

// A configured, available response routes through /api/v2/quick-ask with just {text} (no language field).
const available = await send({ type: "QUICK_ASK", text: "Is the moon made of cheese?" });
if (!available.ok) throw new Error("Quick Ask request failed.");
if (quickAskFetches !== 1) throw new Error(`Expected exactly one fetch, got ${quickAskFetches}.`);
if (!lastQuickAskUrl.endsWith("/api/v2/quick-ask")) throw new Error("Quick Ask did not call /api/v2/quick-ask.");
if (lastQuickAskBody.text !== "Is the moon made of cheese?" || "language" in lastQuickAskBody) throw new Error("Quick Ask request body mismatch.");
if (available.data.available !== true || available.data.sources.length !== 1) throw new Error("Available response shape mismatch.");

// An unavailable (unconfigured/failed) response passes through as ok:true with available:false --
// this is a normal, well-formed response, not a transport failure.
nextResponse = unavailableResponse;
const unavailable = await send({ type: "QUICK_ASK", text: "Is the moon made of cheese?" });
if (!unavailable.ok) throw new Error("Unavailable Quick Ask response should still be ok:true at the routing layer.");
if (unavailable.data.available !== false || unavailable.data.answer !== "Quick Ask isn't available right now.") {
  throw new Error("Unavailable response shape mismatch.");
}

// A malformed backend response (missing required fields) is rejected, not passed through.
nextResponse = { answer: "no available flag or sources array" };
const malformed = await send({ type: "QUICK_ASK", text: "Is the moon made of cheese?" });
if (malformed.ok || malformed.code !== "INVALID_QUICK_ASK_RESPONSE") throw new Error("Malformed Quick Ask response was not rejected.");

console.log(JSON.stringify({ ok: true, quickAskFetches, shortTextRejected: true, availableRouted: true, unavailablePassedThrough: true, malformedResponseRejected: true }));
