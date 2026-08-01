const API = "http://127.0.0.1:8000";
const elements = Object.fromEntries([...document.querySelectorAll("[id]")].map((element) => [element.id, element]));
const CLAIM_LABELS = { supported: "Supported", contradicted: "Contradicted", mixed: "Mixed evidence", insufficient_evidence: "Insufficient evidence", opinion: "Opinion", invalid: "Invalid claim" };
let activeTab = null;
let lastClaimResult = null;

function setText(id, value) { elements[id].textContent = value; }
function show(id, visible) { elements[id].classList.toggle("hidden", !visible); }
function setBusy(button, busy, busyText, normalText) { button.disabled = busy; button.textContent = busy ? busyText : normalText; }
function updateCount(kind) { setText(`${kind}-count`, `${elements[`${kind}-text`].value.length.toLocaleString()} / 5,000`); }

async function updateConnectionStatus() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "RUN_BACKEND_SELF_TEST" });
    const connected = response?.ok;
    setText("connection-status", connected ? "Connected" : "Disconnected");
    elements["connection-dot"].className = `connection-dot ${connected ? "connected" : "disconnected"}`;
    setText("gemini-status", connected && response.provider === "gemini" ? "Gemini enabled" : "Gemini unavailable");
    setText("fallback-status", connected ? "Local fallback enabled" : "Local fallback unavailable");
  } catch (_error) {
    setText("connection-status", "Disconnected");
    elements["connection-dot"].className = "connection-dot disconnected";
    setText("gemini-status", "Gemini unavailable");
    setText("fallback-status", "Local fallback unavailable");
  }
}

elements["diagnostic-button"].addEventListener("click", async () => {
  setBusy(elements["diagnostic-button"], true, "Testing…", "Test backend connection");
  show("diagnostic-results", true);
  setText("diagnostic-results", "Running background-worker self-test…");
  try {
    const result = await chrome.runtime.sendMessage({ type: "RUN_BACKEND_SELF_TEST" });
    const mark = (value) => value ? "Success" : "Failed";
    setText("diagnostic-results", [
      `Extension version: ${result?.extensionVersion || chrome.runtime.getManifest().version}`,
      `Background worker reachable: ${mark(result?.backgroundWorkerReachable)}`,
      `127.0.0.1 health: ${mark(result?.health127?.ok)}${result?.health127?.status ? ` (HTTP ${result.health127.status})` : ""}`,
      `localhost health: ${mark(result?.healthLocalhost?.ok)}${result?.healthLocalhost?.status ? ` (HTTP ${result.healthLocalhost.status})` : ""}`,
      `Tone endpoint: ${mark(result?.toneEndpoint?.ok)}`,
      `Truth status: ${mark(result?.truthStatus?.backend_status === "running")}`,
      `Search provider: ${result?.truthStatus?.search_provider_configured || "Unavailable"}`,
      `Extraction provider: ${result?.truthStatus?.extraction_provider_configured || "Unavailable"}`,
      `Claim cache: ${mark(result?.truthStatus?.cache_available)}`,
      `Tone provider: ${result?.provider || "Unavailable"}`,
      `Error code: ${result?.errorCode || "None"}`,
    ].join("\n"));
    await updateConnectionStatus();
  } catch (error) {
    setText("diagnostic-results", `Extension version: ${chrome.runtime.getManifest().version}\nBackground worker reachable: Failed\nError code: MESSAGE_FAILED\n${error?.message || "No response"}`);
  } finally {
    setBusy(elements["diagnostic-button"], false, "Testing…", "Test backend connection");
  }
});

