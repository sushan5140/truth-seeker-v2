// Only background.js's orchestration (session guard, offscreen-document
// lifecycle, message relay shape) is exercised here -- real tabCapture,
// getUserMedia, and SpeechRecognition cannot run outside a real browser
// with a real tab and real audio, so offscreen.js's actual capture/
// transcription logic is not covered by this or any automated test.

let messageListener;
let createDocumentCalls = [];
let closeDocumentCalls = 0;
let getMediaStreamIdCalls = [];
let sentMessages = [];
let offscreenDocOpen = false;
let nextStreamId = "stream-1";
let getMediaStreamIdShouldReject = null;

globalThis.chrome = {
  runtime: {
    getManifest: () => ({ version: "2.0.0" }),
    onInstalled: { addListener() {} },
    onMessage: { addListener(listener) { messageListener = listener; } },
    getContexts: async () => (offscreenDocOpen ? [{ contextType: "OFFSCREEN_DOCUMENT" }] : []),
    sendMessage: async (message) => {
      sentMessages.push(message);
      return { ok: true };
    },
  },
  storage: { local: { get: async () => ({}), set: async () => {} } },
  tabs: { query: async () => [], reload: async () => {} },
  offscreen: {
    createDocument: async (options) => {
      createDocumentCalls.push(options);
      offscreenDocOpen = true;
    },
    closeDocument: async () => {
      closeDocumentCalls += 1;
      offscreenDocOpen = false;
    },
  },
  tabCapture: {
    getMediaStreamId: async (options) => {
      getMediaStreamIdCalls.push(options);
      if (getMediaStreamIdShouldReject) throw new Error(getMediaStreamIdShouldReject);
      return nextStreamId;
    },
  },
};

await import("../background.js");

function send(message) {
  return new Promise((resolve, reject) => {
    messageListener(message, {}, resolve);
    setTimeout(() => reject(new Error(`Timed out: ${message.type}`)), 2_000);
  });
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 10));

// --- idle state ------------------------------------------------------------

const idleState = await send({ type: "LIVE_CHECK_GET_STATE" });
if (idleState.listening !== false) throw new Error("Expected idle state before any LIVE_CHECK_START.");

// --- missing tab id ----------------------------------------------------------

const noTab = await send({ type: "LIVE_CHECK_START" });
if (noTab.ok || noTab.code !== "INVALID_LIVE_CHECK_TAB") throw new Error("Missing tabId was not rejected.");
if (getMediaStreamIdCalls.length !== 0) throw new Error("tabCapture should not be called without a valid tabId.");

// --- happy path: start ------------------------------------------------------

const started = await send({ type: "LIVE_CHECK_START", tabId: 7 });
if (!started.ok) throw new Error(`Expected LIVE_CHECK_START to succeed, got ${JSON.stringify(started)}`);
if (createDocumentCalls.length !== 1) throw new Error("Offscreen document was not created exactly once.");
if (createDocumentCalls[0].url !== "offscreen.html" || !createDocumentCalls[0].reasons.includes("USER_MEDIA")) {
  throw new Error("createDocument called with unexpected options.");
}
if (getMediaStreamIdCalls.length !== 1 || getMediaStreamIdCalls[0].targetTabId !== 7) {
  throw new Error("getMediaStreamId was not called with the expected tabId.");
}
const relayStart = sentMessages.find((m) => m.type === "LIVE_CHECK_OFFSCREEN_START");
if (!relayStart || relayStart.target !== "offscreen" || relayStart.streamId !== nextStreamId) {
  throw new Error("Start was not relayed to the offscreen document with the expected shape.");
}

const stateWhileListening = await send({ type: "LIVE_CHECK_GET_STATE" });
if (stateWhileListening.listening !== true) throw new Error("Expected listening:true after a successful start.");

// --- double-start guard ------------------------------------------------------

const secondStart = await send({ type: "LIVE_CHECK_START", tabId: 7 });
if (secondStart.ok || secondStart.code !== "LIVE_CHECK_ALREADY_ACTIVE") throw new Error("A second concurrent start was not blocked.");
if (getMediaStreamIdCalls.length !== 1) throw new Error("getMediaStreamId should not be called again while a session is active.");

// --- stop --------------------------------------------------------------------

