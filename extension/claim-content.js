(() => {
  "use strict";

  const ACTION_ATTRIBUTE = "data-truth-tone-claim-action";
  const CARD_ATTRIBUTE = "data-truth-tone-claim-card";
  let actionButton = null;
  let resultCard = null;
  let selectedClaim = "";
  let selectionRect = null;
  let activeRequestId = null;

  function ownedUi(element) {
    return element?.closest?.(`[${ACTION_ATTRIBUTE}], [${CARD_ATTRIBUTE}], [data-truth-tone-card]`);
  }

  function selectionIsExcluded(selection) {
    const node = selection?.rangeCount ? selection.getRangeAt(0).commonAncestorContainer : null;
    const element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
    if (!element || ownedUi(element)) return true;
    return Boolean(element.closest('input, textarea, [contenteditable="true"], [contenteditable="plaintext-only"], [role="textbox"], [aria-label*="password" i], [class*="password" i]'));
  }

  function removeAction() {
    actionButton?.remove();
    actionButton = null;
  }

  function removeCard({ cancel = false } = {}) {
    if (cancel && activeRequestId) chrome.runtime.sendMessage({ type: "CANCEL_CLAIM", requestId: activeRequestId });
    activeRequestId = null;
    resultCard?.remove();
    resultCard = null;
  }

  function button(label, className, handler) {
    const element = document.createElement("button");
    element.type = "button";
    element.className = className;
    element.textContent = label;
    element.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      handler();
    });
    return element;
  }

  function position(element, rect, preferredWidth = 380) {
    const width = Math.min(preferredWidth, window.innerWidth - 24);
    const left = Math.min(window.innerWidth - width - 12, Math.max(12, rect.left));
    element.style.width = `${width}px`;
    element.style.left = `${left}px`;
    const height = element.offsetHeight || 170;
    const below = rect.bottom + 9;
    element.style.top = `${below + height < window.innerHeight ? below : Math.max(12, rect.top - height - 9)}px`;
  }

  function updateSelectionAction() {
    if (ownedUi(document.activeElement)) return;
    const selection = window.getSelection();
    const text = selection?.toString().trim() || "";
    if (!selection || selection.isCollapsed || selectionIsExcluded(selection) || text.length < 12 || text.length > 5000) {
      removeAction();
      return;
    }
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    if (!rect.width && !rect.height) {
      removeAction();
      return;
    }
    selectedClaim = text;
    selectionRect = rect;
    if (!actionButton?.isConnected) {
      actionButton = button("Check claim", "ttg-claim-action", () => startClaimCheck(false));
      actionButton.setAttribute(ACTION_ATTRIBUTE, "");
      actionButton.setAttribute("aria-label", "Check selected factual claim");
      document.documentElement.appendChild(actionButton);
    }
    position(actionButton, rect, 112);
  }

  function ensureCard() {
    removeAction();
    if (resultCard?.isConnected) return resultCard;
    resultCard = document.createElement("aside");
    resultCard.setAttribute(CARD_ATTRIBUTE, "");
    resultCard.setAttribute("role", "dialog");
    resultCard.setAttribute("aria-label", "Claim evidence result");
    document.documentElement.appendChild(resultCard);
    return resultCard;
  }

  function showLoading() {
    const card = ensureCard();
    card.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = "Checking public evidence…";
    const detail = document.createElement("p");
    detail.textContent = "Searching, extracting useful passages, and comparing the claim with retrieved evidence.";
    const actions = document.createElement("div");
    actions.className = "ttg-claim-actions";
    actions.appendChild(button("Cancel", "ttg-claim-secondary", () => removeCard({ cancel: true })));
    card.append(heading, detail, actions);
    position(card, selectionRect || new DOMRect(12, 80, 380, 20));
  }

  function showError(code, message) {
    const card = ensureCard();
    card.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = code === "TIMEOUT" ? "Claim check timed out" : "Claim check unavailable";
    const detail = document.createElement("p");
    detail.textContent = message || "The local backend could not complete this check.";
    const diagnostic = document.createElement("code");
    diagnostic.textContent = `Error code: ${code || "UNKNOWN_ERROR"}`;
    const actions = document.createElement("div");
    actions.className = "ttg-claim-actions";
    actions.append(button("Try again", "ttg-claim-primary", () => startClaimCheck(true)), button("Close", "ttg-claim-secondary", () => removeCard()));
    card.append(heading, detail, diagnostic, actions);
    position(card, selectionRect || new DOMRect(12, 80, 380, 20));
  }

  const VERDICT_LABELS = {
    supported: "Supported",
    contradicted: "Contradicted",
    mixed: "Mixed evidence",
    insufficient_evidence: "Insufficient evidence",
    opinion: "Opinion",
    invalid: "Invalid claim",
  };

  function copyResult(data) {
    const sources = data.evidence.map((item) => `- ${item.title}: ${item.url}`).join("\n");
    const text = `${VERDICT_LABELS[data.verdict] || data.verdict} (${data.confidence} confidence)\n${data.claim}\n${data.summary}\n${sources}\n${data.certainty_notice}`;
    navigator.clipboard.writeText(text).catch(() => console.error("[TruthTone Claim] copy failed", { code: "COPY_FAILED" }));
  }

  function showResult(data) {
    const card = ensureCard();
    card.replaceChildren();
    const header = document.createElement("div");
    header.className = "ttg-claim-header";
    const verdict = document.createElement("strong");
    verdict.className = `ttg-claim-verdict ttg-verdict-${data.verdict}`;
    verdict.textContent = VERDICT_LABELS[data.verdict] || "Claim result";
    const confidence = document.createElement("span");
    confidence.textContent = `${data.confidence} confidence`;
    header.append(verdict, confidence);
    const claim = document.createElement("p");
    claim.className = "ttg-claim-checked";
    claim.textContent = data.claim;
    const summary = document.createElement("p");
    summary.textContent = data.summary;
    card.append(header, claim, summary);

    if (data.evidence.length) {
      const evidenceHeading = document.createElement("strong");
      evidenceHeading.textContent = "Evidence sources";
      card.appendChild(evidenceHeading);
      data.evidence.forEach((item) => {
        const source = document.createElement("section");
        source.className = "ttg-claim-source";
        const title = document.createElement("strong");
        title.textContent = item.title;
        const meta = document.createElement("span");
        meta.textContent = [item.publisher, item.published_at || "Date unavailable", item.stance].join(" · ");
        const excerpt = document.createElement("p");
        excerpt.textContent = item.excerpt;
        const open = button("Open source", "ttg-claim-link", () => chrome.runtime.sendMessage({ type: "OPEN_SOURCE", url: item.url }));
        source.append(title, meta, excerpt, open);
        card.appendChild(source);
      });
    } else {
      const noEvidence = document.createElement("p");
      noEvidence.className = "ttg-claim-no-evidence";
      noEvidence.textContent = "No relevant evidence passage was available from the sources searched.";
      card.appendChild(noEvidence);
    }

    const limitationsHeading = document.createElement("strong");
    limitationsHeading.textContent = "Limitations";
    const limitations = document.createElement("ul");
    data.limitations.forEach((item) => {
      const entry = document.createElement("li");
      entry.textContent = item;
      limitations.appendChild(entry);
    });
    const notice = document.createElement("p");
    notice.className = "ttg-claim-notice";
    notice.textContent = data.certainty_notice;
    const checked = document.createElement("p");
    checked.className = "ttg-claim-meta";
    checked.textContent = `${data.cached ? "Cached result" : "Fresh result"} · Last checked ${new Date(data.checked_at).toLocaleString()}`;
    const actions = document.createElement("div");
    actions.className = "ttg-claim-actions";
    actions.append(
      button("Copy result", "ttg-claim-secondary", () => copyResult(data)),
      button("Check again", "ttg-claim-secondary", () => startClaimCheck(true)),
      button("Close", "ttg-claim-secondary", () => removeCard()),
    );
    card.append(limitationsHeading, limitations, notice, checked, actions);
    position(card, selectionRect || new DOMRect(12, 80, 380, 20), 430);
  }

  function startClaimCheck(bypassCache) {
    if (!selectedClaim) return;
    activeRequestId = crypto.randomUUID();
    const requestId = activeRequestId;
    showLoading();
    chrome.runtime.sendMessage({
      type: "CHECK_CLAIM",
      requestId,
      text: selectedClaim,
      sourceUrl: location.href,
      pageTitle: document.title,
      language: document.documentElement.lang || navigator.language || "en",
      bypassCache,
    }, (response) => {
      if (requestId !== activeRequestId) return;
      activeRequestId = null;
      if (chrome.runtime.lastError) showError("MESSAGE_FAILED", "The extension background worker did not respond.");
      else if (!response?.ok) showError(response?.code, response?.error);
      else showResult(response.data);
    });
  }

  document.addEventListener("mouseup", () => setTimeout(updateSelectionAction, 0), true);
  document.addEventListener("keyup", (event) => {
    if (event.key === "Escape") {
      removeAction();
      if (resultCard) removeCard({ cancel: true });
      return;
    }
    setTimeout(updateSelectionAction, 0);
  }, true);
  document.addEventListener("mousedown", (event) => { if (!ownedUi(event.target)) removeAction(); }, true);
  window.addEventListener("scroll", removeAction, true);
  window.addEventListener("resize", removeAction);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "GET_CLAIM_SELECTION") sendResponse({ text: window.getSelection()?.toString().trim() || "" });
  });
})();
