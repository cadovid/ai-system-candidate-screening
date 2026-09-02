(() => {
  "use strict";

  const chat = document.getElementById("chat");
  const form = document.getElementById("chat-form");
  const message = document.getElementById("message");
  const send = document.getElementById("send");
  const stop = document.getElementById("stop-chat");
  const status = document.getElementById("status");
  const language = document.getElementById("language");
  const key = "candidate-screening-session";
  let session = null;
  let busy = false;
  let terminal = false;

  const copy = {
    en: {
      eyebrow: "Candidate screening",
      title: "Delivery driver application",
      language_label: "Language",
      assistant: "Automated assistant",
      disclosure_label: "Disclosure",
      disclosure: "This chat collects job-related details for recruiter review. You can stop at any time. Do not share passwords, payment details, contact details, or government ID.",
      conversation_label: "Conversation",
      message_label: "Your message",
      message_placeholder: "Write your answer…",
      send: "Send",
      new_chat: "Start over",
      stop_chat: "Stop / Opt out",
      fineprint: "A recruiter reviews screening information. The assistant does not make a subjective hiring decision.",
      starting: "Starting…",
      sending: "Sending…",
      switching: "Changing language…",
      terminal: "This screening is complete. A recruiter will review the outcome.",
      opted_out: "You opted out. Start a new chat if you want to continue.",
      start_failed: "Unable to start a chat.",
      try_again: "Please try again.",
      connection: "Connection problem. Please try again.",
    },
    es: {
      eyebrow: "Evaluación de candidatura",
      title: "Solicitud de repartidor/a",
      language_label: "Idioma",
      assistant: "Asistente automatizado",
      disclosure_label: "Información",
      disclosure: "Este chat recoge datos relacionados con el trabajo para que los revise una persona reclutadora. Puedes parar cuando quieras. No compartas contraseñas, datos de pago, datos de contacto ni documentos de identidad.",
      conversation_label: "Conversación",
      message_label: "Tu mensaje",
      message_placeholder: "Escribe tu respuesta…",
      send: "Enviar",
      new_chat: "Empezar de nuevo",
      stop_chat: "Parar / salir",
      fineprint: "Una persona reclutadora revisa la información. El asistente no toma una decisión subjetiva de contratación.",
      starting: "Iniciando…",
      sending: "Enviando…",
      switching: "Cambiando de idioma…",
      terminal: "Esta evaluación ha terminado. Una persona reclutadora revisará el resultado.",
      opted_out: "Has salido de la evaluación. Empieza un chat nuevo si quieres continuar.",
      start_failed: "No se puede iniciar el chat.",
      try_again: "Inténtalo de nuevo.",
      connection: "Problema de conexión. Inténtalo de nuevo.",
    },
  };

  function currentLanguage() {
    return language.value === "en" ? "en" : "es";
  }

  function translate(keyName) {
    return copy[currentLanguage()][keyName] || copy.es[keyName] || keyName;
  }

  function applyChromeLanguage(nextLanguage) {
    language.value = nextLanguage === "en" ? "en" : "es";
    document.documentElement.lang = currentLanguage();
    for (const element of document.querySelectorAll("[data-i18n]")) {
      const keyName = element.dataset.i18n;
      if (keyName) element.textContent = translate(keyName);
    }
    for (const element of document.querySelectorAll("[data-i18n-placeholder]")) {
      const keyName = element.dataset.i18nPlaceholder;
      if (keyName) element.setAttribute("placeholder", translate(keyName));
    }
    for (const element of document.querySelectorAll("[data-i18n-aria-label]")) {
      const keyName = element.dataset.i18nAriaLabel;
      if (keyName) element.setAttribute("aria-label", translate(keyName));
    }
  }

  function setControls() {
    const disabled = busy || terminal;
    message.disabled = disabled;
    send.disabled = disabled;
    stop.disabled = disabled;
  }

  function setTerminal(nextTerminal, optedOut = false) {
    terminal = nextTerminal;
    if (nextTerminal && !busy) {
      status.textContent = optedOut ? translate("opted_out") : translate("terminal");
    }
    setControls();
  }

  function isTerminal(data) {
    return data && (data.status === "completed" || data.status === "opted_out" ||
      ["qualified", "disqualified", "needs_review", "abandoned"].includes(data.screening_status));
  }

  function addBubble(text, role) {
    const bubble = document.createElement("div");
    bubble.className = `bubble ${role}`;
    bubble.textContent = text;
    chat.appendChild(bubble);
    chat.scrollTop = chat.scrollHeight;
  }

  function clearChat() {
    while (chat.firstChild) chat.removeChild(chat.firstChild);
  }

  function idempotencyKey() {
    return typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`;
  }

  async function start() {
    busy = true;
    terminal = false;
    setControls();
    clearChat();
    status.textContent = translate("starting");
    try {
      const response = await fetch("/api/v1/candidate/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: currentLanguage(), channel: "web" }),
      });
      if (!response.ok) throw new Error("start_failed");
      session = await response.json();
      localStorage.setItem(key, JSON.stringify(session));
      applyChromeLanguage(session.language);
      addBubble(session.assistant_message, "assistant");
      status.textContent = "";
    } finally {
      busy = false;
      setControls();
      message.focus();
    }
  }

  async function restore() {
    const raw = localStorage.getItem(key);
    if (!raw) return start();
    try {
      session = JSON.parse(raw);
      applyChromeLanguage(session.language);
      const response = await fetch(`/api/v1/candidate/conversations/${encodeURIComponent(session.conversation_id)}`, {
        headers: { Authorization: `Bearer ${session.resume_token}` },
      });
      if (!response.ok) throw new Error("restore_failed");
      const view = await response.json();
      clearChat();
      for (const item of view.messages || []) addBubble(item.content, item.direction === "user" ? "user" : "assistant");
      applyChromeLanguage(view.language);
      setTerminal(view.status === "completed" || view.status === "opted_out", view.status === "opted_out");
    } catch (_) {
      localStorage.removeItem(key);
      return start();
    }
  }

  async function sendTurn(content, explicitLanguage = null) {
    if (!session || !content.trim() || busy || terminal) return;
    const requestMessage = content.trim();
    addBubble(requestMessage, "user");
    message.value = "";
    busy = true;
    status.textContent = translate("sending");
    setControls();
    try {
      const response = await fetch(`/api/v1/candidate/conversations/${encodeURIComponent(session.conversation_id)}/turns`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${session.resume_token}`,
          "Idempotency-Key": idempotencyKey(),
        },
        body: JSON.stringify({ message: requestMessage, ...(explicitLanguage ? { language: explicitLanguage } : {}) }),
      });
      const data = await response.json().catch(() => ({}));
      if (data.assistant_message) addBubble(data.assistant_message, "assistant");
      if (!response.ok) {
        status.textContent = data.error?.message || translate("try_again");
      } else {
        session.screening_status = data.screening_status;
        localStorage.setItem(key, JSON.stringify(session));
        setTerminal(isTerminal(data), data.screening_status === "abandoned");
        if (!terminal) status.textContent = "";
      }
    } catch (_) {
      status.textContent = translate("connection");
    } finally {
      busy = false;
      setControls();
      if (!terminal) message.focus();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void sendTurn(message.value);
  });

  language.addEventListener("change", () => {
    const nextLanguage = currentLanguage();
    applyChromeLanguage(nextLanguage);
    if (!session || terminal) return;
    status.textContent = translate("switching");
    // Keep the language change auditable and let the server-side interpreter
    // update canonical preferred_language for the next prompt.
    void sendTurn(nextLanguage === "en" ? "English please" : "Prefiero español", nextLanguage);
  });

  document.getElementById("new-chat").addEventListener("click", () => {
    localStorage.removeItem(key);
    void start().catch(() => { status.textContent = translate("start_failed"); });
  });

  stop.addEventListener("click", () => {
    void sendTurn(currentLanguage() === "en" ? "I want to stop" : "Quiero parar");
  });

  applyChromeLanguage(language.value);
  void restore().catch(() => { status.textContent = translate("start_failed"); });
})();
