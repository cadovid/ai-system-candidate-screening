import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const root = new URL("../../", import.meta.url);

class FakeClassList {
  constructor(element) {
    this.element = element;
    this.values = new Set();
  }

  add(...values) {
    for (const value of values) this.values.add(value);
    this.sync();
  }

  remove(...values) {
    for (const value of values) this.values.delete(value);
    this.sync();
  }

  toggle(value, force) {
    const next = force === undefined ? !this.values.has(value) : Boolean(force);
    if (next) this.values.add(value);
    else this.values.delete(value);
    this.sync();
    return next;
  }

  contains(value) { return this.values.has(value); }

  sync() { this.element._className = [...this.values].join(" "); }
}

class FakeText {
  constructor(text) {
    this.textContent = String(text ?? "");
    this.children = [];
    this.parentNode = null;
  }

  matches() { return false; }
  querySelectorAll() { return []; }
}

class FakeElement {
  constructor(ownerDocument, tagName) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
    this.scrollCalls = [];
    this._className = "";
    this.classList = new FakeClassList(this);
  }

  set className(value) {
    this._className = String(value ?? "");
    this.classList.values = new Set(this._className.split(/\s+/).filter(Boolean));
  }

  get className() { return this._className; }

  set textContent(value) {
    this._textContent = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    if (this.children.length) return this.children.map((child) => child.textContent).join("");
    return this._textContent || "";
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(typeof node === "string" ? this.ownerDocument.createTextNode(node) : node);
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    this._textContent = "";
    return child;
  }

  replaceChildren(...children) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._textContent = "";
    this.append(...children);
  }

  setAttribute(name, value) {
    const stringValue = String(value);
    this.attributes.set(name, stringValue);
    if (name === "class") this.className = stringValue;
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[key] = stringValue;
    }
  }

  getAttribute(name) { return this.attributes.get(name) ?? null; }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      delete this.dataset[key];
    }
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatchEvent(event) {
    const value = event || {};
    if (!value.type) throw new Error("Fake events require a type");
    for (const listener of this.listeners.get(value.type) || []) listener(value);
    return true;
  }

  matches(selector) {
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    const attribute = selector.match(/^\[data-([\w-]+)(?:="([^"]*)")?\]$/);
    if (attribute) {
      const key = attribute[1].replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      return this.dataset[key] !== undefined && (attribute[2] === undefined || this.dataset[key] === attribute[2]);
    }
    return selector.toLowerCase() === this.tagName.toLowerCase();
  }

  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim());
    const result = [];
    const visit = (node) => {
      for (const child of node.children || []) {
        if (selectors.some((part) => child.matches(part))) result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  closest(selector) {
    let current = this;
    while (current) {
      if (current.matches(selector)) return current;
      current = current.parentNode;
    }
    return null;
  }

  scrollIntoView(options) { this.scrollCalls.push(options); }
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement(this, "body");
    this.ids = new Map();
  }

  createElement(tagName) { return new FakeElement(this, tagName); }
  createTextNode(text) { return new FakeText(text); }
  getElementById(id) { return this.ids.get(id) || null; }
  querySelector(selector) { return this.body.querySelector(selector); }

  register(id, element) {
    this.ids.set(id, element);
    element.id = id;
    return element;
  }
}

function buildDocument() {
  const document = new FakeDocument();
  const layout = document.createElement("div");
  layout.className = "review-layout";
  const form = document.register("recruiter-form", document.createElement("form"));
  const key = document.register("internal-key", document.createElement("input"));
  const status = document.register("recruiter-status", document.createElement("p"));
  const statusFilter = document.register("status-filter", document.createElement("select"));
  const queue = document.register("queue", document.createElement("section"));
  const detail = document.register("detail", document.createElement("aside"));
  const detailTitle = document.register("detail-title", document.createElement("h2"));
  const detailContent = document.register("detail-content", document.createElement("div"));
  const closeDetail = document.register("close-detail", document.createElement("button"));
  const retry = document.register("retry-summary", document.createElement("button"));
  const review = document.register("review-form", document.createElement("form"));
  const reviewer = document.register("reviewer-id", document.createElement("input"));
  const decision = document.register("review-decision", document.createElement("select"));
  const notes = document.register("review-notes", document.createElement("textarea"));
  const reviewStatus = document.register("review-status", document.createElement("p"));
  layout.append(queue, detail);
  detail.append(detailTitle, detailContent, closeDetail, retry, review, reviewStatus);
  review.append(reviewer, decision, notes);
  document.body.append(form, key, status, statusFilter, layout);
  return { document, form, key, status, statusFilter, queue, layout, detail, detailTitle, detailContent, closeDetail, retry, review, reviewer, decision, notes, reviewStatus };
}

