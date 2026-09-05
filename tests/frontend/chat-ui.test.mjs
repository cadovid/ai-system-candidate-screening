import assert from "node:assert/strict";
import test from "node:test";

import {
  findTypingIndicator,
  isNearBottom,
  renderMessage,
  removeTypingIndicator,
  replaceTypingIndicator,
  scrollToLatest,
  showTypingIndicator,
} from "../../static/chat-ui.mjs";

class FakeClassList {
  constructor(element) {
    this.element = element;
    this.values = new Set();
  }

  add(...values) { for (const value of values) this.values.add(value); this.sync(); }
  remove(...values) { for (const value of values) this.values.delete(value); this.sync(); }
  contains(value) { return this.values.has(value); }

  sync() { this.element.className = [...this.values].join(" "); }
}

class FakeElement {
  constructor(ownerDocument, tagName) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = new Map();
    this._className = "";
    Object.defineProperty(this, "className", {
      configurable: true,
      get: () => this._className,
      set: (value) => {
        this._className = String(value);
        if (this.classList) this.classList.values = new Set(this._className.split(/\s+/).filter(Boolean));
      },
    });
    this.classList = new FakeClassList(this);
    this.style = {};
    this.scrollTop = 0;
    this.scrollHeight = 600;
    this.clientHeight = 400;
    this._textContent = "";
  }

  set textContent(value) {
    this._textContent = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    if (this.children.length) return this.children.map((child) => child.textContent).join("");
    return this._textContent;
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    this._textContent = "";
    return child;
  }

  replaceChildren(...children) {
    this.children = [];
    this._textContent = "";
    for (const child of children) this.appendChild(child);
  }

  setAttribute(name, value) {
    const stringValue = String(value);
    this.attributes.set(name, stringValue);
    if (name === "class") {
      this.classList.values = new Set(stringValue.split(/\s+/).filter(Boolean));
    }
    if (name.startsWith("data-")) {
      const dataName = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[dataName] = stringValue;
    }
  }

  getAttribute(name) { return this.attributes.get(name) ?? null; }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name.startsWith("data-")) {
      const dataName = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      delete this.dataset[dataName];
    }
  }

  remove() { this.parentNode?.removeChild(this); }

  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) {
      this.children.splice(index, 1);
      child.parentNode = null;
    }
    return child;
  }

  matches(selector) {
    const classMatch = selector.match(/^\.([\w-]+)$/);
    if (classMatch) return this.classList.contains(classMatch[1]);
    const attrMatch = selector.match(/^\[data-([\w-]+)(?:="([^"]*)")?\]$/);
    if (attrMatch) {
      const key = attrMatch[1].replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      return this.dataset[key] !== undefined && (attrMatch[2] === undefined || this.dataset[key] === attrMatch[2]);
    }
    return false;
  }

  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim());
    const results = [];
    const visit = (element) => {
      for (const child of element.children) {
        if (selectors.some((part) => child.matches(part))) results.push(child);
        visit(child);
      }
    };
    visit(this);
    return results;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class FakeDocument {
  createElement(tagName) { return new FakeElement(this, tagName); }
}

function chat() {
  const document = new FakeDocument();
  const container = document.createElement("section");
  container.ownerDocument = document;
  return container;
}

function groups(container) { return [...container.children]; }
function bubbles(group) { return group.querySelectorAll(".message-bubble"); }

test("renders participant identity, groups consecutive messages, and keeps text safe", () => {
  const container = chat();
  const first = renderMessage(container, "Hello <img src=x onerror=alert(1)>", "assistant", {
    assistantName: "Olivia",
    assistantSubtitle: "AI Recruitment Assistant",
    userName: "You",
    forceScroll: true,
  });
  renderMessage(container, "Second assistant message", "assistant");
  renderMessage(container, "Candidate reply", "user", { userName: "Tú" });

  assert.equal(groups(container).length, 2);
  assert.equal(bubbles(first.group).length, 2);
  assert.equal(bubbles(first.group)[0].textContent, "Hello <img src=x onerror=alert(1)>");
  assert.equal(bubbles(first.group)[0].children.length, 0);
  assert.equal(first.group.querySelector(".message-sender").textContent, "Olivia");
  assert.equal(groups(container)[1].querySelector(".message-sender").textContent, "Tú");
  assert.equal(groups(container)[0].querySelector(".message-subtitle").textContent, "AI Recruitment Assistant");
});

test("typing indicator is idempotent and replaces in place", () => {
  const container = chat();
  renderMessage(container, "Question", "assistant", { forceScroll: true });
  renderMessage(container, "Answer", "user", { forceScroll: true });
  const first = showTypingIndicator(container, { typingLabel: "Olivia está respondiendo.", forceScroll: true });
  const second = showTypingIndicator(container, { typingLabel: "duplicate", forceScroll: true });

  assert.equal(first, second);
  assert.equal(findTypingIndicator(container), first);
  assert.equal(container.querySelectorAll('[data-pending="true"]').length, 1);
  assert.equal(first.querySelectorAll(".typing-dot").length, 3);
  assert.equal(first.querySelector(".typing-label").textContent, "Olivia está respondiendo.");

  const replaced = replaceTypingIndicator(container, "Final <b>answer</b>");
  assert.equal(replaced.group, first);
  assert.equal(findTypingIndicator(container), null);
  assert.equal(bubbles(first)[0].textContent, "Final <b>answer</b>");
  assert.equal(first.querySelectorAll(".typing-dot").length, 0);
});

test("pending cleanup removes every leftover indicator", () => {
  const container = chat();
  showTypingIndicator(container, { forceScroll: false });
  const duplicate = showTypingIndicator(container, { forceScroll: false });
  duplicate.dataset.pending = "true";
  const removed = removeTypingIndicator(container);
  assert.equal(removed, 1);
  assert.equal(findTypingIndicator(container), null);
});

test("scroll helpers honor intentional upward reading and forced new turns", () => {
  const container = chat();
  container.scrollHeight = 1000;
  container.clientHeight = 400;
  container.scrollTop = 0;
  assert.equal(isNearBottom(container), false);
  renderMessage(container, "Older context", "assistant");
  assert.equal(container.scrollTop, 0);

  assert.equal(scrollToLatest(container, { wasNearBottom: false }), false);
  assert.equal(scrollToLatest(container, { force: true }), true);
  assert.equal(container.scrollTop, 1000);
  assert.equal(isNearBottom(container), true);
});
