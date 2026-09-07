import { VoiceController, VoiceState } from "./voice.mjs";
import {
  DEFAULT_AVATAR_SRC,
  appendMessage,
  localizeMessageIdentities,
  removeTypingIndicator,
  replaceTypingIndicator,
  showTypingIndicator,
  scrollToLatest,
  waitForMinimumTypingDuration,
} from "./chat-ui.mjs";

const chat = document.getElementById("chat");
const form = document.getElementById("chat-form");
const message = document.getElementById("message");
const send = document.getElementById("send");
const stop = document.getElementById("stop-chat");
const status = document.getElementById("status");
const language = document.getElementById("language");
const microphone = document.getElementById("microphone");
const stopListening = document.getElementById("stop-listening");
const cancelListening = document.getElementById("cancel-listening");
const interim = document.getElementById("interim-transcript");
const readAloud = document.getElementById("read-aloud");
const stopAudio = document.getElementById("stop-audio");
const newChat = document.getElementById("new-chat");
const candidatePage = document.querySelector(".candidate-page") || document.body;
const sessionKey = "candidate-screening-session";
let session = null;
let busy = false;
let terminal = false;
let draftMode = "text";
let pendingTurn = null;

const copy = {
  en: {
    eyebrow: "Candidate screening", title: "Delivery driver application", language_label: "Language",
    assistant: "AI Recruitment Assistant", assistant_name: "Olivia", assistant_subtitle: "AI Recruitment Assistant",
    assistant_online: "Online", online: "Online", assistant_avatar_alt: "Olivia, AI Recruitment Assistant", user_identity: "You",
    typing: "Olivia is responding.", disclosure_label: "Disclosure",
    disclosure: "This chat collects job-related details for recruiter review. You can stop at any time. Do not share passwords, payment details, contact details, or government ID.",
    conversation_label: "Conversation", message_label: "Your message", message_placeholder: "Write your answer…",
    send: "Send", speak: "Speak", stop_listening: "Finish", cancel_listening: "Cancel",
    read_aloud: "Read replies aloud", stop_audio: "Stop audio", new_chat: "Start over",
    stop_chat: "Stop / Opt out", fineprint: "A recruiter reviews screening information. The assistant does not make a subjective hiring decision.",
    voice_privacy: "Voice recognition may be processed by your browser provider. Audio is not sent to or stored by this application. Review the transcript before sending.",
    starting: "Starting…", sending: "Sending…", switching: "Changing language…",
    terminal: "This screening is complete. A recruiter will review the outcome.",
    opted_out: "You opted out. Start a new chat if you want to continue.", start_failed: "Unable to start a chat.",
    try_again: "Please try again.", connection: "Connection problem. Please try again.",
    requesting_permission: "Allow microphone access in your browser.", listening: "Listening…",
    transcribing: "Finishing transcript…", draft_ready: "Review the transcript, edit it if needed, then press Send.",
    speaking: "Reading the reply aloud…", unsupported: "Voice input is unavailable in this browser; you can keep typing.",
    no_speech: "I did not hear any speech. Try again or type your answer.", no_match: "I could not recognize that. Try again or type your answer.",
    not_allowed: "Microphone permission was denied. You can continue by typing.", audio_capture: "No microphone is available. You can continue by typing.",
    network: "Voice recognition is unavailable. You can continue by typing.", recognition_failed: "Voice recognition failed. You can continue by typing.",
    speech_failed: "Audio playback failed; the written reply is still available.",
  },
  es: {
    eyebrow: "Evaluación de candidatura", title: "Solicitud de repartidor/a", language_label: "Idioma",
    assistant: "Asistente de reclutamiento con IA", assistant_name: "Olivia", assistant_subtitle: "Asistente de reclutamiento con IA",
    assistant_online: "En línea", online: "En línea", assistant_avatar_alt: "Olivia, asistente de reclutamiento con IA", user_identity: "Tú",
    typing: "Olivia está respondiendo.", disclosure_label: "Información",
    disclosure: "Este chat recoge datos relacionados con el trabajo para que los revise una persona reclutadora. Puedes parar cuando quieras. No compartas contraseñas, datos de pago, datos de contacto ni documentos de identidad.",
    conversation_label: "Conversación", message_label: "Tu mensaje", message_placeholder: "Escribe tu respuesta…",
    send: "Enviar", speak: "Hablar", stop_listening: "Terminar", cancel_listening: "Cancelar",
    read_aloud: "Leer respuestas en voz alta", stop_audio: "Parar audio", new_chat: "Empezar de nuevo",
    stop_chat: "Parar / salir", fineprint: "Una persona reclutadora revisa la información. El asistente no toma una decisión subjetiva de contratación.",
    voice_privacy: "El reconocimiento de voz puede ser procesado por el proveedor de tu navegador. Esta aplicación no recibe ni almacena el audio. Revisa la transcripción antes de enviarla.",
    starting: "Iniciando…", sending: "Enviando…", switching: "Cambiando de idioma…",
    terminal: "Esta evaluación ha terminado. Una persona reclutadora revisará el resultado.",
    opted_out: "Has salido de la evaluación. Empieza un chat nuevo si quieres continuar.", start_failed: "No se puede iniciar el chat.",
    try_again: "Inténtalo de nuevo.", connection: "Problema de conexión. Inténtalo de nuevo.",
    requesting_permission: "Permite el acceso al micrófono en el navegador.", listening: "Escuchando…",
    transcribing: "Terminando la transcripción…", draft_ready: "Revisa la transcripción, corrígela si hace falta y pulsa Enviar.",
    speaking: "Leyendo la respuesta en voz alta…", unsupported: "La entrada de voz no está disponible en este navegador; puedes seguir escribiendo.",
    no_speech: "No he detectado voz. Inténtalo de nuevo o escribe tu respuesta.", no_match: "No he podido reconocerlo. Inténtalo de nuevo o escribe tu respuesta.",
    not_allowed: "No se ha permitido usar el micrófono. Puedes continuar escribiendo.", audio_capture: "No hay ningún micrófono disponible. Puedes continuar escribiendo.",
    network: "El reconocimiento de voz no está disponible. Puedes continuar escribiendo.", recognition_failed: "El reconocimiento de voz ha fallado. Puedes continuar escribiendo.",
    speech_failed: "La reproducción de audio ha fallado; la respuesta escrita sigue disponible.",
  },
};

