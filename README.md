# Truth & Tone Guardian — Milestone 2

Truth & Tone Guardian is a local Microsoft Edge and Chrome extension with two tools:

- **Tone Guard** waits while you write and offers a gentler rewrite when wording is clearly hostile or dismissive. It never replaces or sends text unless you click **Replace**.
- **Manual Truth Check** checks only text you deliberately select or paste. It retrieves limited evidence, compares the claim only with that evidence, and shows citations and limitations.

The backend must be running while the extension is in use.

## Start the backend

The simplest method is to open this project in Codex and ask:

> Start the Truth & Tone Guardian backend and keep it running.

If you prefer PowerShell, open it in this project folder and run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Keep that window open. The health page is http://127.0.0.1:8000/health.

## Configure Gemini

Gemini is already configured when `backend/.env` contains a valid `GEMINI_API_KEY`. Never put the key in extension files or Git.

For a new installation, copy `backend/.env.example` to `backend/.env`, replace `your_key_here` with your key, and restart the backend. If the key is missing or Gemini fails, Tone Guard uses its local fallback. Truth Check remains available but returns **insufficient evidence** when evidence cannot be safely verified.

## Search and extraction options

Milestone 2 uses interchangeable providers:

- **SearXNG search:** optional. Run your own SearXNG instance, then set `SEARXNG_URL` in `backend/.env`, for example `SEARXNG_URL=http://127.0.0.1:8080`. The backend starts safely without it.
- **Crawl4AI extraction:** optional. After installing it inside this project's virtual environment and completing its browser setup, set `TRUTH_EXTRACTION_PROVIDER=crawl4ai`. It is not needed for the controlled development flow.
- **Development mode:** when SearXNG is not configured, controlled mock search and extraction keep the complete interface testable without pretending that mock evidence is live research.

To see the active providers, open http://127.0.0.1:8000/api/truth/status.

## Load or reload the extension

The extension folder is:

```text
C:\Users\DELL\Documents\Codex\2026-08-01\referenced-chatgpt-conversation-this-is-an\truth-tone-guardian\extension
```

In Microsoft Edge:

1. Open `edge://extensions`.
2. Turn on **Developer mode**.
3. If this extension is already loaded, click **Reload** on its card.
4. Otherwise click **Load unpacked** and select the extension folder above.
5. Refresh any webpage that was already open.

Chrome uses the same steps at `chrome://extensions`.

## Perform a manual claim check

1. Select a factual sentence on a normal webpage.
2. Click the small **Check claim** button beside the selection.
3. Wait for the evidence card.
4. Read the verdict, sources, excerpts, limitations, and last-checked time.
5. Use **Open source**, **Copy result**, **Check again**, or **Close**.

Nothing is checked automatically. You can also paste a claim into the extension popup as a manual fallback.

## Understand the verdicts

- **Supported:** the retrieved evidence supports the main claim.
- **Contradicted:** the retrieved evidence contradicts the main claim.
- **Mixed evidence:** relevant sources do not agree or support only part of the claim.
- **Insufficient evidence:** the limited search did not produce enough verifiable evidence. This does not mean the claim is false.
- **Opinion:** the selected text is mainly a viewpoint rather than a checkable factual claim.
- **Invalid claim:** the selection is empty, too short, a question, or otherwise unsuitable.

Confidence is shown as high, medium, or low. It describes the evidence comparison, not a percentage of truth. Verdicts reflect the evidence found from the sources searched, not absolute certainty.

## Tone Guard settings

Open the extension popup to turn **Automatic Tone Guard** On or Off. Sensitivity options are:

- **Low:** clear insults and personal attacks only.
- **Balanced:** insults, hostile wording, and strong dismissiveness. This is the default.
- **High:** also warns about milder aggressive or dismissive wording.

## Clear the local claim cache

The cache avoids repeating the same research. To clear it, open this project in Codex and ask:

> Clear the Truth & Tone Guardian claim cache.

For testing, **Check again** bypasses the cached result for that claim.

## Privacy

- Claim checking sends only the selected claim and basic page context to the local backend.
- When live search is configured, the claim is sent to that search provider.
- Extracted public evidence and the claim are sent to Gemini for comparison when Gemini is configured.
- Tone analysis sends checked text to Google's Gemini API when Gemini is configured.
- The claim cache stores the normalized claim, verdict, evidence metadata, provider details, and timestamps. It does not store the surrounding webpage.
- Tone-check text is not stored. Full messages and API keys are not logged.

## Troubleshooting

- **Backend unavailable:** ask Codex to start it, then confirm http://127.0.0.1:8000/health says `ok`.
- **No Check claim button:** refresh the webpage after reloading the extension, select a complete factual sentence, and avoid selecting text inside an editor.
- **Development mock shown:** SearXNG is not configured. The interface works, but the sources are controlled test evidence rather than live search results.
- **Search failure:** check `/api/truth/status` and confirm the configured SearXNG service is running.
- **Extraction failure:** a source may block automated access or return an unsupported file type. The result should explain that evidence was insufficient.
- **Gemini failure:** check that `backend/.env` contains a valid key and restart the backend. Truth Check will not fabricate a verdict when verification fails.
- **Old popup or content script:** reload the extension and refresh the webpage.

## Current limitations

- The backend and any configured SearXNG service must remain running locally.
- The default development provider uses controlled mock evidence, not live web search.
- Only the first five search results and at most three evidence sources are considered.
- Paywalled, login-only, heavily scripted, or scraping-resistant pages may not be extractable.
- The verifier checks one main claim from a selection and may omit secondary claims.
- Search results can be incomplete, outdated, duplicated, or misleading; citations should be opened and reviewed.
- Browser-internal pages and protected extension pages do not allow content scripts.
