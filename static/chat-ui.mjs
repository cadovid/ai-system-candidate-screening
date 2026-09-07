/**
 * Small, dependency-free message renderer for the candidate conversation.
 *
 * The renderer deliberately knows nothing about the candidate API. It owns
 * only DOM structure, safe text rendering, participant identity, and the
 * temporary assistant typing state used while a turn is in flight.
 */

export const DEFAULT_AVATAR_SRC = "/static/images/olivia-avatar.webp";
export const MIN_TYPING_DURATION_MS = 360;

const ASSISTANT_ROLE = "assistant";
const USER_ROLE = "user";
const PENDING_SELECTOR = '[data-pending="true"]';

/**
 * Keep a very fast successful reply human-readable without slowing down a
 * provider that is already taking longer than the minimum.  The clock and
 * delay are injectable so the browser behavior can be tested without real
 * timers; callers should invoke this only for successful replies.
 */
export async function waitForMinimumTypingDuration(
  startedAt,
  {
    minimumMs = MIN_TYPING_DURATION_MS,
    now = () => Date.now(),
    delay = (duration) => new Promise((resolve) => setTimeout(resolve, duration)),
  } = {},
) {
  const elapsed = Math.max(0, Number(now()) - Number(startedAt));
  const remaining = Math.max(0, Number(minimumMs) - elapsed);
  if (remaining <= 0) return 0;
  await delay(remaining);
  return remaining;
}

function normalizeRole(role) {
  return role === USER_ROLE ? USER_ROLE : ASSISTANT_ROLE;
}

function documentFor(container) {
  return container?.ownerDocument || globalThis.document;
}

function createElement(container, tagName, className = "") {
  const document = documentFor(container);
  if (!document?.createElement) throw new Error("A document is required to render chat messages");
  const element = document.createElement(tagName);
  if (className) element.className = className;
  return element;
}

function append(parent, child) {
  parent.appendChild(child);
  return child;
}

function setData(element, name, value) {
  if (element.dataset) element.dataset[name] = String(value);
  else element.setAttribute(`data-${name.replace(/[A-Z]/g, (match) => `-${match.toLowerCase()}`)}`, String(value));
}

function getData(element, name) {
  if (element?.dataset && element.dataset[name] !== undefined) return element.dataset[name];
  return element?.getAttribute?.(`data-${name.replace(/[A-Z]/g, (match) => `-${match.toLowerCase()}`)}`);
}

function hasPendingState(element) {
  return getData(element, "pending") === "true";
}

function lastElement(container) {
  const children = container?.children;
  return children && children.length ? children[children.length - 1] : null;
}

function messageGroupRole(group) {
  return getData(group, "role") || (group?.classList?.contains("user") ? USER_ROLE : ASSISTANT_ROLE);
}

function messageGroup(container, role, {
  assistantName = "Olivia",
  assistantSubtitle = "AI Recruitment Assistant",
  assistantAlt = "Olivia, AI Recruitment Assistant",
  userName = "You",
  avatarSrc = DEFAULT_AVATAR_SRC,
} = {}) {
  const normalizedRole = normalizeRole(role);
  const group = createElement(
    container,
    "article",
    `message-group message-${normalizedRole} ${normalizedRole}`,
  );
  setData(group, "messageGroup", "true");
  setData(group, "role", normalizedRole);
  group.setAttribute?.("aria-label", normalizedRole === ASSISTANT_ROLE ? assistantName : userName);

  const identity = append(
    group,
    createElement(container, "div", `message-identity ${normalizedRole}-identity`),
  );
  setData(identity, "messageIdentity", normalizedRole);

  if (normalizedRole === ASSISTANT_ROLE) {
    const avatar = append(identity, createElement(container, "img", "message-avatar assistant-avatar"));
    avatar.src = avatarSrc;
    avatar.alt = assistantAlt;
    setData(avatar, "messageAvatar", "assistant");

    const identityText = append(identity, createElement(container, "span", "message-identity-text"));
    const sender = append(identityText, createElement(container, "span", "message-sender"));
    sender.textContent = assistantName;
    setData(sender, "messageSender", ASSISTANT_ROLE);
    const subtitle = append(identityText, createElement(container, "span", "message-subtitle"));
    subtitle.textContent = assistantSubtitle;
    setData(subtitle, "messageSubtitle", ASSISTANT_ROLE);
  } else {
    const sender = append(identity, createElement(container, "span", "message-sender"));
    sender.textContent = userName;
    setData(sender, "messageSender", USER_ROLE);
  }
  return group;
}

function messageBubble(container, text, role) {
  const normalizedRole = normalizeRole(role);
  const bubble = createElement(container, "div", `bubble ${normalizedRole} message-bubble`);
  setData(bubble, "messageBubble", normalizedRole);
  // textContent is intentional: candidate/assistant text must never become
  // executable markup, including content returned after an API error.
  bubble.textContent = String(text ?? "");
  return bubble;
}

function pendingIndicators(container) {
  if (!container) return [];
  if (typeof container.querySelectorAll === "function") {
    return Array.from(container.querySelectorAll(PENDING_SELECTOR));
  }
  return Array.from(container.children || []).filter(hasPendingState);
}

export function findTypingIndicator(container) {
  return pendingIndicators(container)[0] || null;
}

export function isNearBottom(container, threshold = 96) {
  if (!container) return true;
  const scrollHeight = Number(container.scrollHeight) || 0;
  const scrollTop = Number(container.scrollTop) || 0;
  const clientHeight = Number(container.clientHeight) || 0;
  return scrollHeight - scrollTop - clientHeight <= threshold;
}

