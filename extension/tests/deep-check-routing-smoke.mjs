let messageListener;
let deepFetches = 0;
let lastDeepUrl = null;
let lastDeepBody = null;

globalThis.chrome = {
  runtime: {
    getManifest: () => ({ version: "2.0.0" }),
    onInstalled: { addListener() {} },
    onMessage: { addListener(listener) { messageListener = listener; } },
  },
  storage: { local: { get: async () => ({}), set: async () => {} } },
  tabs: { query: async () => [], reload: async () => {}, create: async () => {} },
};

function claimPlan(overrides = {}) {
  return {
    claim_id: "claim-1",
    original_text: "The city has twelve public parks.",
    normalized_claim: "the city has twelve public parks",
    claim_type: "factual",
    time_sensitive: false,
    entities: [],
    location: null,
    time_reference: null,
    queries: [],
    followup_queries: [],
    rounds: 1,
    matrix: {
      supporting: [{ title: "Official report", url: "https://example.org/report", publisher: "Example", published_at: "2026-01-01", query: "twelve public parks", query_category: "supporting", retrieval_round: 1, excerpt: "The city has twelve public parks.", relevance_score: 0.9 }],
      contradicting: [],
      context: [{ title: "City overview", url: "https://example.org/overview", publisher: "Example", published_at: null, query: "city parks overview", query_category: "official_source", retrieval_round: 1, excerpt: "General background on city parks.", relevance_score: 0.4 }],
      unresolved: [],
    },
    source_quality: { total_evidence: 2, independent_domain_count: 1, independent_publisher_count: 1, evidence_with_published_date_count: 1, duplicate_evidence_removed_count: 0 },
    classifier: "mock_classifier",
    verdict: { verdict: "TRUE", internal_verdict: "SUPPORTED", rationale: "The evidence supports the claim.", confidence_score: 0.8, verifier: "mock_verifier" },
    provider: "mock",
    provider_errors: [],
    ...overrides,
  };
}

const singleClaimResponse = { claims: [claimPlan()], multiple_claims_detected: false };
const multiClaimResponse = {
  claims: [
    claimPlan(),
    claimPlan({ claim_id: "claim-2", original_text: "The park opened in 2010.", verdict: { verdict: "FALSE", internal_verdict: "CONTRADICTED", rationale: "The evidence contradicts the claim.", confidence_score: 0.7, verifier: "mock_verifier" } }),
  ],
  multiple_claims_detected: true,
};

let nextResponse = singleClaimResponse;

globalThis.fetch = async (url, options = {}) => {
  if (url.endsWith("/api/v2/verify")) {
    deepFetches += 1;
    lastDeepUrl = url;
    lastDeepBody = JSON.parse(options.body);
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
const tooShort = await send({ type: "DEEP_CHECK", text: "short" });
if (tooShort.ok || tooShort.code !== "INVALID_DEEP_CHECK") throw new Error("Short text was not rejected.");
if (deepFetches !== 0) throw new Error("Short text incorrectly reached the backend.");

// A single-claim response routes through /api/v2/verify with {text, language}.
const single = await send({ type: "DEEP_CHECK", text: "The city has twelve public parks.", language: "en" });
if (!single.ok) throw new Error("Single-claim deep check failed.");
if (deepFetches !== 1) throw new Error(`Expected exactly one fetch, got ${deepFetches}.`);
if (!lastDeepUrl.endsWith("/api/v2/verify")) throw new Error("Deep check did not call /api/v2/verify.");
if (lastDeepBody.text !== "The city has twelve public parks." || lastDeepBody.language !== "en") throw new Error("Deep check request body mismatch.");
if (single.data.claims.length !== 1 || single.data.claims[0].verdict.verdict !== "TRUE") throw new Error("Single-claim response shape mismatch.");

// Default language falls back to "en" when not supplied.
nextResponse = singleClaimResponse;
const defaultLanguage = await send({ type: "DEEP_CHECK", text: "The city has twelve public parks." });
if (!defaultLanguage.ok) throw new Error("Deep check without explicit language failed.");
if (lastDeepBody.language !== "en") throw new Error("Deep check did not default language to en.");

// A multi-claim response is passed through untouched, including multiple_claims_detected.
nextResponse = multiClaimResponse;
const multi = await send({ type: "DEEP_CHECK", text: "The city has twelve public parks. The park opened in 2010." });
if (!multi.ok) throw new Error("Multi-claim deep check failed.");
if (multi.data.claims.length !== 2 || multi.data.multiple_claims_detected !== true) throw new Error("Multi-claim response shape mismatch.");
if (multi.data.claims[1].verdict.verdict !== "FALSE") throw new Error("Second claim's verdict was not preserved.");

// A malformed backend response (missing claims array) is rejected, not passed through.
nextResponse = { not_claims: [] };
const malformed = await send({ type: "DEEP_CHECK", text: "The city has twelve public parks." });
if (malformed.ok || malformed.code !== "INVALID_DEEP_RESPONSE") throw new Error("Malformed deep check response was not rejected.");

console.log(JSON.stringify({ ok: true, deepFetches, shortTextRejected: true, singleClaimRouted: true, defaultLanguageApplied: true, multiClaimPassedThrough: true, malformedResponseRejected: true }));
