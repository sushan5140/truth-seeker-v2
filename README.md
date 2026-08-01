# Truth & Tone Guardian — Milestone 1

Truth & Tone Guardian is a local Microsoft Edge and Chrome extension with:

- A basic manual Claim Checker. It labels opinions and otherwise returns **unverified**. Unverified does not mean false.
- Automatic Tone Guard for supported writing fields on webpages.
- A manual Tone Guard inside the popup.
- Gemini as the primary tone analyzer, with fast local deterministic rules as a fallback.

Tone Guard never replaces or submits text automatically. The floating card appears only when an intervention is recommended. **Replace** changes the editor text, **Ignore** leaves it unchanged, and **Dismiss** closes the card.

## Start the backend

The backend must be running while the extension is in use.

The easiest method is to open this project in Codex and ask:

> Start the Truth & Tone Guardian backend and keep it running.

To start it from PowerShell instead, open PowerShell in this project folder and run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Keep that PowerShell window open. The health page is http://127.0.0.1:8000/health.

## Load or reload the extension

The extension folder is:

```text
C:\Users\DELL\Documents\Codex\2026-08-01\referenced-chatgpt-conversation-this-is-an\truth-tone-guardian\extension
```

### Microsoft Edge

1. Open `edge://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked**.
4. Select the extension folder shown above.

### Google Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked**.
4. Select the extension folder shown above.

After project updates, click **Reload** on the extension card. Open ChatGPT tabs are refreshed automatically so they receive the new content script.

## Automatic Tone Guard setting

1. Click the Truth & Tone Guardian extension icon.
2. Find **Automatic Tone Guard**.
3. Choose **On** or **Off**.

The default is **On**. When it is Off, webpage editors are not checked automatically. The manual popup Tone Guard remains available.

## Sensitivity setting

Choose a sensitivity in the extension popup:

- **Low:** Only clear insults and personal attacks.
- **Balanced:** Insults, hostile commands, and strong dismissiveness. This is the default.
- **High:** Also warns about milder aggressive or dismissive phrasing.

The setting applies to both Gemini and the local fallback. Normal disagreement and neutral criticism should remain silent at every level.

## Test Tone Guard

1. Make sure the backend is running.
2. Open a normal webpage with a textarea or use ChatGPT's message composer.
3. Type `This idea is stupid and completely useless.`
4. Pause for about one second.
5. A floating suggestion card should appear. Nothing is sent or replaced automatically.

Use **Test backend connection** in the popup to verify the background worker, both local health addresses, the tone endpoint, and the active provider.

## Privacy

When Gemini is configured, the text being checked is sent to Google's Gemini API for tone analysis. Password fields, disabled fields, readonly fields, extension-owned fields, and very short text are excluded.

The Gemini API key exists only in `backend/.env`. It is not stored in the extension. The backend does not store submitted messages or log their full text.

## Current limitations

- The backend must remain running locally.
- Claim checking does not search the web or verify factual claims against evidence.
- Gemini requires an internet connection and valid quota; failures use the local fallback.
- The local fallback understands a limited set of deterministic phrases and cannot match Gemini's nuance.
- Browser-internal pages, password fields, and some protected editors cannot be analyzed.
- A website can change its editor implementation and temporarily require an extension update.
- Milestone 2 evidence retrieval and web scraping are not included.