export function scrollToLatest(container, { force = false, wasNearBottom = true } = {}) {
  if (!container || (!force && !wasNearBottom)) return false;
  container.scrollTop = container.scrollHeight;
  return true;
}

function shouldScroll(container, forceScroll) {
  return Boolean(forceScroll) || isNearBottom(container);
}

/**
 * Append a regular participant message. Consecutive messages from one
 * participant share the identity row while retaining separate bubbles.
 */
export function renderMessage(container, text, role = ASSISTANT_ROLE, options = {}) {
  const normalizedRole = normalizeRole(role);
  const {
    forceNewGroup = false,
    forceScroll = false,
    scroll = true,
  } = options;
  const wasNearBottom = shouldScroll(container, forceScroll);
  let group = !forceNewGroup ? lastElement(container) : null;
  if (!group || messageGroupRole(group) !== normalizedRole || hasPendingState(group)) {
    group = messageGroup(container, normalizedRole, options);
    append(container, group);
  }
  const bubble = append(group, messageBubble(container, text, normalizedRole));
  if (scroll) scrollToLatest(container, { force: forceScroll, wasNearBottom });
  return { group, bubble, grouped: group === lastElement(container) && group.children.length > 2 };
}

export const appendMessage = renderMessage;

/**
 * Add the in-flight assistant item. It is idempotent so a retry path or a
 * duplicate event cannot leave two typing indicators in the transcript.
 */
export function showTypingIndicator(container, {
  assistantName = "Olivia",
  assistantSubtitle = "AI Recruitment Assistant",
  assistantAlt = "Olivia, AI Recruitment Assistant",
  avatarSrc = DEFAULT_AVATAR_SRC,
  typingLabel = "Olivia is responding.",
  forceScroll = true,
} = {}) {
  const existing = findTypingIndicator(container);
  if (existing) {
    const all = pendingIndicators(container);
    for (const duplicate of all.slice(1)) duplicate.remove?.();
    scrollToLatest(container, { force: forceScroll, wasNearBottom: true });
    return existing;
  }

  const rendered = renderMessage(container, "", ASSISTANT_ROLE, {
    assistantName,
    assistantSubtitle,
    assistantAlt,
    avatarSrc,
    forceNewGroup: true,
    forceScroll: false,
    scroll: false,
  });
  const { group, bubble } = rendered;
  setData(group, "pending", "true");
  group.classList.add("typing-indicator", "message-pending");
  group.setAttribute?.("role", "status");
  group.setAttribute?.("aria-label", typingLabel);
  bubble.classList.add("typing-bubble");
  bubble.replaceChildren?.();
  bubble.setAttribute?.("aria-hidden", "true");

  const dots = append(bubble, createElement(container, "span", "typing-dots"));
  dots.setAttribute?.("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) {
    const dot = append(dots, createElement(container, "span", "typing-dot"));
    dot.textContent = ".";
    setData(dot, "typingDot", index + 1);
  }
  const accessibleLabel = append(bubble, createElement(container, "span", "sr-only typing-label"));
  accessibleLabel.textContent = typingLabel;
  scrollToLatest(container, { force: forceScroll, wasNearBottom: true });
  return group;
}

function firstBubble(group) {
  return group?.querySelector?.(".message-bubble, .bubble") || null;
}

/** Replace the temporary typing item in place with a safe assistant message. */
export function replaceTypingIndicator(container, text, { forceScroll = false } = {}) {
  const group = findTypingIndicator(container);
  if (!group) return renderMessage(container, text, ASSISTANT_ROLE, { forceScroll });
  const wasNearBottom = shouldScroll(container, forceScroll);
  const bubble = firstBubble(group);
  if (!bubble) return renderMessage(container, text, ASSISTANT_ROLE, { forceScroll });
  bubble.replaceChildren?.();
  bubble.classList.remove("typing-bubble");
  bubble.removeAttribute?.("aria-hidden");
  bubble.textContent = String(text ?? "");
  group.classList.remove("typing-indicator", "message-pending");
  group.removeAttribute?.("data-pending");
  group.removeAttribute?.("role");
  group.removeAttribute?.("aria-label");
  if (group.dataset) delete group.dataset.pending;
  if (wasNearBottom || forceScroll) scrollToLatest(container, { force: forceScroll, wasNearBottom });
  return { group, bubble, replaced: true };
}

/** Remove every temporary typing item, including defensive cleanup leftovers. */
export function removeTypingIndicator(container) {
  const indicators = pendingIndicators(container);
  for (const indicator of indicators) {
    if (typeof indicator.remove === "function") indicator.remove();
    else indicator.parentNode?.removeChild(indicator);
  }
  return indicators.length;
}

export function localizeMessageIdentities(container, {
  assistantName = "Olivia",
  assistantSubtitle = "AI Recruitment Assistant",
  assistantAlt = "Olivia, AI Recruitment Assistant",
  userName = "You",
} = {}) {
  if (!container?.querySelectorAll) return;
  for (const element of container.querySelectorAll("[data-message-sender]")) {
    element.textContent = getData(element, "messageSender") === ASSISTANT_ROLE ? assistantName : userName;
  }
  for (const element of container.querySelectorAll("[data-message-subtitle]")) {
    element.textContent = assistantSubtitle;
  }
  for (const element of container.querySelectorAll('[data-message-avatar="assistant"]')) {
    element.alt = assistantAlt;
  }
  for (const group of container.querySelectorAll('[data-role="assistant"]')) {
    group.setAttribute?.("aria-label", assistantName);
  }
}