function response(payload, ok = true) {
  return { ok, json: async () => payload };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function deferred() {
  let resolve;
  const promise = new Promise((value) => { resolve = value; });
  return { promise, resolve };
}

async function loadRecruiter(fetchImpl, options = {}) {
  const controls = buildDocument();
  const context = vm.createContext({
    document: controls.document,
    window: {
      innerWidth: options.innerWidth ?? 390,
      matchMedia: (query) => ({ matches: query === "(max-width: 800px)" && !options.reducedMotion }),
    },
    fetch: fetchImpl,
    encodeURIComponent,
    setImmediate,
    Promise,
    JSON,
    Error,
    String,
  });
  const source = await readFile(new URL("static/recruiter.js", root), "utf8");
  vm.runInContext(source, context);
  return controls;
}

test("queue presentation uses safe status badges, empty state, and selection persistence", async () => {
  const calls = [];
  const queueItems = [
    {
      session_id: "session-1",
      candidate_name: "Ada <script>alert(1)</script>",
      status: "qualified",
      summary: "Ready for review",
      summary_status: "generated",
    },
    {
      session_id: "session-2",
      candidate_name: "Unknown status",
      status: "future_state",
      summary: null,
      summary_status: "pending",
    },
  ];
  const controls = await loadRecruiter(async (url, options) => {
    calls.push({ url, options });
    if (url.includes("/screenings/session-1")) return response({
      session_id: "session-1",
      candidate_name: queueItems[0].candidate_name,
      status: "qualified",
      summary: "Ready",
      summary_status: "generated",
      handoff_status: "ready",
      messages: [],
    });
    return response({ items: queueItems });
  });
  controls.key.value = "secret";
  assert.equal(controls.statusFilter.disabled, true);
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();

  const cards = controls.queue.querySelectorAll(".screening-card");
  assert.equal(controls.statusFilter.disabled, false);
  assert.equal(cards.length, 2);
  assert.equal(cards[0].querySelector(".screening-card__name").textContent, "Ada <script>alert(1)</script>");
  assert.equal(cards[0].querySelector(".status-badge").textContent, "Qualified");
  assert.equal(cards[0].querySelector(".status-badge").classList.contains("status-badge--qualified"), true);
  assert.equal(cards[1].querySelector(".status-badge").textContent, "Future State");
  assert.equal(cards[1].querySelector(".status-badge").classList.contains("status-badge--neutral"), true);
  assert.equal(controls.queue.querySelectorAll(".status-column").length, 6);
  assert.equal(controls.queue.querySelector(".status-column--qualified").querySelector(".status-column__count").textContent, "1");
  assert.equal(controls.queue.querySelector(".status-column--neutral").querySelector(".status-column__count").textContent, "1");
  assert.equal(controls.queue.getAttribute("aria-busy"), "false");
  assert.equal(calls[0].url, "/api/v1/internal/screenings?limit=100");
  assert.equal(calls[0].options.headers.Authorization, "Bearer secret");

  const action = cards[0].querySelector(".screening-card__action");
  action.dispatchEvent({ type: "click" });
  await flush();
  assert.equal(cards[0].classList.contains("is-selected"), true);
  assert.equal(controls.layout.classList.contains("has-detail"), true);
  assert.equal(controls.detail.hidden, false);
  assert.equal(controls.detail.scrollCalls[0].behavior, "smooth");

  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();
  assert.equal(controls.queue.querySelector(".screening-card").classList.contains("is-selected"), true);

  controls.closeDetail.dispatchEvent({ type: "click" });
  assert.equal(controls.detail.hidden, true);
  assert.equal(controls.layout.classList.contains("has-detail"), false);

  controls.statusFilter.value = "qualified";
  controls.statusFilter.dispatchEvent({ type: "change" });
  assert.equal(controls.queue.querySelectorAll(".screening-card").length, 1);
  assert.equal(controls.queue.querySelectorAll(".status-column").length, 1);
  assert.equal(controls.queue.classList.contains("is-filtered"), true);
  assert.equal(controls.status.textContent, "1 of 2 screening(s)");
  controls.statusFilter.value = "";
  controls.statusFilter.dispatchEvent({ type: "change" });
  assert.equal(controls.queue.querySelectorAll(".status-column").length, 6);
  assert.equal(controls.queue.classList.contains("is-filtered"), false);

  const emptyControls = await loadRecruiter(async () => response({ items: [] }), { innerWidth: 1200 });
  emptyControls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();
  assert.equal(emptyControls.queue.querySelector(".queue-empty").textContent, "No screenings yetNew candidate screenings will appear here.");
  assert.equal(emptyControls.queue.querySelector(".queue-empty").getAttribute("role"), "status");
});

test("detail presentation keeps data safe, labels transcript speakers, and exposes disclosures", async () => {
  const controls = await loadRecruiter(async (url) => {
    if (url.includes("/screenings/session-detail")) return response({
      session_id: "session-detail",
      candidate_name: "Candidate <b>unsafe</b>",
      status: "needs_review",
      summary: "Summary <img src=x onerror=alert(1)>",
      summary_status: "pending",
      handoff_status: "ready",
      decision: {
        reason_codes: ["ambiguous_location"],
        missing_fields: [],
        rule_trace: { decision_rule: "location_uncertain" },
      },
      messages: [
        { direction: "assistant", content: "Olivia <em>message</em>" },
        { direction: "user", content: "Candidate <em>message</em>" },
      ],
    });
    return response({ items: [{ session_id: "session-detail", status: "needs_review" }] });
  });
  controls.key.value = "secret";
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();
  controls.queue.querySelector(".screening-card__action").dispatchEvent({ type: "click" });
  await flush();

  assert.equal(controls.detailTitle.querySelector(".detail-title__status").textContent, "Needs review");
  assert.equal(controls.detailContent.querySelector(".detail-summary__text").textContent, "Summary <img src=x onerror=alert(1)>");
  assert.equal(controls.detailContent.querySelector(".rule-trace").querySelector("pre").textContent.includes("location_uncertain"), true);
  const assistantLine = controls.detailContent.querySelector(".transcript-line--assistant");
  const candidateLine = controls.detailContent.querySelector(".transcript-line--candidate");
  assert.equal(assistantLine.querySelector(".transcript-line__speaker").textContent, "Olivia");
  assert.equal(candidateLine.querySelector(".transcript-line__speaker").textContent, "Candidate");
  assert.equal(assistantLine.querySelector(".transcript-line__content").children.length, 0);
  assert.equal(controls.retry.hidden, false);
  assert.equal(controls.detail.getAttribute("aria-busy"), "false");
});

test("queue loading exposes a busy state until the request settles", async () => {
  const request = deferred();
  const controls = await loadRecruiter(() => request.promise);
  assert.equal(controls.statusFilter.disabled, true);
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });

  assert.equal(controls.queue.getAttribute("aria-busy"), "true");
  assert.equal(controls.queue.classList.contains("is-loading"), true);
  assert.equal(controls.status.textContent, "Loading…");
  assert.equal(controls.status.classList.contains("status-message--loading"), true);

  request.resolve(response({ items: [] }));
  await flush();
  assert.equal(controls.queue.getAttribute("aria-busy"), "false");
  assert.equal(controls.queue.classList.contains("is-loading"), false);
  assert.equal(controls.statusFilter.disabled, false);
});

