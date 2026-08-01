(() => {
  "use strict";

  const DEFAULTS = Object.freeze({
    automaticToneGuard: true,
    toneSensitivity: "balanced",
  });
  const SENSITIVITIES = Object.freeze(["low", "balanced", "high"]);

  function normalize(values = {}) {
    return {
      automaticToneGuard: values.automaticToneGuard !== false,
      toneSensitivity: SENSITIVITIES.includes(values.toneSensitivity)
        ? values.toneSensitivity
        : DEFAULTS.toneSensitivity,
    };
  }

  async function read() {
    return normalize(await chrome.storage.local.get(DEFAULTS));
  }

  async function save(values) {
    const settings = normalize(values);
    await chrome.storage.local.set(settings);
    return settings;
  }

  function shouldAnalyze(settings, textLength, excluded = false) {
    const normalized = normalize(settings);
    return normalized.automaticToneGuard && !excluded && Number.isFinite(textLength) && textLength >= 8;
  }

  globalThis.TruthToneSettings = Object.freeze({ DEFAULTS, SENSITIVITIES, normalize, read, save, shouldAnalyze });
})();
