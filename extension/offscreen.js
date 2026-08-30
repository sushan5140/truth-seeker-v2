// Offscreen document: the only MV3 context that can hold a live MediaStream
// and drive SpeechRecognition against it. background.js (a service worker)
// cannot do either directly -- this file exists purely to bridge that gap.
// Reached only via chrome.runtime.sendMessage({..., target: "offscreen"}).

let activeStream = null;
let audioContext = null;
let recognition = null;
let stopRequested = false;

// Temporary diagnostic instrumentation for the ~30s auto-stop bug --
// every SpeechRecognition lifecycle event is logged with a timestamp so
// the offscreen console shows exactly what fired and when.
const LOG_PREFIX = "[LiveCheck offscreen]";
function log(...args) {
  console.log(LOG_PREFIX, `t+${Math.round(performance.now())}ms`, ...args);
}
function logError(...args) {
  console.error(LOG_PREFIX, `t+${Math.round(performance.now())}ms`, ...args);
}

// SpeechRecognition.start(audioTrack) accepts an arbitrary MediaStreamTrack
// instead of the microphone (Web Speech API spec, shipped in Chromium since
// mid-2024) -- this is what makes transcribing *tab* audio possible at all
// without ever touching the user's microphone.
const SpeechRecognitionCtor = self.SpeechRecognition || self.webkitSpeechRecognition;

const ERROR_MESSAGES = {
  "not-allowed": "Speech recognition was blocked by the browser.",
  "service-not-allowed": "The browser's speech recognition service is unavailable.",
  "audio-capture": "The captured tab audio could not be read.",
  network: "Speech recognition lost its network connection.",
  aborted: "Speech recognition was stopped.",
};

function send(message) {
  chrome.runtime.sendMessage(message).catch(() => {
    // No popup is listening right now -- listening continues in the
    // background regardless, this just means nothing shows it live.
  });
}

function teardown(reason) {
  log("teardown()", { reason, stopRequestedBefore: stopRequested });
  stopRequested = true;
  try {
    recognition?.stop();
  } catch (error) {
    log("teardown: recognition.stop() threw (likely already stopped)", error?.message);
  }
  recognition = null;
  activeStream?.getTracks().forEach((track) => track.stop());
  activeStream = null;
  if (audioContext && audioContext.state !== "closed") {
    audioContext.close().catch(() => {});
  }
  audioContext = null;
}

async function startCapture(streamId) {
  stopRequested = false;
  log("startCapture() called", { streamId });

  if (!SpeechRecognitionCtor) {
    logError("SpeechRecognition constructor unavailable on this browser");
    send({ type: "LIVE_CHECK_ERROR", code: "SPEECH_API_UNSUPPORTED", message: "This browser does not support live speech recognition." });
    return;
  }

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    });
    log("getUserMedia succeeded", { audioTracks: stream.getAudioTracks().length });
  } catch (error) {
    logError("getUserMedia failed", error?.name, error?.message);
    send({ type: "LIVE_CHECK_ERROR", code: "CAPTURE_FAILED", message: "Could not capture this tab's audio. Reload the tab and try again." });
    return;
  }
  activeStream = stream;

  const audioTrack = stream.getAudioTracks()[0];
  if (!audioTrack) {
    logError("No audio track on captured stream");
    send({ type: "LIVE_CHECK_ERROR", code: "NO_AUDIO_TRACK", message: "This tab has no audio to capture." });
    teardown("no-audio-track");
    return;
  }
  log("audio track obtained", { readyState: audioTrack.readyState, label: audioTrack.label });

  // Capturing tab audio otherwise silences it for the user -- route the
  // same stream back out to the speakers so playback is unaffected while
  // Live Check is listening.
  audioContext = new AudioContext();
  const source = audioContext.createMediaStreamSource(stream);
  source.connect(audioContext.destination);

  let finalTranscript = "";

  recognition = new SpeechRecognitionCtor();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = navigator.language || "en-US";

  recognition.onresult = (event) => {
    let interim = "";
    let finalCount = 0;
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i];
      if (result.isFinal) { finalTranscript += `${result[0].transcript} `; finalCount += 1; }
      else interim += result[0].transcript;
    }
    log("onresult", { resultIndex: event.resultIndex, resultCount: event.results.length, finalCount, interim, finalTranscriptLength: finalTranscript.length });
    send({ type: "LIVE_CHECK_TRANSCRIPT_UPDATE", transcript: (finalTranscript + interim).trim() });
  };

  recognition.onerror = (event) => {
    // "no-speech" is routine (nothing audible right now, e.g. the tab is
    // momentarily quiet) -- not a failure worth surfacing as an error;
    // recognition.onend's auto-restart below keeps listening through it.
    if (event.error === "no-speech") {
      log("onerror: no-speech (ignored, not surfaced as an error)");
      return;
    }
    logError("onerror", { error: event.error, message: event.message });
    send({
      type: "LIVE_CHECK_ERROR",
      code: `SPEECH_${String(event.error || "unknown").toUpperCase().replace(/-/g, "_")}`,
      message: ERROR_MESSAGES[event.error] || "Speech recognition hit an unexpected error.",
    });
  };

  recognition.onend = () => {
    // Chrome's SpeechRecognition stops itself periodically (silence,
    // internal timeouts) even with continuous:true. If the user didn't
    // ask to stop and the track is still live, restart transparently so
    // the transcript keeps updating instead of silently going stale.
    log("onend fired", { stopRequested, hasActiveStream: Boolean(activeStream), trackReadyState: audioTrack.readyState });
    if (stopRequested || !activeStream || audioTrack.readyState !== "live") {
      log("onend: not restarting", { stopRequested, hasActiveStream: Boolean(activeStream), trackReadyState: audioTrack.readyState });
      return;
    }
    try {
      recognition.start(audioTrack);
      log("onend: restart attempted successfully (recognition.start did not throw)");
    } catch (error) {
      logError("onend: restart attempt threw", error?.name, error?.message);
      send({ type: "LIVE_CHECK_ERROR", code: "SPEECH_RESTART_FAILED", message: "Speech recognition stopped and could not restart." });
    }
  };

  try {
    recognition.start(audioTrack);
    log("initial recognition.start(audioTrack) did not throw");
  } catch (error) {
    logError("initial recognition.start(audioTrack) threw", error?.name, error?.message);
    send({ type: "LIVE_CHECK_ERROR", code: "SPEECH_START_FAILED", message: "Speech recognition could not start." });
    teardown("initial-start-failed");
    return;
  }

  send({ type: "LIVE_CHECK_STARTED" });
}

chrome.runtime.onMessage.addListener((message) => {
  if (message?.target !== "offscreen") return false;
  if (message.type === "LIVE_CHECK_OFFSCREEN_START") {
    log("received LIVE_CHECK_OFFSCREEN_START");
    startCapture(message.streamId);
    return false;
  }
  if (message.type === "LIVE_CHECK_OFFSCREEN_STOP") {
    log("received LIVE_CHECK_OFFSCREEN_STOP");
    teardown("offscreen-stop-message");
    send({ type: "LIVE_CHECK_ENDED" });
    return false;
  }
  return false;
});
