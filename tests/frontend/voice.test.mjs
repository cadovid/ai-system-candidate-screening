import assert from "node:assert/strict";
import test from "node:test";

import { VoiceController, VoiceState, chooseVoice, recognitionLanguage } from "../../static/voice.mjs";

class FakeRecognition {
  static latest = null;

  constructor() {
    FakeRecognition.latest = this;
  }

  start() { this.onstart?.(); }
  stop() { this.onspeechend?.(); }
  abort() { this.onerror?.({ error: "aborted" }); }
}

function controller(overrides = {}) {
  const states = [];
  const interim = [];
  const final = [];
  const errors = [];
  const instance = new VoiceController({
    Recognition: FakeRecognition,
    synthesis: null,
    Utterance: null,
    getLanguage: () => "es",
    onState: (state) => states.push(state),
    onInterim: (text) => interim.push(text),
    onFinal: (text) => final.push(text),
    onError: (code) => errors.push(code),
    ...overrides,
  });
  return { instance, states, interim, final, errors };
}

test("language and voice helpers prefer the active conversation language", () => {
  assert.equal(recognitionLanguage("es"), "es-ES");
  assert.equal(recognitionLanguage("en"), "en-US");
  const voices = [{ lang: "en-US", name: "English" }, { lang: "es-MX", name: "Español" }];
  assert.equal(chooseVoice(voices, "es"), voices[1]);
  assert.equal(chooseVoice([], "en"), null);
  assert.equal(chooseVoice([{ lang: null }], "en"), null);
});

test("unsupported recognition remains a text-only capability", () => {
  const { instance, states } = controller({ Recognition: null });
  assert.equal(instance.supported, false);
  assert.equal(instance.start(), false);
  assert.deepEqual(states, [VoiceState.UNSUPPORTED]);
});

test("interim speech is shown but only a final result creates a draft", () => {
  const { instance, states, interim, final } = controller();
  assert.equal(instance.start(), true);
  const recognition = FakeRecognition.latest;
  assert.equal(recognition.lang, "es-ES");
  recognition.onresult({ results: [Object.assign([{ transcript: "tengo licencia" }], { isFinal: false })] });
  assert.equal(interim.at(-1), "tengo licencia");
  assert.deepEqual(final, []);
  recognition.onresult({ results: [Object.assign([{ transcript: "Tengo licencia vigente" }], { isFinal: true })] });
  assert.equal(final.at(-1), "Tengo licencia vigente");
  assert.equal(states.at(-1), VoiceState.DRAFT_READY);
});

test("recognition ignores duplicate starts and retains one finalized draft", () => {
  const { instance, final } = controller();
  assert.equal(instance.start(), true);
  const recognition = FakeRecognition.latest;
  assert.equal(instance.start(), false);
  recognition.onresult({ results: [Object.assign([{ transcript: "uno" }], { isFinal: true })] });
  recognition.onresult({ results: [
    Object.assign([{ transcript: "uno" }], { isFinal: true }),
    Object.assign([{ transcript: "dos" }], { isFinal: false }),
  ] });
  assert.deepEqual(final, ["uno"]);
  assert.equal(instance.stop(), false);
});

test("permission and no-speech failures return an actionable error state", () => {
  const permission = controller();
  permission.instance.start();
  FakeRecognition.latest.onerror({ error: "not-allowed" });
  assert.deepEqual(permission.errors, ["not-allowed"]);
  assert.equal(permission.states.at(-1), VoiceState.ERROR);

  const silence = controller();
  silence.instance.start();
  FakeRecognition.latest.onend();
  assert.deepEqual(silence.errors, ["no_speech"]);
});

test("speech synthesis uses a matching voice and microphone start cancels playback", () => {
  const spoken = [];
  const synthesis = {
    speaking: false,
    cancelled: 0,
    getVoices: () => [{ lang: "es-ES", name: "Spanish" }],
    speak: (utterance) => { spoken.push(utterance); utterance.onstart(); },
    cancel() { this.cancelled += 1; this.speaking = false; },
  };
  class Utterance {
    constructor(text) { this.text = text; }
  }
  const { instance, states } = controller({ synthesis, Utterance });
  assert.equal(instance.speak("Hola", "es"), true);
  assert.equal(spoken[0].text, "Hola");
  assert.equal(spoken[0].voice.name, "Spanish");
  assert.equal(states.at(-1), VoiceState.SPEAKING);
  instance.start();
  assert.equal(synthesis.cancelled, 1);
  assert.equal(states.at(-1), VoiceState.LISTENING);
});

test("speech state is visible before asynchronous playback and stale errors are ignored", () => {
  let utterance = null;
  const synthesis = {
    speaking: false,
    speak(value) { utterance = value; },
    cancel() { this.speaking = false; },
  };
  class Utterance {
    constructor(text) { this.text = text; }
  }
  const { instance, states, errors } = controller({ synthesis, Utterance });
  assert.equal(instance.speak("Hola", "es"), true);
  assert.equal(states.at(-1), VoiceState.SPEAKING);
  instance.cancelSpeech();
  assert.equal(states.at(-1), VoiceState.IDLE);
  utterance.onerror?.();
  assert.deepEqual(errors, []);
});