function currentLanguage() { return language.value === "en" ? "en" : "es"; }
function translate(name) { return copy[currentLanguage()][name] || copy.es[name] || name; }

function applyChromeLanguage(nextLanguage) {
  language.value = nextLanguage === "en" ? "en" : "es";
  document.documentElement.lang = currentLanguage();
  for (const element of document.querySelectorAll("[data-i18n]")) {
    if (element.dataset.i18n) element.textContent = translate(element.dataset.i18n);
  }
  for (const element of document.querySelectorAll("[data-i18n-placeholder]")) {
    if (element.dataset.i18nPlaceholder) element.placeholder = translate(element.dataset.i18nPlaceholder);
  }
  for (const element of document.querySelectorAll("[data-i18n-aria-label]")) {
    if (element.dataset.i18nAriaLabel) element.setAttribute("aria-label", translate(element.dataset.i18nAriaLabel));
  }
  for (const element of document.querySelectorAll("[data-i18n-alt]")) {
    if (element.dataset.i18nAlt) {
      const key = element.dataset.i18nAlt === "assistant" ? "assistant_avatar_alt" : element.dataset.i18nAlt;
      element.setAttribute("alt", translate(key));
    }
  }
  for (const element of document.querySelectorAll("[data-i18n-presence]")) {
    if (!element.dataset.i18nPresence) continue;
    const label = element.lastElementChild || element;
    label.textContent = translate(element.dataset.i18nPresence);
  }
  localizeMessageIdentities(chat, {
    assistantName: translate("assistant_name"),
    assistantSubtitle: translate("assistant_subtitle"),
    assistantAlt: translate("assistant_avatar_alt"),
    userName: translate("user_identity"),
  });
}

