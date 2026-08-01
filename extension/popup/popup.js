const API = "http://127.0.0.1:8000";
const elements = Object.fromEntries([...document.querySelectorAll("[id]")].map((el) => [el.id, el]));

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
      `Provider: ${result?.provider || "Unavailable"}`,
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
  updateCount(kind); chrome.storage.local.set({ [`last${kind[0].toUpperCase()}${kind.slice(1)}Text`]: elements[`${kind}-text`].value });
}));

elements["claim-button"].addEventListener("click", async () => {
  const text = elements["claim-text"].value.trim(); show("claim-result", false);
  if (!text) { setText("claim-status", "Enter or select a claim first."); return; }
  setText("claim-status", "Checking locally…"); setBusy(elements["claim-button"], true, "Checking…", "Check Claim");
  try {
    const data = await post("/api/check-claim", text);
    if (!data.status || !data.explanation || !data.confidence) throw new Error("The backend returned an invalid response.");
    setText("claim-badge", data.status.replaceAll("_", " ")); setText("claim-confidence", `Confidence: ${data.confidence}`);
    setText("claim-explanation", data.explanation); setText("claim-limitations", data.limitations || ""); setText("claim-status", "Check complete."); show("claim-result", true);
  } catch (error) { setText("claim-status", error.message); }
  finally { setBusy(elements["claim-button"], false, "Checking…", "Check Claim"); }
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
  elements["claim-text"].value = saved.lastClaimText || ""; elements["tone-text"].value = saved.lastToneText || "";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { type: "GET_SELECTED_TEXT" });
      if (response?.text) { elements["claim-text"].value = response.text.slice(0, 5000); await chrome.storage.local.set({ lastClaimText: elements["claim-text"].value }); }
    }
  } catch (_error) { /* Browser-internal pages do not allow content scripts. Saved/manual text still works. */ }
  updateCount("claim"); updateCount("tone");
}
async function saveToneSettings() {
  await TruthToneSettings.save({
    automaticToneGuard: elements["automatic-tone-guard"].value === "on",
    toneSensitivity: elements["tone-sensitivity"].value,
  });
  setText("settings-status", "Settings saved.");
  setTimeout(() => setText("settings-status", ""), 1200);
}
elements["automatic-tone-guard"].addEventListener("change", saveToneSettings);
elements["tone-sensitivity"].addEventListener("change", saveToneSettings);
elements["privacy-dismiss"].addEventListener("click", async () => {
  await chrome.storage.local.set({ geminiPrivacyNoticeSeen: true });
  show("privacy-notice", false);
});
initialize();
