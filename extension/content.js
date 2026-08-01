(() => {
  "use strict";

  const MIN_LENGTH = 8;
  const DEBOUNCE_MS = 750;
  const CARD_ATTRIBUTE = "data-truth-tone-card";
  const SENSITIVE_PATTERN = /password|passcode|secret|token|credit|card.?number|cvv|cvc|ssn|social.?security|one.?time|otp|pin/i;

  let activeEditor = null;
  let card = null;
  let debounceTimer = null;
  let requestNumber = 0;
  let lastCheckedText = "";
  let toneSettings = { ...TruthToneSettings.DEFAULTS };
  const suppressedText = new WeakMap();
  const detectedEditors = new WeakSet();
  const lastRequestedText = new WeakMap();
  const failedRequestCooldown = new WeakMap();

  function isEditor(element) {
    if (!(element instanceof HTMLElement) || element.closest(`[${CARD_ATTRIBUTE}]`)) return false;
    if (element.matches('textarea, input[type="text"]')) return !element.disabled && !element.readOnly;
    return element.matches('[contenteditable="true"], [contenteditable="plaintext-only"], [role="textbox"], .ProseMirror') && element.getAttribute("aria-disabled") !== "true";
  }

  function findEditor(target) {
    if (!(target instanceof Element)) return null;
    const candidate = target.closest('textarea, input[type="text"], [contenteditable="true"], [contenteditable="plaintext-only"], [role="textbox"], .ProseMirror');
    return isEditor(candidate) ? candidate : null;
  }

  function isSensitive(editor) {
    if (!editor) return true;
    if (editor instanceof HTMLInputElement && editor.type === "password") return true;
    const indicators = [editor.id, editor.getAttribute("name"), editor.getAttribute("autocomplete"), editor.getAttribute("aria-label"), editor.getAttribute("placeholder")].filter(Boolean).join(" ");
    return SENSITIVE_PATTERN.test(indicators) || Boolean(editor.closest('[data-sensitive="true"], [aria-label*="password" i], [class*="password" i]'));
  }

  function readText(editor) {
    if (editor instanceof HTMLInputElement || editor instanceof HTMLTextAreaElement) return editor.value.trim();
    return (editor.innerText || editor.textContent || "").trim();
  }

  function hideCard() {
    card?.remove();
    card = null;
  }

  function makeButton(label, className, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      action();
    });
    return button;
  }

  function ensureCard() {
    if (card?.isConnected) return card;
    card = document.createElement("aside");
    card.setAttribute(CARD_ATTRIBUTE, "");
    card.setAttribute("role", "status");
    card.setAttribute("aria-live", "polite");
    document.documentElement.appendChild(card);
    return card;
  }

  function positionCard() {
    if (!card || !activeEditor?.isConnected) return;
    const rect = activeEditor.getBoundingClientRect();
    const width = Math.min(360, Math.max(280, rect.width));
    const left = Math.min(window.innerWidth - width - 12, Math.max(12, rect.left));
    card.style.width = `${width}px`;
    card.style.left = `${left}px`;
    const measuredHeight = card.offsetHeight || 150;
    const above = rect.top - measuredHeight - 10;
    card.style.top = `${above >= 12 ? above : Math.min(window.innerHeight - measuredHeight - 12, rect.bottom + 10)}px`;
  }

  function showLoading() {
    const container = ensureCard();
    container.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "ttg-loading";
    loading.textContent = "Checking tone locally…";
    container.appendChild(loading);
    positionCard();
  }

  function showUnavailable() {
    const container = ensureCard();
    container.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = "Tone check unavailable";
    const message = document.createElement("p");
    message.textContent = "Start the local Truth & Tone Guardian backend, then continue typing.";
    const actions = document.createElement("div");
    actions.className = "ttg-actions";
    actions.appendChild(makeButton("Dismiss", "ttg-button ttg-secondary", hideCard));
    container.append(title, message, actions);
    positionCard();
  }

  function setNativeValue(editor, value) {
    const prototype = editor instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) setter.call(editor, value);
    else editor.value = value;
  }

  function placeCursorAtEnd(editor) {
    if (editor instanceof HTMLInputElement || editor instanceof HTMLTextAreaElement) {
      editor.setSelectionRange(editor.value.length, editor.value.length);
      return;
    }
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function replaceEditorText(editor, suggestion) {
    editor.focus({ preventScroll: true });
    if (editor instanceof HTMLInputElement || editor instanceof HTMLTextAreaElement) {
      setNativeValue(editor, suggestion);
    } else {
      editor.replaceChildren(document.createTextNode(suggestion));
    }
    editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertReplacementText", data: suggestion }));
    editor.dispatchEvent(new Event("change", { bubbles: true }));
    placeCursorAtEnd(editor);
    suppressedText.set(editor, suggestion.trim());
    lastCheckedText = suggestion.trim();
    hideCard();
  }

  function showSuggestion(data, checkedText) {
    const editor = activeEditor;
    if (!editor?.isConnected || readText(editor) !== checkedText) return;
    const container = ensureCard();
    container.replaceChildren();

    const label = document.createElement("strong");
    label.className = "ttg-label";
    const dismissive = data.category?.includes("dismissive") || ["nuts", "stupid", "ridiculous", "useless"].includes(data.problematic_phrase);
    label.textContent = dismissive ? "This may sound dismissive." : "This message may sound impolite.";
    const heading = document.createElement("span");
    heading.className = "ttg-heading";
    heading.textContent = "Suggested rewrite";
    const suggestion = document.createElement("p");
    suggestion.className = "ttg-suggestion";
    suggestion.textContent = data.suggested_rewrite;
    const actions = document.createElement("div");
    actions.className = "ttg-actions";
    actions.append(
      makeButton("Replace", "ttg-button ttg-primary", () => replaceEditorText(editor, data.suggested_rewrite)),
      makeButton("Ignore", "ttg-button ttg-secondary", () => { suppressedText.set(editor, checkedText); hideCard(); }),
      makeButton("Dismiss", "ttg-button ttg-quiet", () => { suppressedText.set(editor, checkedText); hideCard(); }),
    );
    container.append(label, heading, suggestion, actions);
    positionCard();
    container.querySelector("button")?.focus({ preventScroll: true });
  }

  function requestTone(text) {
    return new Promise((resolve, reject) => {
      if (!globalThis.chrome?.runtime?.sendMessage) {
        reject(new Error("Extension background worker unavailable"));
        return;
      }
      const timer = window.setTimeout(() => {
        const error = new Error("Tone request timed out");
        error.name = "TimeoutError";
        reject(error);
      }, 10_200);
      chrome.runtime.sendMessage({ type: "CHECK_TONE", text, sensitivity: toneSettings.toneSensitivity }, (response) => {
        clearTimeout(timer);
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!response?.ok) {
          const error = new Error(`${response?.code || "UNKNOWN_ERROR"}: ${response?.error || "Tone request failed"}`);
          if (response?.code === "TIMEOUT") error.name = "TimeoutError";
          reject(error);
          return;
        }
        resolve(response.data);
      });
    });
  }

  async function analyze(editor, { showProgress = true } = {}) {
    if (!isEditor(editor)) { hideCard(); return; }
    activeEditor = editor;
    const text = readText(editor);
    if (!TruthToneSettings.shouldAnalyze(toneSettings, text.length, isSensitive(editor))) { hideCard(); return; }
    const failedRequest = failedRequestCooldown.get(editor);
    if (failedRequest?.text === text && failedRequest.retryAfter > Date.now()) { hideCard(); return; }
    if (text.length < MIN_LENGTH || suppressedText.get(editor) === text || lastRequestedText.get(editor) === text) { hideCard(); return; }
    lastCheckedText = text;
    lastRequestedText.set(editor, text);
    const currentRequest = ++requestNumber;
    const loadingTimer = showProgress ? window.setTimeout(showLoading, 400) : null;
    try {
      const data = await requestTone(text);
      if (loadingTimer) clearTimeout(loadingTimer);
      if (!toneSettings.automaticToneGuard || currentRequest !== requestNumber || activeEditor !== editor || readText(editor) !== text) return;
      if (typeof data.intervention_needed !== "boolean" || typeof data.suggested_rewrite !== "string") throw new Error("Invalid tone response");
      if (data.intervention_needed && data.confidence === "high") showSuggestion(data, text);
      else hideCard();
    } catch (error) {
      if (loadingTimer) clearTimeout(loadingTimer);
      lastRequestedText.delete(editor);
      failedRequestCooldown.set(editor, { text, retryAfter: Date.now() + 3_000 });
      const code = String(error?.message || "UNKNOWN_ERROR").split(":", 1)[0];
      console.error("[TruthTone] tone check failed", { code });
      if (currentRequest === requestNumber && activeEditor === editor) {
        if (error?.name === "TimeoutError") hideCard();
        else showUnavailable();
      }
    }
  }

  function schedule(editor) {
    clearTimeout(debounceTimer);
    requestNumber += 1;
    activeEditor = editor;
    hideCard();
    if (!isEditor(editor)) return;
    if (!TruthToneSettings.shouldAnalyze(toneSettings, readText(editor).length, isSensitive(editor))) return;
    debounceTimer = window.setTimeout(() => analyze(editor), DEBOUNCE_MS);
  }

  document.addEventListener("focusin", (event) => {
    const editor = findEditor(event.target);
    if (editor && !detectedEditors.has(editor)) {
      detectedEditors.add(editor);
    }
    if (editor && editor !== activeEditor) {
      activeEditor = editor;
      lastCheckedText = "";
      hideCard();
    }
  }, true);

  document.addEventListener("input", (event) => {
    const editor = findEditor(event.target);
    if (editor) schedule(editor);
  }, true);

  document.addEventListener("keyup", (event) => {
    const editor = findEditor(event.target);
    if (editor && !event.ctrlKey && !event.metaKey && !event.altKey) schedule(editor);
  }, true);

  document.addEventListener("focusout", (event) => {
    const editor = findEditor(event.target);
    if (editor && !event.relatedTarget?.closest?.(`[${CARD_ATTRIBUTE}]`)) {
      clearTimeout(debounceTimer);
      analyze(editor, { showProgress: false });
    }
  }, true);

  document.addEventListener("click", (event) => {
    const submitter = event.target.closest?.('button, input[type="submit"], [role="button"]');
    if (!submitter || submitter.closest(`[${CARD_ATTRIBUTE}]`) || !activeEditor) return;
    const marker = [submitter.type, submitter.getAttribute("aria-label"), submitter.getAttribute("title"), submitter.textContent].filter(Boolean).join(" ");
    if (/submit|send|post|comment|reply/i.test(marker)) analyze(activeEditor, { showProgress: false });
  }, true);

  window.addEventListener("scroll", positionCard, true);
  window.addEventListener("resize", positionCard);

  new MutationObserver(() => {
    if (activeEditor && !activeEditor.isConnected) { activeEditor = null; hideCard(); }
    else positionCard();
  }).observe(document.documentElement, { childList: true, subtree: true });

  TruthToneSettings.read()
    .then((settings) => { toneSettings = settings; })
    .catch(() => console.error("[TruthTone] settings load failed", { code: "SETTINGS_READ_FAILED" }));

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local" || (!changes.automaticToneGuard && !changes.toneSensitivity)) return;
    toneSettings = TruthToneSettings.normalize({
      automaticToneGuard: changes.automaticToneGuard?.newValue ?? toneSettings.automaticToneGuard,
      toneSensitivity: changes.toneSensitivity?.newValue ?? toneSettings.toneSensitivity,
    });
    clearTimeout(debounceTimer);
    requestNumber += 1;
    hideCard();
    if (toneSettings.automaticToneGuard && activeEditor) {
      lastRequestedText.delete(activeEditor);
      schedule(activeEditor);
    }
  });

  if (globalThis.chrome?.runtime?.onMessage) {
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message.type === "GET_SELECTED_TEXT") sendResponse({ text: window.getSelection()?.toString().trim() || "" });
    });
  }
})();
