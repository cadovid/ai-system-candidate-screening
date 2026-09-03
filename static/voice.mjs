export const VoiceState = Object.freeze({
  UNSUPPORTED: "unsupported",
  IDLE: "idle",
  REQUESTING_PERMISSION: "requesting_permission",
  LISTENING: "listening",
  TRANSCRIBING: "transcribing",
  DRAFT_READY: "draft_ready",
  PROCESSING: "processing",
  SPEAKING: "speaking",
  ERROR: "error",
});

export function recognitionLanguage(language) {
  return language === "en" ? "en-US" : "es-ES";
}

export function chooseVoice(voices, language) {
  const prefix = language === "en" ? "en-" : "es-";
  return (Array.isArray(voices) ? voices : []).find((voice) => {
    const voiceLanguage = typeof voice?.lang === "string" ? voice.lang.toLowerCase() : "";
    return voiceLanguage.startsWith(prefix);
  }) || null;
}

export class VoiceController {
  constructor({
    Recognition,
    synthesis,
    Utterance,
    getLanguage,
    onState = () => {},
    onInterim = () => {},
    onFinal = () => {},
    onError = () => {},
  }) {
    this.Recognition = Recognition;
    this.synthesis = synthesis;
    this.Utterance = Utterance;
    this.getLanguage = typeof getLanguage === "function" ? getLanguage : () => "es";
    this.onState = onState;
    this.onInterim = onInterim;
    this.onFinal = onFinal;
    this.onError = onError;
    this.recognition = null;
    this.activeUtterance = null;
    this.aborted = false;
    this.receivedFinal = false;
    this.finalTranscript = "";
    this.state = Recognition ? VoiceState.IDLE : VoiceState.UNSUPPORTED;
    this.onState(this.state);
  }

  get supported() {
    return Boolean(this.Recognition);
  }

  transition(state) {
    this.state = state;
    this.onState(state);
  }

  start() {
    if (!this.supported || this.recognition || [
      VoiceState.PROCESSING,
      VoiceState.REQUESTING_PERMISSION,
      VoiceState.LISTENING,
      VoiceState.TRANSCRIBING,
    ].includes(this.state)) {
      return false;
    }
    this.cancelSpeech();
    this.aborted = false;
    this.receivedFinal = false;
    this.finalTranscript = "";
    this.onInterim("");
    let recognition;
    try {
      recognition = new this.Recognition();
    } catch (_error) {
      this.fail("recognition_failed");
      return false;
    }
    this.recognition = recognition;
    const isCurrent = () => this.recognition === recognition;
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    recognition.lang = recognitionLanguage(this.getLanguage());
    recognition.onstart = () => {
      if (isCurrent() && this.state === VoiceState.REQUESTING_PERMISSION) {
        this.transition(VoiceState.LISTENING);
      }
    };
    recognition.onspeechend = () => {
      if (isCurrent() && this.state === VoiceState.LISTENING) {
        this.transition(VoiceState.TRANSCRIBING);
      }
    };
    recognition.onresult = (event) => {
      if (!isCurrent()) return;
      const finalParts = [];
      const interimParts = [];
      for (let index = 0; index < event.results.length; index += 1) {
        const result = event.results[index];
        const transcript = String(result[0]?.transcript || "").trim();
        if (!transcript) continue;
        (result.isFinal ? finalParts : interimParts).push(transcript);
      }
      this.onInterim(interimParts.join(" "));
      const finalTranscript = finalParts.join(" ").trim();
      if (finalTranscript && finalTranscript !== this.finalTranscript) {
        this.finalTranscript = finalTranscript;
        this.receivedFinal = true;
        this.onFinal(finalTranscript);
        this.transition(VoiceState.DRAFT_READY);
      }
    };
    recognition.onnomatch = () => {
      if (!isCurrent()) return;
      this.recognition = null;
      this.fail("no_match");
    };
    recognition.onerror = (event) => {
      if (!isCurrent()) return;
      const code = event?.error;
      if (this.aborted || code === "aborted") {
        this.recognition = null;
        this.onInterim("");
        this.transition(VoiceState.IDLE);
        return;
      }
      this.recognition = null;
      this.fail(code || "recognition_failed");
    };
    recognition.onend = () => {
      if (!isCurrent()) return;
      this.recognition = null;
      if (!this.aborted && !this.receivedFinal && this.state !== VoiceState.ERROR) {
        this.fail("no_speech");
      }
    };
    this.transition(VoiceState.REQUESTING_PERMISSION);
    try {
      recognition.start();
      return true;
    } catch (_error) {
      this.recognition = null;
      this.fail("recognition_failed");
      return false;
    }
  }

