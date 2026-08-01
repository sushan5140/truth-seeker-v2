let messageListener;
let claimFetches = 0;
let openedUrl = null;
let lastClaimBody = null;

globalThis.chrome = {
  runtime: {
    getManifest: () => ({ version: "2.0.0" }),
    onInstalled: { addListener() {} },
    onMessage: { addListener(listener) { messageListener = listener; } },
  },
  storage: { local: { get: async () => ({}), set: async () => {} } },
  tabs: { query: async () => [], reload: async () => {}, create: async ({ url }) => { openedUrl = url; } },
};

const responseData = {
  verdict: "supported",
  claim: "The city has twelve public parks.",
  confidence: "high",
  summary: "The controlled evidence supports the claim.",
  limitations: ["Controlled test sources only."],
  evidence: [{ title: "Official report", url: "https://example.org/report", publisher: "Example", published_at: null, retrieved_at: new Date().toISOString(), stance: "supports", excerpt: "The city has twelve public parks.", source_category: "general", ranking_score: 50 }],
  provider: { search: "mock", extractor: "mock", verifier: "gemini" },
  cached: false,
  checked_at: new Date().toISOString(),
  certainty_notice: "Verdicts reflect the evidence found from the sources searched, not absolute certainty.",
};

globalThis.fetch = async (url, options = {}) => {
  if (url.endsWith("/api/check-claim")) {
    claimFetches += 1;
    lastClaimBody = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () => responseData };
  }
  if (url.endsWith("/api/check-tone")) return { ok: true, status: 200, json: async () => ({ intervention_needed: false, confidence: "low", suggested_rewrite: "neutral text", explanation: "neutral", provider: "local_fallback" }) };
  if (url.endsWith("/api/truth/status")) return { ok: true, status: 200, json: async () => ({ backend_status: "running", search_provider_configured: "development_mock", extraction_provider_configured: "development_mock", cache_available: true }) };
  return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
};

await import("../background.js");

function send(message) {
  return new Promise((resolve, reject) => {
    const open = messageListener(message, {}, resolve);
    if (!open && message.type !== "OPEN_SOURCE") reject(new Error("Response channel closed."));
    setTimeout(() => reject(new Error(`Timed out: ${message.type}`)), 2_000);
  });
}

const request = { type: "CHECK_CLAIM", requestId: "one", text: "The city has twelve public parks.", sourceUrl: "https://example.org/post", pageTitle: "Post", language: "en" };
const first = await send(request);
const duplicate = await send({ ...request, requestId: "two" });
const bypass = await send({ ...request, requestId: "three", bypassCache: true });
if (!first.ok || !duplicate.ok || !bypass.ok) throw new Error("Claim routing failed.");
if (claimFetches !== 2) throw new Error(`Duplicate suppression failed: ${claimFetches}`);
if (lastClaimBody.text !== request.text || lastClaimBody.source_url !== request.sourceUrl || lastClaimBody.bypass_cache !== true) throw new Error("Claim request body mismatch.");

await send({ type: "OPEN_SOURCE", url: "https://example.org/report" });
if (openedUrl !== "https://example.org/report") throw new Error("Safe source was not opened.");
const unsafe = await send({ type: "OPEN_SOURCE", url: "http://127.0.0.1/private" });
if (unsafe.ok || unsafe.code !== "UNSAFE_SOURCE_URL") throw new Error("Unsafe source URL was not blocked.");

console.log(JSON.stringify({ ok: true, claimFetches, duplicateSuppressed: true, safeSourceOpened: true, unsafeSourceBlocked: true }));
