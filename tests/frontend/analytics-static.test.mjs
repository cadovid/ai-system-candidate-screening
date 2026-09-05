import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const root = new URL("../../", import.meta.url);

class FakeElement {
  constructor(ownerDocument, tagName) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.style = {};
    this.className = "";
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

  append(...nodes) {
    for (const node of nodes) this.appendChild(node);
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    this._textContent = "";
    return child;
  }

  replaceChildren(...children) {
    this.children = [];
    this._textContent = "";
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) { return this.attributes.get(name) ?? null; }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
  }

  querySelectorAll(selector) {
    const wanted = selector.split(",").map((part) => part.trim().toUpperCase());
    const result = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (wanted.includes(child.tagName)) result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }
}

class FakeDocument {
  constructor() { this.ids = new Map(); }

  createElement(tagName) { return new FakeElement(this, tagName); }

  getElementById(id) { return this.ids.get(id) || null; }

  register(id, tagName) {
    const element = this.createElement(tagName);
    this.ids.set(id, element);
    return element;
  }
}

function buildDocument() {
  const document = new FakeDocument();
  const form = document.register("analytics-form", "form");
  const keyInput = document.register("internal-key", "input");
  const status = document.register("analytics-status", "p");
  const dashboard = document.register("analytics-dashboard", "section");
  dashboard.hidden = true;
  const headlineMetrics = document.register("headline-metrics", "div");
  const statusBreakdown = document.register("status-breakdown", "div");
  const workflowMetrics = document.register("workflow-metrics", "dl");
  const languageBreakdown = document.register("language-breakdown", "div");
  const modeBreakdown = document.register("mode-breakdown", "div");
  const operationsMetrics = document.register("operations-metrics", "dl");
  const dropoffBreakdown = document.register("dropoff-breakdown", "div");
  const reasonBreakdown = document.register("reason-breakdown", "div");
  const eventBreakdown = document.register("event-breakdown", "div");
  const clarificationBreakdown = document.register("clarification-breakdown", "div");
  form.appendChild(keyInput);
  return {
    document,
    form,
    keyInput,
    status,
    dashboard,
    headlineMetrics,
    statusBreakdown,
    workflowMetrics,
    languageBreakdown,
    modeBreakdown,
    operationsMetrics,
    dropoffBreakdown,
    reasonBreakdown,
    eventBreakdown,
    clarificationBreakdown,
  };
}

function response(payload, ok = true) {
  return { ok, json: async () => payload };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function loadAnalytics(fetchImpl) {
  const controls = buildDocument();
  const context = vm.createContext({
    document: controls.document,
    fetch: fetchImpl,
    Promise,
    Number,
    String,
    Error,
    setImmediate,
  });
  const source = await readFile(new URL("static/analytics.js", root), "utf8");
  vm.runInContext(source, context);
  return controls;
}

const analyticsPayload = {
  total_screenings: 12,
  status_counts: { qualified: 4, in_progress: 3, needs_review: 2, disqualified: 2, abandoned: 1 },
  total_turns: 44,
  completed_turns: 40,
  failed_turns: 4,
  average_turn_latency_ms: 620,
  summaries_generated: 7,
  summaries_fallback: 1,
  handoffs_ready: 6,
  reviews_recorded: 3,
  screenings_started: 12,
  screenings_completed: 9,
  screenings_in_progress: 3,
  screenings_abandoned: 1,
  qualified: 4,
  disqualified: 2,
  needs_review: 2,
  average_completed_messages: 6.5,
  average_completed_turns: 5.5,
  average_screening_duration_seconds: 184,
  language_distribution: { es: 8, en: 4 },
  interaction_mode_distribution: { text: 10, voice: 2 },
  faq_usage_count: 5,
  guardrail_blocks: 1,
  reengagements_sent: 2,
  reengagements_suppressed: 1,
  dropoff_stage_distribution: { location: 2 },
  disqualification_reason_distribution: { outside_service_area: 2 },
  event_counts: { turn_completed: 40 },
  clarification_retry_counts: { location: 3 },
  clarification_retry_count: 3,
  summaries_pending: 0,
  turns_started: 44,
  total_events: 40,
  opted_out: 1,
};

test("analytics page declares the protected dashboard contract", async () => {
  const template = await readFile(new URL("templates/analytics.html", root), "utf8");
  const styles = await readFile(new URL("static/analytics.css", root), "utf8");
  const script = await readFile(new URL("static/analytics.js", root), "utf8");
  assert.match(template, /class="analytics-page"/);
  assert.match(template, /id="analytics-form"/);
  assert.match(template, /id="analytics-dashboard"/);
  assert.match(template, /href="\/recruiter"/);
  assert.match(template, /src="\/static\/analytics\.js"/);
  assert.match(script, /\/api\/v1\/internal\/analytics/);
  assert.match(script, /Authorization: `Bearer \$\{keyInput\.value\}`/);
  assert.doesNotMatch(script, /localStorage|sessionStorage/);
  assert.match(styles, /\.analytics-page/);
});

test("successful analytics load renders aggregate metrics and uses the internal key", async () => {
  const calls = [];
  const controls = await loadAnalytics(async (url, options) => {
    calls.push({ url, options });
    return response(analyticsPayload);
  });
  controls.keyInput.value = "secret";
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/internal/analytics");
  assert.equal(calls[0].options.headers.Authorization, "Bearer secret");
  assert.equal(controls.dashboard.hidden, false);
  assert.equal(controls.headlineMetrics.children.length, 8);
  assert.match(controls.headlineMetrics.textContent, /Total screenings/);
  assert.match(controls.headlineMetrics.textContent, /12/);
  assert.match(controls.statusBreakdown.textContent, /Qualified/);
  assert.match(controls.workflowMetrics.textContent, /Ready handoffs/);
  assert.match(controls.languageBreakdown.textContent, /Spanish/);
  assert.match(controls.modeBreakdown.textContent, /Voice/);
  assert.match(controls.eventBreakdown.textContent, /Turn Completed/);
  assert.match(controls.clarificationBreakdown.textContent, /Location/);
  assert.equal(controls.status.textContent, "Analytics loaded.");
  assert.equal(controls.status.dataset.state, "success");
});

test("analytics errors hide stale data and expose a safe status", async () => {
  const controls = await loadAnalytics(async () => response({ error: { message: "Invalid internal key" } }, false));
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();

  assert.equal(controls.dashboard.hidden, true);
  assert.equal(controls.status.textContent, "Invalid internal key");
  assert.equal(controls.status.dataset.state, "error");
});

test("malformed analytics payloads do not render a misleading dashboard", async () => {
  const controls = await loadAnalytics(async () => response({ unexpected: true }));
  controls.form.dispatchEvent({ type: "submit", preventDefault() {} });
  await flush();

  assert.equal(controls.dashboard.hidden, true);
  assert.equal(controls.status.textContent, "Analytics response was invalid.");
  assert.equal(controls.status.dataset.state, "error");
});