function addBubble(text, role, { forceScroll = false, forceNewGroup = false } = {}) {
  return appendMessage(chat, text, role, {
    assistantName: translate("assistant_name"),
    assistantSubtitle: translate("assistant_subtitle"),
    assistantAlt: translate("assistant_avatar_alt"),
    userName: translate("user_identity"),
    avatarSrc: DEFAULT_AVATAR_SRC,
    forceScroll,
    forceNewGroup,
  });
}

function showPendingAssistant({ forceScroll = true } = {}) {
  return showTypingIndicator(chat, {
    assistantName: translate("assistant_name"),
    assistantSubtitle: translate("assistant_subtitle"),
    assistantAlt: translate("assistant_avatar_alt"),
    avatarSrc: DEFAULT_AVATAR_SRC,
    typingLabel: translate("typing"),
    forceScroll,
  });
}

function replacePendingAssistant(text, { forceScroll = false } = {}) {
  return replaceTypingIndicator(chat, text, { forceScroll });
}

function resizeMessage() {
  if (!message?.style) return;
  message.style.height = "auto";
  const maxHeight = 140;
  const nextHeight = Math.min(Math.max(message.scrollHeight || 0, 48), maxHeight);
  message.style.height = `${nextHeight}px`;
  message.style.overflowY = (message.scrollHeight || 0) > maxHeight ? "auto" : "hidden";
}

function setVoiceStateClass(state) {
  const stateClasses = Object.values(VoiceState).map((value) => `voice-state-${value}`);
  const visualState = state === VoiceState.REQUESTING_PERMISSION ? "requesting" : state;
  for (const element of [candidatePage, microphone]) {
    if (!element?.classList) continue;
    for (const className of stateClasses) element.classList.remove(className);
    element.classList.add(`voice-state-${state}`, `voice-state-${visualState}`);
    if (element.dataset) {
      element.dataset.voiceState = visualState;
      element.dataset.voiceStateRaw = state;
    }
  }
}

function idempotencyKey() {
  const randomUUID = globalThis.crypto?.randomUUID;
  return typeof randomUUID === "function"
    ? randomUUID.call(globalThis.crypto)
    : `${Date.now()}-${Math.random()}`;
}

function voiceErrorKey(code) {
  const key = String(code || "").replaceAll("-", "_");
  return Object.hasOwn(copy[currentLanguage()], key) ? key : "recognition_failed";
}

function pendingTurnFor(requestMessage) {
  // The server binds idempotency to the submitted message. Reusing this
  // client key for a retry is safer even if the user changed only the input
  // provenance while recovering a failed request.
  const fingerprint = requestMessage;
  if (!pendingTurn || pendingTurn.fingerprint !== fingerprint) {
    pendingTurn = {
      fingerprint,
      key: idempotencyKey(),
      bubbleAdded: false,
      responseTurnId: null,
    };
  }
  return pendingTurn;
}

const browser = typeof window === "object" ? window : globalThis;
const recognitionConstructor = browser.SpeechRecognition || browser.webkitSpeechRecognition;
const voice = new VoiceController({
  Recognition: recognitionConstructor,
  synthesis: browser.speechSynthesis,
  Utterance: browser.SpeechSynthesisUtterance,
  getLanguage: currentLanguage,
  onInterim: (text) => { interim.textContent = text; interim.hidden = !text; },
  onFinal: (text) => { message.value = text; draftMode = "voice"; interim.textContent = ""; interim.hidden = true; resizeMessage(); message.focus(); },
  onError: (code) => { status.textContent = translate(voiceErrorKey(code)); },
  onState: (state) => {
    setVoiceStateClass(state);
    const active = [VoiceState.REQUESTING_PERMISSION, VoiceState.LISTENING, VoiceState.TRANSCRIBING].includes(state);
    stopListening.hidden = !active;
    cancelListening.hidden = !active;
    stopAudio.hidden = state !== VoiceState.SPEAKING;
    microphone.classList.toggle("listening", state === VoiceState.LISTENING);
    microphone.setAttribute("aria-pressed", String(active));
    microphone.setAttribute("aria-busy", String(state === VoiceState.REQUESTING_PERMISSION || state === VoiceState.TRANSCRIBING));
    microphone.disabled = busy || terminal || active || !recognitionConstructor;
    if ([VoiceState.REQUESTING_PERMISSION, VoiceState.LISTENING, VoiceState.TRANSCRIBING, VoiceState.DRAFT_READY, VoiceState.SPEAKING].includes(state)) {
      status.textContent = translate(state);
    } else if (state === VoiceState.IDLE && !busy && !terminal) {
      status.textContent = "";
    } else if (state === VoiceState.UNSUPPORTED && !busy && !terminal) {
      status.textContent = translate("unsupported");
    }
    resizeMessage();
  },
});

