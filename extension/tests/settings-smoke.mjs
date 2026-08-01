const stored = {};
globalThis.chrome = {
  storage: {
    local: {
      async get(defaults) { return { ...defaults, ...stored }; },
      async set(values) { Object.assign(stored, values); },
    },
  },
};

await import("../settings.js");

const defaults = await TruthToneSettings.read();
if (!defaults.automaticToneGuard || defaults.toneSensitivity !== "balanced") {
  throw new Error(`Unexpected defaults: ${JSON.stringify(defaults)}`);
}

await TruthToneSettings.save({ automaticToneGuard: false, toneSensitivity: "high" });
const saved = await TruthToneSettings.read();
if (saved.automaticToneGuard || saved.toneSensitivity !== "high") {
  throw new Error(`Settings did not persist: ${JSON.stringify(saved)}`);
}

const normalized = TruthToneSettings.normalize({ automaticToneGuard: true, toneSensitivity: "invalid" });
if (normalized.toneSensitivity !== "balanced") throw new Error("Invalid sensitivity was not normalized.");
if (TruthToneSettings.shouldAnalyze({ automaticToneGuard: false }, 40)) throw new Error("Automatic checks did not stop when disabled.");
if (!TruthToneSettings.shouldAnalyze({ automaticToneGuard: true }, 40)) throw new Error("Automatic checks did not resume when enabled.");
if (TruthToneSettings.shouldAnalyze({ automaticToneGuard: true }, 7)) throw new Error("Short text was not excluded.");
if (TruthToneSettings.shouldAnalyze({ automaticToneGuard: true }, 40, true)) throw new Error("Sensitive text was not excluded.");

console.log(JSON.stringify({ ok: true, defaults, saved, automaticOffStopsChecks: true, automaticOnRestoresChecks: true }));