test("status filter cannot render the initial empty queue before data loads", async () => {
  const request = deferred();
  const controls = await loadRecruiter(() => request.promise);

  controls.statusFilter.value = "qualified";
  controls.statusFilter.dispatchEvent({ type: "change" });

  assert.equal(controls.queue.children.length, 0);
  assert.equal(controls.queue.querySelector(".queue-empty"), null);
});

test("invalid queue payload renders an error instead of a false empty state", async () => {
  const controls = await loadRecruiter(async () => response({ screenings: [] }));
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();

  const error = controls.queue.querySelector(".queue-message--error");
  assert.equal(error.textContent, "Queue response was invalid.");
  assert.equal(controls.queue.querySelector(".queue-empty"), null);
  assert.equal(controls.statusFilter.disabled, true);
});

test("queue errors render an accessible message and restore the idle state", async () => {
  const controls = await loadRecruiter(async () => response({ error: { message: "Invalid internal key" } }, false));
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();

  const error = controls.queue.querySelector(".queue-message--error");
  assert.equal(error.textContent, "Invalid internal key");
  assert.equal(error.getAttribute("role"), "alert");
  assert.equal(controls.queue.getAttribute("aria-busy"), "false");
  assert.equal(controls.queue.classList.contains("is-loading"), false);
  assert.equal(controls.status.textContent, "");
  assert.equal(controls.status.classList.contains("status-message--error"), false);
});

