const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const PRIMARY_BACKEND = "http://127.0.0.1:8000";
const LOCALHOST_BACKEND = "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 10_000;
const CLAIM_TIMEOUT_MS = 22_000;
// /api/v2/verify runs decomposition, counter-evidence retrieval, evidence
// classification, and a live Gemini call per claim (more for multi-claim
// text) -- a much heavier pipeline than /api/check-claim, so it gets its
// own, longer budget rather than sharing CLAIM_TIMEOUT_MS.
const DEEP_CHECK_TIMEOUT_MS = 90_000;
// Quick Ask's OpenRouter call can involve multiple server-side
// web_search/web_fetch round-trips before a final answer comes back --
// its own budget too, not shared with Deep Check's.
const QUICK_ASK_TIMEOUT_MS = 60_000;
const OFFSCREEN_DOCUMENT_URL = "offscreen.html";
const claimControllers = new Map();
const recentClaimResponses = new Map();
// Guards against a second LIVE_CHECK_START racing a capture already in
// flight -- unlike the stateless HTTP tabs, background.js can hold real
// session state here, so this gets the in-flight guard those tabs don't.
let liveCheckSession = null;

// Temporary diagnostic instrumentation, mirroring offscreen.js's, for the
// ~30s auto-stop bug -- logs every Live Check message this service worker
// sees and every offscreen-document lifecycle action it takes. A fresh
// "service worker (re)evaluated" line appearing mid-session is itself a
// finding: it means Chrome killed and respawned this service worker,
// which would reset liveCheckSession back to null.
const LOG_PREFIX = "[LiveCheck background]";
function log(...args) {
  console.log(LOG_PREFIX, `t+${Math.round(performance.now())}ms`, ...args);
}
function logError(...args) {
  console.error(LOG_PREFIX, `t+${Math.round(performance.now())}ms`, ...args);
}
log("service worker (re)evaluated -- module top-level code just ran");

chrome.runtime.onInstalled.addListener(async () => {
  const saved = await chrome.storage.local.get(["automaticToneGuard", "toneSensitivity"]);
  await chrome.storage.local.set({
    installedVersion: EXTENSION_VERSION,
    automaticToneGuard: saved.automaticToneGuard ?? true,
    toneSensitivity: ["low", "balanced", "high"].includes(saved.toneSensitivity) ? saved.toneSensitivity : "balanced",
  });
  try {
    const chatTabs = await chrome.tabs.query({
      url: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
    });
    await Promise.all(chatTabs.filter((tab) => Number.isInteger(tab.id)).map((tab) => chrome.tabs.reload(tab.id)));
  } catch (error) {
    console.warn("[TruthTone Background] ChatGPT refresh skipped", { code: "TAB_REFRESH_FAILED", message: error?.message || "Unknown error" });
  }
});

function failure(code, message, details = {}) {
  return { ok: false, code, error: message, ...details };
}

async function fetchJson(url, options = {}, timeoutMs = REQUEST_TIMEOUT_MS, suppliedController = null) {
  const controller = suppliedController || new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    let data;
    try {
      data = await response.json();
    } catch (_error) {
      return failure("INVALID_JSON", "The backend returned invalid JSON.", { status: response.status });
    }
    if (!response.ok) {
      return failure("HTTP_ERROR", `The backend returned HTTP ${response.status}.`, { status: response.status, data });
    }
    return { ok: true, status: response.status, data };
  } catch (error) {
    if (error?.name === "AbortError") return failure("TIMEOUT", "The backend request timed out.");
    return failure("FETCH_FAILED", error?.message || "The backend could not be reached.");
  } finally {
    clearTimeout(timeout);
  }
}

function normalizeToneData(raw) {
  if (!raw || typeof raw !== "object") return null;
  const data = { ...raw, suggested_rewrite: raw.suggested_rewrite ?? raw.suggestion };
  if (
    typeof data.intervention_needed !== "boolean" ||
    typeof data.confidence !== "string" ||
    typeof data.suggested_rewrite !== "string" ||
    typeof data.explanation !== "string" ||
    typeof data.provider !== "string"
  ) return null;
  return data;
}

async function checkTone(text, sensitivity = "balanced") {
  if (typeof text !== "string" || text.trim().length < 8) {
    return failure("INVALID_REQUEST", "CHECK_TONE requires at least 8 characters of text.");
  }
  const normalizedSensitivity = ["low", "balanced", "high"].includes(sensitivity) ? sensitivity : "balanced";
  const result = await fetchJson(`${PRIMARY_BACKEND}/api/check-tone`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, sensitivity: normalizedSensitivity }),
  });
  if (!result.ok) {
    console.error("[TruthTone Background] tone request failed", { code: result.code });
    return result;
  }
  const data = normalizeToneData(result.data);
  if (!data) return failure("INVALID_TONE_RESPONSE", "The tone endpoint returned an unexpected response.");
  return { ok: true, data };
}

function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    const host = url.hostname.toLowerCase();
    if (host === 'localhost' || host.endsWith('.localhost') || /^127\.|^10\.|^192\.168\.|^169\.254\.|^0\./.test(host) || host === '::1') return null;
    return url.href;
  } catch (_error) {
    return null;
  }
}