function setControls() {
  const disabled = busy || terminal;
  message.disabled = disabled;
  send.disabled = disabled;
  stop.disabled = disabled;
  microphone.disabled = disabled || !voice.supported;
  language.disabled = disabled;
  newChat.disabled = busy;
}

function setTerminal(nextTerminal, optedOut = false) {
  terminal = nextTerminal;
  if (nextTerminal) {
    voice.abort();
    voice.cancelSpeech();
    status.textContent = optedOut ? translate("opted_out") : translate("terminal");
  }
  setControls();
}

function isTerminal(data) {
  // CandidateTurnResponse.status is the status of this individual turn and
  // is normally "completed" after every successful request. Only the
  // screening status determines whether the conversation should be locked.
  return data && ["qualified", "disqualified", "needs_review", "abandoned"].includes(data.screening_status);
}

async function start() {
  if (busy) return;
  busy = true; terminal = false; setControls(); removeTypingIndicator(chat); chat.replaceChildren(); status.textContent = translate("starting");
  try {
    const response = await fetch("/api/v1/candidate/conversations", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: currentLanguage(), channel: "web" }),
    });
    if (!response.ok) throw new Error("start_failed");
    session = await response.json();
    pendingTurn = null;
    localStorage.setItem(sessionKey, JSON.stringify(session));
    applyChromeLanguage(session.language); addBubble(session.assistant_message, "assistant", { forceScroll: true, forceNewGroup: true });
    status.textContent = voice.supported ? "" : translate("unsupported");
  } finally { busy = false; setControls(); message.focus(); }
}

async function restore() {
  const raw = localStorage.getItem(sessionKey);
  if (!raw) return start();
  try {
    session = JSON.parse(raw); applyChromeLanguage(session.language);
    const response = await fetch(`/api/v1/candidate/conversations/${encodeURIComponent(session.conversation_id)}`, {
      headers: { Authorization: `Bearer ${session.resume_token}` },
    });
    if (!response.ok) throw new Error("restore_failed");
    const view = await response.json(); removeTypingIndicator(chat); chat.replaceChildren();
    for (const item of view.messages || []) addBubble(item.content, item.direction === "user" ? "user" : "assistant");
    applyChromeLanguage(view.language); scrollToLatest(chat, { force: true });
    setTerminal(view.status === "completed" || view.status === "opted_out", view.status === "opted_out");
  } catch (_error) { localStorage.removeItem(sessionKey); return start(); }
}