test("detail errors hide review controls and restore the detail busy state", async () => {
  const controls = await loadRecruiter(async (url) => {
    if (url.includes("/screenings/session-error")) {
      return response({ error: { message: "Screening no longer exists" } }, false);
    }
    return response({ items: [{ session_id: "session-error", candidate_name: "Candidate", status: "in_progress" }] });
  });
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();
  controls.queue.querySelector(".screening-card__action").dispatchEvent({ type: "click" });
  await flush();

  const error = controls.detailContent.querySelector(".detail-message--error");
  assert.equal(error.textContent, "Screening no longer exists");
  assert.equal(controls.detail.hidden, false);
  assert.equal(controls.review.hidden, true);
  assert.equal(controls.retry.hidden, true);
  assert.equal(controls.detail.getAttribute("aria-busy"), "false");
  assert.equal(controls.detail.classList.contains("is-loading"), false);
});

test("recruiter endpoints, review payload, and non-persistence invariants remain unchanged", async () => {
  const recruiter = await readFile(new URL("static/recruiter.js", root), "utf8");
  const styles = await readFile(new URL("static/recruiter.css", root), "utf8");
  assert.match(recruiter, /\/api\/v1\/internal\/screenings\?limit=100/);
  assert.match(recruiter, /\/summary\/retry/);
  assert.match(recruiter, /\/reviews/);
  assert.match(recruiter, /Authorization: `Bearer \$\{keyInput\.value\}`/);
  assert.match(recruiter, /reviewer_id: reviewerId\.value\.trim\(\)/);
  assert.match(recruiter, /decision: reviewDecision\.value/);
  assert.match(recruiter, /notes: reviewNotes\.value\.trim\(\) \|\| null/);
  assert.doesNotMatch(recruiter, /localStorage|sessionStorage/);
  assert.match(recruiter, /\$\{className\}--\$\{presentation\.tone\}/);
  assert.match(recruiter, /tone: "neutral"/);
  assert.match(styles, /--recruiter-qualified-bg:\s*#e7f4df/);
  assert.match(styles, /--recruiter-review-bg:\s*#fff4d7/);
  assert.match(styles, /--recruiter-disqualified-bg:\s*#f8e7e4/);
  assert.match(styles, /--recruiter-progress-bg:\s*#e7ece2/);
  assert.match(styles, /--recruiter-abandoned-bg:\s*#ece8e1/);
  assert.doesNotMatch(styles, /var\(--ui-(qualified|review|disqualified|progress|abandoned|error)/);
});