const stopped = await send({ type: "LIVE_CHECK_STOP" });
if (!stopped.ok) throw new Error("LIVE_CHECK_STOP did not respond ok:true.");
const relayStop = sentMessages.find((m) => m.type === "LIVE_CHECK_OFFSCREEN_STOP");
if (!relayStop || relayStop.target !== "offscreen") throw new Error("Stop was not relayed to the offscreen document.");

const stateAfterStop = await send({ type: "LIVE_CHECK_GET_STATE" });
if (stateAfterStop.listening !== false) throw new Error("Expected listening:false after LIVE_CHECK_STOP.");

// --- guard clears correctly: a new session can start after stopping ---------

sentMessages = [];
createDocumentCalls = [];
getMediaStreamIdCalls = [];
nextStreamId = "stream-2";
const restarted = await send({ type: "LIVE_CHECK_START", tabId: 9 });
if (!restarted.ok) throw new Error("Could not start a new session after a clean stop.");
if (getMediaStreamIdCalls.length !== 1) throw new Error("Restart after stop did not call getMediaStreamId.");

// --- offscreen-originated LIVE_CHECK_ERROR clears session + closes doc ------

messageListener({ type: "LIVE_CHECK_ERROR", code: "SPEECH_API_UNSUPPORTED", message: "unsupported" }, {}, () => {});
await flush();
const stateAfterError = await send({ type: "LIVE_CHECK_GET_STATE" });
if (stateAfterError.listening !== false) throw new Error("LIVE_CHECK_ERROR did not clear the session guard.");
if (closeDocumentCalls !== 1) throw new Error("LIVE_CHECK_ERROR did not close the offscreen document.");

// A fresh start is possible again after the error cleared the guard.
const afterErrorRestart = await send({ type: "LIVE_CHECK_START", tabId: 9 });
if (!afterErrorRestart.ok) throw new Error("Could not restart after LIVE_CHECK_ERROR cleared the guard.");

// --- offscreen-originated LIVE_CHECK_ENDED also clears session + closes doc --

closeDocumentCalls = 0;
messageListener({ type: "LIVE_CHECK_ENDED" }, {}, () => {});
await flush();
const stateAfterEnded = await send({ type: "LIVE_CHECK_GET_STATE" });
if (stateAfterEnded.listening !== false) throw new Error("LIVE_CHECK_ENDED did not clear the session guard.");
if (closeDocumentCalls !== 1) throw new Error("LIVE_CHECK_ENDED did not close the offscreen document.");

// --- getMediaStreamId rejecting is surfaced and does not leave the guard stuck --

getMediaStreamIdShouldReject = "Extension has not been invoked for the current page.";
const rejectedStart = await send({ type: "LIVE_CHECK_START", tabId: 11 });
if (rejectedStart.ok || rejectedStart.code !== "LIVE_CHECK_START_FAILED") throw new Error("A rejected getMediaStreamId call was not surfaced as a failure.");
if (!rejectedStart.error.includes("has not been invoked")) throw new Error("Failure message did not propagate from getMediaStreamId's rejection.");
const stateAfterRejectedStart = await send({ type: "LIVE_CHECK_GET_STATE" });
if (stateAfterRejectedStart.listening !== false) throw new Error("A failed start left the session guard stuck.");
getMediaStreamIdShouldReject = null;

// --- capability check: missing tabCapture/offscreen APIs is a clear, distinct failure --

delete chrome.tabCapture.getMediaStreamId;
const unsupported = await send({ type: "LIVE_CHECK_START", tabId: 12 });
if (unsupported.ok || unsupported.code !== "LIVE_CHECK_UNSUPPORTED") throw new Error("Missing tabCapture API was not reported as LIVE_CHECK_UNSUPPORTED.");

console.log(JSON.stringify({
  ok: true,
  idleStateCorrect: true,
  missingTabIdRejected: true,
  startRelaysToOffscreen: true,
  doubleStartGuarded: true,
  stopRelaysAndClearsGuard: true,
  restartAfterStopWorks: true,
  offscreenErrorClearsGuardAndClosesDoc: true,
  offscreenEndedClearsGuardAndClosesDoc: true,
  getMediaStreamIdRejectionSurfaced: true,
  guardNotStuckAfterFailure: true,
  unsupportedCapabilityDetected: true,
}));