async function sendTurn(content, explicitLanguage = null, inputMode = "text") {
  if (!session || typeof content !== "string" || !content.trim() || busy || terminal) return;
  const requestMessage = content.trim();
  const pending = pendingTurnFor(requestMessage);
  if (!pending.bubbleAdded) {
    // The candidate just acted, so the latest interaction should remain
    // visible even when the transcript was already scrolled near its end.
    addBubble(requestMessage, "user", { forceScroll: true });
    pending.bubbleAdded = true;
  }
  const typingStartedAt = Date.now();
  showPendingAssistant({ forceScroll: true });
  message.value = "";
  resizeMessage();
  draftMode = "text";
  busy = true; voice.setProcessing(true); status.textContent = translate("sending"); setControls();
  let replyToSpeak = null;
  let retryMessage = null;
  let assistantMessageDisplayed = false;
  try {
    const response = await fetch(`/api/v1/candidate/conversations/${encodeURIComponent(session.conversation_id)}/turns`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${session.resume_token}`, "Idempotency-Key": pending.key },
      body: JSON.stringify({ message: requestMessage, input_mode: inputMode, ...(explicitLanguage ? { language: explicitLanguage } : {}) }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      retryMessage = data.error?.message || data.assistant_message || translate("try_again");
      // Errors are actionable immediately; never add the success affordance
      // delay to a failed or unavailable turn.
      if (data.assistant_message && pending.responseTurnId !== data.turn_id) {
        replacePendingAssistant(data.assistant_message);
        assistantMessageDisplayed = true;
        pending.responseTurnId = data.turn_id || "displayed";
      }
      if (!assistantMessageDisplayed) replacePendingAssistant(retryMessage);
      // A failed turn is already durably recorded by the API. Retrying it
      // with the same key would only replay that failure, so let the next
      // deliberate submit receive a fresh key. If no turn ID was returned,
      // the request may not have reached the reservation boundary and the
      // key remains reusable.
      if (data.turn_id) pendingTurn = null;
    } else {
      if (data.assistant_message && pending.responseTurnId !== data.turn_id) {
        await waitForMinimumTypingDuration(typingStartedAt);
        replacePendingAssistant(data.assistant_message);
        assistantMessageDisplayed = true;
        pending.responseTurnId = data.turn_id || "displayed";
      }
      session.screening_status = data.screening_status; localStorage.setItem(sessionKey, JSON.stringify(session));
      setTerminal(isTerminal(data), data.screening_status === "abandoned");
      if (!terminal && readAloud.checked && data.assistant_message) replyToSpeak = data.assistant_message;
      else if (!terminal) status.textContent = "";
      pendingTurn = null;
    }
  } catch (_error) {
    retryMessage = translate("connection");
    replacePendingAssistant(retryMessage);
  } finally {
    // The cleanup is intentionally defensive: every request path must leave
    // no pending item behind, including malformed responses and exceptions
    // thrown before fetch reaches the network.
    removeTypingIndicator(chat);
    busy = false;
    voice.setProcessing(false);
    setControls();
    if (retryMessage) {
      status.textContent = retryMessage;
      message.value = requestMessage;
      draftMode = inputMode;
      resizeMessage();
    }
    if (replyToSpeak) voice.speak(replyToSpeak, currentLanguage());
    if (!terminal) {
      message.focus();
    }
  }
}

form.addEventListener("submit", (event) => { event.preventDefault(); void sendTurn(message.value, null, draftMode); });
message.addEventListener("input", () => { if (!message.value.trim()) draftMode = "text"; resizeMessage(); });
message.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!busy && !terminal) void sendTurn(message.value, null, draftMode);
});
microphone.addEventListener("click", () => voice.start());
stopListening.addEventListener("click", () => voice.stop());
cancelListening.addEventListener("click", () => voice.abort());
stopAudio.addEventListener("click", () => voice.cancelSpeech());
language.addEventListener("change", () => {
  voice.abort(); voice.cancelSpeech(); const nextLanguage = currentLanguage(); applyChromeLanguage(nextLanguage);
  if (session && !terminal) { status.textContent = translate("switching"); void sendTurn(nextLanguage === "en" ? "English please" : "Prefiero español", nextLanguage); }
});
newChat.addEventListener("click", () => {
  if (busy) return;
  voice.abort();
  voice.cancelSpeech();
  session = null;
  pendingTurn = null;
  localStorage.removeItem(sessionKey);
  void start().catch(() => { status.textContent = translate("start_failed"); });
});
stop.addEventListener("click", () => { void sendTurn(currentLanguage() === "en" ? "I want to stop" : "Quiero parar"); });

applyChromeLanguage(language.value);
resizeMessage();
setControls();
if (!voice.supported) status.textContent = translate("unsupported");
void restore().catch(() => { status.textContent = translate("start_failed"); });