  stop() {
    if (!this.recognition || ![
      VoiceState.REQUESTING_PERMISSION,
      VoiceState.LISTENING,
    ].includes(this.state)) return false;
    this.transition(VoiceState.TRANSCRIBING);
    try {
      this.recognition.stop();
      return true;
    } catch (_error) {
      this.recognition = null;
      this.fail("recognition_failed");
      return false;
    }
  }

  abort() {
    const recognition = this.recognition;
    this.recognition = null;
    this.aborted = true;
    if (recognition) {
      try {
        recognition.abort();
      } catch (_error) {
        // A recognition session can already have ended between an event and
        // this cancellation. It is safe to return to the idle state.
      }
    }
    this.onInterim("");
    this.transition(this.supported ? VoiceState.IDLE : VoiceState.UNSUPPORTED);
  }

  setProcessing(processing) {
    if (processing) {
      this.cancelSpeech();
      this.abort();
      this.transition(VoiceState.PROCESSING);
    } else if (this.state === VoiceState.PROCESSING) {
      this.transition(this.supported ? VoiceState.IDLE : VoiceState.UNSUPPORTED);
    }
  }

  speak(text, language) {
    if (
      !this.synthesis ||
      typeof this.synthesis.speak !== "function" ||
      !this.Utterance ||
      !text?.trim()
    ) return false;
    this.cancelSpeech();
    let utterance;
    try {
      utterance = new this.Utterance(text);
      utterance.lang = recognitionLanguage(language);
      const availableVoices = typeof this.synthesis.getVoices === "function"
        ? this.synthesis.getVoices()
        : [];
      const voice = chooseVoice(availableVoices, language);
      if (voice) utterance.voice = voice;
    } catch (_error) {
      this.fail("speech_failed");
      return false;
    }
    const token = {};
    let playbackFailed = false;
    this.activeUtterance = token;
    const isCurrent = () => this.activeUtterance === token;
    utterance.onstart = () => {
      if (isCurrent() && this.state !== VoiceState.SPEAKING) {
        this.transition(VoiceState.SPEAKING);
      }
    };
    utterance.onend = () => {
      if (!isCurrent()) return;
      this.activeUtterance = null;
      this.transition(VoiceState.IDLE);
    };
    utterance.onerror = () => {
      if (!isCurrent()) return;
      playbackFailed = true;
      this.activeUtterance = null;
      this.fail("speech_failed");
    };
    // Set the state before speak() because browsers dispatch onstart
    // asynchronously. This keeps the stop-audio affordance visible for the
    // entire playback lifecycle and prevents processing cleanup from hiding it.
    this.transition(VoiceState.SPEAKING);
    try {
      this.synthesis.speak(utterance);
      return !playbackFailed;
    } catch (_error) {
      if (isCurrent()) {
        this.activeUtterance = null;
        this.fail("speech_failed");
      }
      return false;
    }
  }

  cancelSpeech() {
    const hasActiveSpeech = this.activeUtterance !== null || this.state === VoiceState.SPEAKING;
    this.activeUtterance = null;
    if (this.synthesis?.speaking || hasActiveSpeech) {
      try {
        this.synthesis?.cancel?.();
      } catch (_error) {
        // Cancellation is best effort; the written response remains usable.
      }
    }
    if (this.state === VoiceState.SPEAKING) this.transition(VoiceState.IDLE);
  }

  fail(code) {
    this.onInterim("");
    this.onError(code || "recognition_failed");
    this.transition(VoiceState.ERROR);
  }
}