async function post(path, text, extra = {}) {
  let response;
  try {
    response = await fetch(`${API}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, ...extra }) });
  } catch (_error) {
    throw new Error("Backend unavailable. Start the backend, then try again.");
  }
  let data;
  try { data = await response.json(); } catch (_error) { throw new Error("The backend returned an invalid response."); }
  if (!response.ok) throw new Error(data.message || "The request could not be checked.");
  return data;
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((item) => { item.classList.toggle("active", item === tab); item.setAttribute("aria-selected", String(item === tab)); });
  show("claim-panel", tab.dataset.tab === "claim"); show("tone-panel", tab.dataset.tab === "tone");
}));

["claim", "tone"].forEach((kind) => elements[`${kind}-text`].addEventListener("input", () => {
  updateCount(kind);
  chrome.storage.local.set({ [`last${kind[0].toUpperCase()}${kind.slice(1)}Text`]: elements[`${kind}-text`].value });
}));

function renderClaimResult(data) {
  lastClaimResult = data;
  setText("claim-badge", CLAIM_LABELS[data.verdict] || data.verdict);
  setText("claim-confidence", `Confidence: ${data.confidence}`);
  setText("claim-checked-text", data.claim);
  setText("claim-summary", data.summary);
  elements["claim-evidence"].replaceChildren();
  data.evidence.forEach((item) => {
    const source = document.createElement("section");
    source.className = "popup-evidence";
    const title = document.createElement("strong");
    title.textContent = item.title;
    const meta = document.createElement("span");
    meta.textContent = [item.publisher, item.published_at || "Date unavailable", item.stance].join(" · ");
    const excerpt = document.createElement("p");
    excerpt.textContent = item.excerpt;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "secondary";
    open.textContent = "Open source";
    open.addEventListener("click", () => chrome.runtime.sendMessage({ type: "OPEN_SOURCE", url: item.url }));
    source.append(title, meta, excerpt, open);
    elements["claim-evidence"].appendChild(source);
  });
  elements["claim-limitations"].replaceChildren();
  data.limitations.forEach((item) => {
    const entry = document.createElement("li");
    entry.textContent = item;
    elements["claim-limitations"].appendChild(entry);
  });
  setText("claim-certainty", data.certainty_notice);
  setText("claim-checked-at", `${data.cached ? "Cached" : "Fresh"} · Last checked ${new Date(data.checked_at).toLocaleString()}`);
}

elements["claim-button"].addEventListener("click", async () => {
  const text = elements["claim-text"].value.trim();
  show("claim-result", false);
  if (text.length < 12) { setText("claim-status", "Enter a factual claim of at least 12 characters."); return; }
  setText("claim-status", "Searching and comparing evidence…");
  setBusy(elements["claim-button"], true, "Checking…", "Check Claim");
  try {
    const response = await chrome.runtime.sendMessage({
      type: "CHECK_CLAIM",
      requestId: crypto.randomUUID(),
      text,
      sourceUrl: activeTab?.url?.startsWith("http") ? activeTab.url : null,
      pageTitle: activeTab?.title || null,
      language: navigator.language || "en",
    });
    if (!response?.ok) throw new Error(`${response?.code || "CLAIM_FAILED"}: ${response?.error || "Claim check failed"}`);
    renderClaimResult(response.data);
    setText("claim-status", "Check complete.");
    show("claim-result", true);
  } catch (error) {
    setText("claim-status", error.message);
  } finally {
    setBusy(elements["claim-button"], false, "Checking…", "Check Claim");
  }
});

elements["claim-copy"].addEventListener("click", async () => {
  if (!lastClaimResult) return;
  const sources = lastClaimResult.evidence.map((item) => `${item.title}: ${item.url}`).join("\n");
  const text = `${CLAIM_LABELS[lastClaimResult.verdict]} (${lastClaimResult.confidence})\n${lastClaimResult.claim}\n${lastClaimResult.summary}\n${sources}\n${lastClaimResult.certainty_notice}`;
  try {
    await navigator.clipboard.writeText(text);
    setText("claim-copy", "Copied!");
    setTimeout(() => setText("claim-copy", "Copy Result"), 1200);
  } catch (_error) {
    setText("claim-status", "Could not copy the result automatically.");
  }
});

elements["tone-button"].addEventListener("click", async () => {
  const text = elements["tone-text"].value.trim(); show("tone-result", false);
  if (!text) { setText("tone-status", "Enter a message first."); return; }
  setText("tone-status", "Checking locally…"); setBusy(elements["tone-button"], true, "Checking…", "Check Tone");
  try {
    const data = await post("/api/check-tone", text, { sensitivity: elements["tone-sensitivity"].value });
    if (typeof data.intervention_needed !== "boolean" || !data.category || typeof data.suggested_rewrite !== "string") throw new Error("The backend returned an invalid response.");
    setText("tone-badge", data.intervention_needed && data.confidence === "high" ? "Suggestion available" : "Tone looks neutral");
    setText("tone-phrase", data.problematic_phrase || "None"); setText("tone-explanation", data.explanation); elements["tone-suggestion"].value = data.suggested_rewrite;
    setText("tone-status", "Check complete."); show("tone-result", true);
  } catch (error) { setText("tone-status", error.message); }
  finally { setBusy(elements["tone-button"], false, "Checking…", "Check Tone"); }
});

elements["copy-button"].addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(elements["tone-suggestion"].value); setText("copy-button", "Copied!"); setTimeout(() => setText("copy-button", "Copy Suggestion"), 1200); }
  catch (_error) { setText("tone-status", "Could not copy automatically. Select and copy the suggestion manually."); }
});

async function initialize() {
  await updateConnectionStatus();
  const [saved, toneSettings] = await Promise.all([
    chrome.storage.local.get(["lastClaimText", "lastToneText", "geminiPrivacyNoticeSeen"]),
    TruthToneSettings.read(),
  ]);
  elements["automatic-tone-guard"].value = toneSettings.automaticToneGuard ? "on" : "off";
  elements["tone-sensitivity"].value = toneSettings.toneSensitivity;
  show("privacy-notice", !saved.geminiPrivacyNoticeSeen);
  elements["claim-text"].value = saved.lastClaimText || "";
  elements["tone-text"].value = saved.lastToneText || "";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    activeTab = tab || null;
    if (tab?.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { type: "GET_SELECTED_TEXT" });
      if (response?.text) {
        elements["claim-text"].value = response.text.slice(0, 5000);
        await chrome.storage.local.set({ lastClaimText: elements["claim-text"].value });
      }
    }
  } catch (_error) { /* Protected browser pages do not allow content scripts. */ }
  updateCount("claim"); updateCount("tone");
}

async function saveToneSettings() {
  await TruthToneSettings.save({ automaticToneGuard: elements["automatic-tone-guard"].value === "on", toneSensitivity: elements["tone-sensitivity"].value });
  setText("settings-status", "Settings saved.");
  setTimeout(() => setText("settings-status", ""), 1200);
}

elements["automatic-tone-guard"].addEventListener("change", saveToneSettings);
elements["tone-sensitivity"].addEventListener("change", saveToneSettings);
elements["privacy-dismiss"].addEventListener("click", async () => { await chrome.storage.local.set({ geminiPrivacyNoticeSeen: true }); show("privacy-notice", false); });
initialize();