async function checkClaim(message) {
  const text = typeof message.text === "string" ? message.text.trim() : "";
  if (text.length < 12 || text.length > 5000) return failure("INVALID_CLAIM", "Select a factual claim between 12 and 5,000 characters.");
  const requestId = message.requestId || crypto.randomUUID();
  const cacheKey = text.toLowerCase().replace(/\s+/g, " ");
  const recent = recentClaimResponses.get(cacheKey);
  if (!message.bypassCache && recent && Date.now() - recent.savedAt < 30_000) return { ok: true, data: recent.data };

  const controller = new AbortController();
  claimControllers.set(requestId, controller);
  try {
    const result = await fetchJson(`${PRIMARY_BACKEND}/api/check-claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        source_url: message.sourceUrl || null,
        page_title: message.pageTitle || null,
        language: message.language || "en",
        bypass_cache: message.bypassCache === true,
      }),
    }, CLAIM_TIMEOUT_MS, controller);
    if (!result.ok) return result;
    if (!result.data || typeof result.data.verdict !== "string" || !Array.isArray(result.data.evidence)) {
      return failure("INVALID_CLAIM_RESPONSE", "The claim endpoint returned an unexpected response.");
    }
    recentClaimResponses.set(cacheKey, { savedAt: Date.now(), data: result.data });
    return { ok: true, data: result.data };
  } finally {
    claimControllers.delete(requestId);
  }
}

async function checkDeep(message) {
  const text = typeof message.text === "string" ? message.text.trim() : "";
  if (text.length < 12 || text.length > 5000) return failure("INVALID_DEEP_CHECK", "Enter text between 12 and 5,000 characters.");
  const result = await fetchJson(`${PRIMARY_BACKEND}/api/v2/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, language: message.language || "en" }),
  }, DEEP_CHECK_TIMEOUT_MS);
  if (!result.ok) return result;
  if (!result.data || !Array.isArray(result.data.claims)) {
    return failure("INVALID_DEEP_RESPONSE", "The deep check endpoint returned an unexpected response.");
  }
  return { ok: true, data: result.data };
}

