const API = "http://127.0.0.1:8000";
const elements = Object.fromEntries([...document.querySelectorAll("[id]")].map((element) => [element.id, element]));
const CLAIM_LABELS = { supported: "Supported", contradicted: "Contradicted", mixed: "Mixed evidence", insufficient_evidence: "Insufficient evidence", opinion: "Opinion", invalid: "Invalid claim" };
const DEEP_VERDICT_LABELS = { TRUE: "True", FALSE: "False", MISLEADING: "Misleading", INSUFFICIENT_EVIDENCE: "Insufficient evidence" };
const DEEP_VERDICT_BADGE_CLASS = { TRUE: "badge-true", FALSE: "badge-false", MISLEADING: "badge-misleading", INSUFFICIENT_EVIDENCE: "badge-insufficient" };
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
  show("claim-panel", tab.dataset.tab === "claim"); show("tone-panel", tab.dataset.tab === "tone"); show("deep-panel", tab.dataset.tab === "deep"); show("quickask-panel", tab.dataset.tab === "quickask"); show("live-panel", tab.dataset.tab === "live");
}));

["claim", "tone", "deep", "quickask"].forEach((kind) => elements[`${kind}-text`].addEventListener("input", () => {
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

function buildDeepEvidenceCard(item) {
  const source = document.createElement("section");
  source.className = "popup-evidence";
  const title = document.createElement("strong");
  title.textContent = item.title || item.url;
  const meta = document.createElement("span");
  meta.textContent = [item.publisher || "Unknown publisher", item.published_at || "Date unavailable"].join(" · ");
  const excerpt = document.createElement("p");
  excerpt.textContent = item.excerpt;
  const open = document.createElement("button");
  open.type = "button";
  open.className = "secondary";
  open.textContent = "Open source";
  open.addEventListener("click", () => chrome.runtime.sendMessage({ type: "OPEN_SOURCE", url: item.url }));
  source.append(title, meta, excerpt, open);
  return source;
}

function buildDeepEvidenceGroup(kind, label, items) {
  const details = document.createElement("details");
  details.className = `evidence-group ${kind}`;
  const summary = document.createElement("summary");
  summary.textContent = `${label} (${items.length})`;
  details.appendChild(summary);
  if (items.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "No evidence found in this category.";
    details.appendChild(empty);
  } else {
    items.forEach((item) => details.appendChild(buildDeepEvidenceCard(item)));
  }
  return details;
}

function buildDeepClaimCard(claim) {
  const card = document.createElement("div");
  card.className = "result";

  const title = document.createElement("div");
  title.className = "result-title";
  const badge = document.createElement("span");
  badge.className = `badge ${DEEP_VERDICT_BADGE_CLASS[claim.verdict.verdict] || ""}`;
  badge.textContent = DEEP_VERDICT_LABELS[claim.verdict.verdict] || claim.verdict.verdict;
  title.appendChild(badge);
  card.appendChild(title);

  const claimText = document.createElement("p");
  claimText.textContent = claim.original_text;
  card.appendChild(claimText);

  const rationale = document.createElement("p");
  rationale.className = "deep-rationale";
  rationale.textContent = claim.verdict.rationale;
  card.appendChild(rationale);

  card.appendChild(buildDeepEvidenceGroup("supporting", "Supporting", claim.matrix.supporting));
  card.appendChild(buildDeepEvidenceGroup("contradicting", "Contradicting", claim.matrix.contradicting));
  card.appendChild(buildDeepEvidenceGroup("context", "Context", claim.matrix.context));

  const quality = document.createElement("p");
  quality.className = "source-quality-line";
  const domainCount = claim.source_quality.independent_domain_count;
  const dateCount = claim.source_quality.evidence_with_published_date_count;
  quality.textContent = `${domainCount} independent domain${domainCount === 1 ? "" : "s"} · ${dateCount} with a published date`;
  card.appendChild(quality);

  if (claim.rounds > 1) {
    const note = document.createElement("p");
    note.className = "round-note";
    note.textContent = "A follow-up search ran to fill evidence gaps.";
    card.appendChild(note);
  }

  return card;
}

function renderDeepResult(data) {
  elements["deep-results"].replaceChildren();
  data.claims.forEach((claim) => elements["deep-results"].appendChild(buildDeepClaimCard(claim)));
}

elements["deep-button"].addEventListener("click", async () => {
  const text = elements["deep-text"].value.trim();
  elements["deep-results"].replaceChildren();
  if (text.length < 12) { setText("deep-status", "Enter text of at least 12 characters."); return; }
  setText("deep-status", "Searching and comparing evidence…");
  setBusy(elements["deep-button"], true, "Checking…", "Deep Check");
  try {
    const response = await chrome.runtime.sendMessage({ type: "DEEP_CHECK", text, language: navigator.language || "en" });
    if (!response?.ok) throw new Error(`${response?.code || "DEEP_CHECK_FAILED"}: ${response?.error || "Deep check failed"}`);
    renderDeepResult(response.data);
    setText("deep-status", response.data.multiple_claims_detected ? `Check complete. ${response.data.claims.length} claims found.` : "Check complete.");
  } catch (error) {
    setText("deep-status", error.message);
  } finally {
    setBusy(elements["deep-button"], false, "Checking…", "Deep Check");
  }
});

function renderQuickAskResult(data) {
  setText("quickask-answer", data.answer);
  elements["quickask-sources"].replaceChildren();
  if (data.sources.length > 0) {
    const heading = document.createElement("strong");
    heading.className = "quickask-sources-heading";
    heading.textContent = "Sources Gemma looked at";
    elements["quickask-sources"].appendChild(heading);
    data.sources.forEach((url) => {
      const link = document.createElement("button");
      link.type = "button";
      link.className = "quickask-source-link";
      link.textContent = url;
      link.addEventListener("click", () => chrome.runtime.sendMessage({ type: "OPEN_SOURCE", url }));
      elements["quickask-sources"].appendChild(link);
    });
  }
}

elements["quickask-button"].addEventListener("click", async () => {
  const text = elements["quickask-text"].value.trim();
  show("quickask-result", false);
  if (text.length < 4) { setText("quickask-status", "Enter a question first."); return; }
  setText("quickask-status", "Asking Gemma…");
  setBusy(elements["quickask-button"], true, "Asking…", "Quick Ask");
  try {
    const response = await chrome.runtime.sendMessage({ type: "QUICK_ASK", text });
    if (!response?.ok) throw new Error(`${response?.code || "QUICK_ASK_FAILED"}: ${response?.error || "Quick Ask failed"}`);
    renderQuickAskResult(response.data);
    setText("quickask-status", response.data.available ? "" : "Quick Ask isn't available right now.");
    show("quickask-result", true);
  } catch (error) {
    setText("quickask-status", error.message);
  } finally {
    setBusy(elements["quickask-button"], false, "Asking…", "Quick Ask");
  }
});

function setLiveListening(listening) {
  elements["live-start-button"].disabled = listening;
  elements["live-stop-button"].disabled = !listening;
  elements["live-dot"].classList.toggle("listening", listening);
  setText("live-indicator-text", listening ? "Listening…" : "Idle");
}

elements["live-start-button"].addEventListener("click", async () => {
  if (!activeTab?.id) { setText("live-status", "No active tab to listen to."); return; }
  setText("live-status", "Starting…");
  elements["live-start-button"].disabled = true;
  try {
    const response = await chrome.runtime.sendMessage({ type: "LIVE_CHECK_START", tabId: activeTab.id });
    if (!response?.ok) throw new Error(`${response?.code || "LIVE_CHECK_FAILED"}: ${response?.error || "Live Check failed to start"}`);
    setText("live-transcript", "");
    show("live-transcript-box", true);
    setLiveListening(true);
  } catch (error) {
    elements["live-start-button"].disabled = false;
    setText("live-status", error.message);
  }
});

elements["live-stop-button"].addEventListener("click", async () => {
  elements["live-stop-button"].disabled = true;
  try {
    await chrome.runtime.sendMessage({ type: "LIVE_CHECK_STOP" });
  } catch (_error) {
    // Background worker unreachable -- reset local state regardless.
  }
  setLiveListening(false);
  setText("live-status", "Stopped.");
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "LIVE_CHECK_STARTED") {
    setText("live-status", "Listening…");
  } else if (message?.type === "LIVE_CHECK_TRANSCRIPT_UPDATE") {
    setText("live-transcript", message.transcript || "");
    elements["live-transcript"].scrollTop = elements["live-transcript"].scrollHeight;
  } else if (message?.type === "LIVE_CHECK_ERROR") {
    setLiveListening(false);
    setText("live-status", message.message || "Live Check hit an error.");
  } else if (message?.type === "LIVE_CHECK_ENDED") {
    setLiveListening(false);
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
    chrome.storage.local.get(["lastClaimText", "lastToneText", "lastDeepText", "lastQuickaskText", "geminiPrivacyNoticeSeen"]),
    TruthToneSettings.read(),
  ]);
  elements["automatic-tone-guard"].value = toneSettings.automaticToneGuard ? "on" : "off";
  elements["tone-sensitivity"].value = toneSettings.toneSensitivity;
  show("privacy-notice", !saved.geminiPrivacyNoticeSeen);
  elements["claim-text"].value = saved.lastClaimText || "";
  elements["tone-text"].value = saved.lastToneText || "";
  elements["deep-text"].value = saved.lastDeepText || "";
  elements["quickask-text"].value = saved.lastQuickaskText || "";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    activeTab = tab || null;
    if (tab?.id) {
      const response = await chrome.tabs.sendMessage(tab.id, { type: "GET_SELECTED_TEXT" });
      if (response?.text) {
        const selected = response.text.slice(0, 5000);
        elements["claim-text"].value = selected;
        elements["deep-text"].value = selected;
        elements["quickask-text"].value = selected;
        await chrome.storage.local.set({ lastClaimText: selected, lastDeepText: selected, lastQuickaskText: selected });
      }
    }
  } catch (_error) { /* Protected browser pages do not allow content scripts. */ }
  updateCount("claim"); updateCount("tone"); updateCount("deep"); updateCount("quickask");
  try {
    const state = await chrome.runtime.sendMessage({ type: "LIVE_CHECK_GET_STATE" });
    if (state?.listening) {
      setLiveListening(true);
      setText("live-status", "Listening… (resumed after reopening the popup)");
      show("live-transcript-box", true);
    } else {
      setLiveListening(false);
    }
  } catch (_error) {
    setLiveListening(false);
  }
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
