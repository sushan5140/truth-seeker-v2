const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const PRIMARY_BACKEND = "http://127.0.0.1:8000";
const LOCALHOST_BACKEND = "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 10_000;

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

async function fetchJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
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

async function runSelfTest() {
  const health127 = await fetchJson(`${PRIMARY_BACKEND}/health`);
  const healthLocalhost = await fetchJson(`${LOCALHOST_BACKEND}/health`);
  const tone = await checkTone("This idea is stupid and completely useless.", "balanced");
  return {
    ok: health127.ok && healthLocalhost.ok && tone.ok,
    extensionVersion: EXTENSION_VERSION,
    backgroundWorkerReachable: true,
    health127: { ok: health127.ok, status: health127.status ?? null, code: health127.code ?? null },
    healthLocalhost: { ok: healthLocalhost.ok, status: healthLocalhost.status ?? null, code: healthLocalhost.code ?? null },
    toneEndpoint: { ok: tone.ok, code: tone.code ?? null },
    provider: tone.ok ? tone.data.provider : null,
    errorCode: health127.code || healthLocalhost.code || tone.code || null,
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "CHECK_TONE") {
    checkTone(message.text, message.sensitivity).then(sendResponse).catch(() => sendResponse(failure("WORKER_ERROR", "The background worker failed.")));
    return true;
  }
  if (message?.type === "RUN_BACKEND_SELF_TEST") {
    runSelfTest().then(sendResponse).catch(() => sendResponse(failure("SELF_TEST_ERROR", "The background self-test failed.")));
    return true;
  }
  return false;
});