async function checkQuickAsk(message) {
  const text = typeof message.text === "string" ? message.text.trim() : "";
  if (text.length < 4 || text.length > 5000) return failure("INVALID_QUICK_ASK", "Enter a question between 4 and 5,000 characters.");
  const result = await fetchJson(`${PRIMARY_BACKEND}/api/v2/quick-ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }, QUICK_ASK_TIMEOUT_MS);
  if (!result.ok) return result;
  if (!result.data || typeof result.data.available !== "boolean" || typeof result.data.answer !== "string" || !Array.isArray(result.data.sources)) {
    return failure("INVALID_QUICK_ASK_RESPONSE", "The Quick Ask endpoint returned an unexpected response.");
  }
  return { ok: true, data: result.data };
}

async function ensureOffscreenDocument() {
  const existing = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  log("ensureOffscreenDocument: getContexts found", existing.length, "existing offscreen document(s)");
  if (existing.length > 0) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_DOCUMENT_URL,
    reasons: ["USER_MEDIA"],
    justification: "Capture and transcribe the active tab's audio for Live Check.",
  });
  log("ensureOffscreenDocument: createDocument() completed");
}

async function closeOffscreenDocumentIfOpen() {
  log("closeOffscreenDocumentIfOpen() called");
  try {
    const existing = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
    log("closeOffscreenDocumentIfOpen: getContexts found", existing.length, "existing offscreen document(s)");
    if (existing.length > 0) {
      await chrome.offscreen.closeDocument();
      log("closeOffscreenDocumentIfOpen: closeDocument() completed");
    }
  } catch (error) {
    logError("closeOffscreenDocumentIfOpen: threw", error?.name, error?.message);
  }
}

async function startLiveCheck(message) {
  log("startLiveCheck() called", { tabId: message.tabId });
  if (typeof chrome.tabCapture?.getMediaStreamId !== "function" || typeof chrome.offscreen?.createDocument !== "function") {
    logError("startLiveCheck: tabCapture/offscreen API unavailable");
    return failure("LIVE_CHECK_UNSUPPORTED", "This browser does not support tab audio capture.");
  }
  if (liveCheckSession) {
    log("startLiveCheck: rejected, session already active", liveCheckSession);
    return failure("LIVE_CHECK_ALREADY_ACTIVE", "Live Check is already listening to a tab.");
  }
  const tabId = message.tabId;
  if (!Number.isInteger(tabId)) {
    log("startLiveCheck: rejected, invalid tabId");
    return failure("INVALID_LIVE_CHECK_TAB", "No active tab to listen to.");
  }
  try {
    await ensureOffscreenDocument();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    log("startLiveCheck: got streamId, relaying to offscreen", { streamId });
    liveCheckSession = { tabId };
    chrome.runtime.sendMessage({ type: "LIVE_CHECK_OFFSCREEN_START", target: "offscreen", streamId }).catch((error) => {
      logError("startLiveCheck: relay sendMessage rejected", error?.message);
    });
    return { ok: true };
  } catch (error) {
    logError("startLiveCheck: threw", error?.name, error?.message);
    liveCheckSession = null;
    return failure("LIVE_CHECK_START_FAILED", error?.message || "Could not start capturing this tab's audio.");
  }
}

async function stopLiveCheck() {
  log("stopLiveCheck() called (explicit user stop)", { previousSession: liveCheckSession });
  liveCheckSession = null;
  chrome.runtime.sendMessage({ type: "LIVE_CHECK_OFFSCREEN_STOP", target: "offscreen" }).catch((error) => {
    logError("stopLiveCheck: relay sendMessage rejected", error?.message);
  });
  return { ok: true };
}

async function runSelfTest() {
  const health127 = await fetchJson(`${PRIMARY_BACKEND}/health`);
  const healthLocalhost = await fetchJson(`${LOCALHOST_BACKEND}/health`);
  const truthStatus = await fetchJson(`${PRIMARY_BACKEND}/api/truth/status`);
  const tone = await checkTone("This idea is stupid and completely useless.", "balanced");
  return {
    ok: health127.ok && healthLocalhost.ok && truthStatus.ok && tone.ok,
    extensionVersion: EXTENSION_VERSION,
    backgroundWorkerReachable: true,
    health127: { ok: health127.ok, status: health127.status ?? null, code: health127.code ?? null },
    healthLocalhost: { ok: healthLocalhost.ok, status: healthLocalhost.status ?? null, code: healthLocalhost.code ?? null },
    toneEndpoint: { ok: tone.ok, code: tone.code ?? null },
    truthStatus: truthStatus.ok ? truthStatus.data : { ok: false, code: truthStatus.code },
    provider: tone.ok ? tone.data.provider : null,
    errorCode: health127.code || healthLocalhost.code || truthStatus.code || tone.code || null,
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "CHECK_TONE") {
    checkTone(message.text, message.sensitivity).then(sendResponse).catch(() => sendResponse(failure("WORKER_ERROR", "The background worker failed.")));
    return true;
  }
  if (message?.type === "CHECK_CLAIM") {
    checkClaim(message).then(sendResponse).catch((error) => {
      const code = error?.name === "AbortError" ? "CLAIM_CANCELLED" : "CLAIM_WORKER_ERROR";
      sendResponse(failure(code, code === "CLAIM_CANCELLED" ? "Claim check cancelled." : "The claim check failed."));
    });
    return true;
  }
  if (message?.type === "DEEP_CHECK") {
    checkDeep(message).then(sendResponse).catch(() => sendResponse(failure("DEEP_WORKER_ERROR", "The deep check failed.")));
    return true;
  }
  if (message?.type === "QUICK_ASK") {
    checkQuickAsk(message).then(sendResponse).catch(() => sendResponse(failure("QUICK_ASK_WORKER_ERROR", "Quick Ask failed.")));
    return true;
  }
  if (message?.type === "LIVE_CHECK_START") {
    startLiveCheck(message).then(sendResponse).catch(() => sendResponse(failure("LIVE_CHECK_WORKER_ERROR", "Live Check failed to start.")));
    return true;
  }
  if (message?.type === "LIVE_CHECK_STOP") {
    stopLiveCheck().then(sendResponse).catch(() => sendResponse(failure("LIVE_CHECK_WORKER_ERROR", "Live Check failed to stop.")));
    return true;
  }
  if (message?.type === "LIVE_CHECK_GET_STATE") {
    log("LIVE_CHECK_GET_STATE queried", { listening: liveCheckSession !== null });
    sendResponse({ ok: true, listening: liveCheckSession !== null });
    return false;
  }
  if (message?.type === "LIVE_CHECK_ENDED" || message?.type === "LIVE_CHECK_ERROR") {
    // Broadcast from the offscreen document (not targeted at "offscreen"
    // itself) -- popup.js reacts to the same message for its own UI/
    // transcript update; this listener only clears orchestration state.
    log(`received ${message.type}`, message);
    liveCheckSession = null;
    closeOffscreenDocumentIfOpen();
    return false;
  }
  if (message?.type === "CANCEL_CLAIM") {
    claimControllers.get(message.requestId)?.abort();
    claimControllers.delete(message.requestId);
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "OPEN_SOURCE") {
    const safeUrl = safeSourceUrl(message.url);
    if (!safeUrl) {
      sendResponse(failure("UNSAFE_SOURCE_URL", "The source URL was blocked."));
      return false;
    }
    chrome.tabs.create({ url: safeUrl }).then(() => sendResponse({ ok: true })).catch(() => sendResponse(failure("OPEN_SOURCE_FAILED", "The source could not be opened.")));
    return true;
  }
  if (message?.type === "RUN_BACKEND_SELF_TEST") {
    runSelfTest().then(sendResponse).catch(() => sendResponse(failure("SELF_TEST_ERROR", "The background self-test failed.")));
    return true;
  }
  return false;
});
