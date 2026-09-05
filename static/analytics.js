(() => {
  "use strict";

  const form = document.getElementById("analytics-form");
  const keyInput = document.getElementById("internal-key");
  const status = document.getElementById("analytics-status");
  const dashboard = document.getElementById("analytics-dashboard");
  const headlineMetrics = document.getElementById("headline-metrics");
  const statusBreakdown = document.getElementById("status-breakdown");
  const workflowMetrics = document.getElementById("workflow-metrics");
  const languageBreakdown = document.getElementById("language-breakdown");
  const modeBreakdown = document.getElementById("mode-breakdown");
  const operationsMetrics = document.getElementById("operations-metrics");
  const dropoffBreakdown = document.getElementById("dropoff-breakdown");
  const reasonBreakdown = document.getElementById("reason-breakdown");
  const eventBreakdown = document.getElementById("event-breakdown");
  const clarificationBreakdown = document.getElementById("clarification-breakdown");

  const STATUS_LABELS = Object.freeze({
    qualified: "Qualified",
    disqualified: "Disqualified",
    needs_review: "Needs review",
    in_progress: "In progress",
    abandoned: "Abandoned",
  });

  const LANGUAGE_LABELS = Object.freeze({ es: "Spanish", en: "English", unknown: "Unknown" });
  const MODE_LABELS = Object.freeze({ text: "Text", voice: "Voice", unknown: "Unknown" });

  function setMessage(message, tone = "") {
    if (!status) return;
    status.textContent = message;
    status.dataset.state = tone;
  }

  function setBusy(busy) {
    if (!form) return;
    form.setAttribute("aria-busy", String(Boolean(busy)));
    for (const control of form.querySelectorAll("input, button")) control.disabled = Boolean(busy);
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatNumber(value, digits = 0) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return number.toLocaleString(undefined, {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  }

  function formatPercent(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${formatNumber(number * 100, 1)}%` : "—";
  }

  function formatDuration(seconds) {
    const number = Number(seconds);
    if (!Number.isFinite(number)) return "—";
    if (number < 60) return `${formatNumber(number, 1)} sec`;
    return `${formatNumber(number / 60, 1)} min`;
  }

  function humanize(value) {
    return String(value ?? "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase()) || "Unknown";
  }

  function clear(node) {
    node?.replaceChildren();
  }

  function renderMetricCards(data) {
    clear(headlineMetrics);
    const cards = [
      ["Total screenings", formatNumber(data.total_screenings), "All persisted sessions"],
      ["Completion rate", formatPercent(data.completion_rate), "Terminal screenings"],
      ["Qualified", formatNumber(data.qualified), "Explicit criteria met"],
      ["Needs review", formatNumber(data.needs_review), "Human attention"],
      ["Disqualified", formatNumber(data.disqualified), "Explicit rule outcome"],
      ["In progress", formatNumber(data.screenings_in_progress), "Still active"],
      ["Average duration", formatDuration(data.average_screening_duration_seconds), "Completed sessions"],
      ["Average turns", formatNumber(data.average_completed_turns, 1), "Completed sessions"],
    ];
    for (const [label, value, detail] of cards) {
      const card = element("article", "metric-card");
      card.append(
        element("p", "metric-card__label", label),
        element("p", "metric-card__value", value),
        element("p", "metric-card__detail", detail),
      );
      headlineMetrics.appendChild(card);
    }
  }

  function renderBarList(node, values, labels = {}) {
    clear(node);
    const entries = Object.entries(values || {}).sort(([, left], [, right]) => Number(right) - Number(left));
    if (!entries.length) {
      node.appendChild(element("p", "analytics-empty", "No data yet."));
      return;
    }
    const maximum = Math.max(...entries.map(([, value]) => Number(value) || 0), 1);
    for (const [key, value] of entries) {
      const row = element("div", "bar-row");
      const track = element("span", "bar-track");
      const fill = element("span", "bar-fill");
      fill.style.width = `${Math.max(0, (Number(value) || 0) / maximum * 100)}%`;
      track.appendChild(fill);
      row.append(
        element("span", "bar-row__label", labels[key] || humanize(key)),
        track,
        element("strong", "bar-row__value", formatNumber(value)),
      );
      node.appendChild(row);
    }
  }

  function renderCompactList(node, values, labels = {}) {
    clear(node);
    const entries = Object.entries(values || {}).sort(([, left], [, right]) => Number(right) - Number(left));
    if (!entries.length) {
      node.appendChild(element("p", "analytics-empty", "No data yet."));
      return;
    }
    for (const [key, value] of entries) {
      const row = element("div", "compact-row");
      row.append(
        element("span", "compact-row__label", labels[key] || humanize(key)),
        element("strong", "compact-row__value", formatNumber(value)),
      );
      node.appendChild(row);
    }
  }

  function renderDefinitionList(node, entries) {
    clear(node);
    for (const [label, value] of entries) {
      const row = element("div", "detail-metric");
      row.append(
        element("dt", "detail-metric__label", label),
        element("dd", "detail-metric__value", value),
      );
      node.appendChild(row);
    }
  }

  function renderDashboard(data) {
    renderMetricCards(data);
    renderBarList(statusBreakdown, data.status_counts, STATUS_LABELS);
    renderDefinitionList(workflowMetrics, [
      ["Started", formatNumber(data.screenings_started)],
      ["Completed", formatNumber(data.screenings_completed)],
      ["In progress", formatNumber(data.screenings_in_progress)],
      ["Abandoned", formatNumber(data.screenings_abandoned)],
      ["Ready handoffs", formatNumber(data.handoffs_ready)],
      ["Recorded reviews", formatNumber(data.reviews_recorded)],
      ["Completed messages", formatNumber(data.average_completed_messages, 1)],
      ["FAQ answers", formatNumber(data.faq_usage_count)],
      ["Opted out", formatNumber(data.opted_out)],
    ]);
    renderCompactList(languageBreakdown, data.language_distribution, LANGUAGE_LABELS);
    renderCompactList(modeBreakdown, data.interaction_mode_distribution, MODE_LABELS);
    renderDefinitionList(operationsMetrics, [
      ["Total turns", formatNumber(data.total_turns)],
      ["Turns started", formatNumber(data.turns_started)],
      ["Completed turns", formatNumber(data.completed_turns)],
      ["Failed turns", formatNumber(data.failed_turns)],
      ["Average turn latency", data.average_turn_latency_ms == null ? "—" : `${formatNumber(data.average_turn_latency_ms, 0)} ms`],
      ["Total audit events", formatNumber(data.total_events)],
      ["Summaries generated", formatNumber(data.summaries_generated)],
      ["Summary fallbacks", formatNumber(data.summaries_fallback)],
      ["Summaries pending", formatNumber(data.summaries_pending)],
      ["Guardrail blocks", formatNumber(data.guardrail_blocks)],
      ["Clarification retries", formatNumber(data.clarification_retry_count)],
      ["Re-engagement sent", formatNumber(data.reengagements_sent)],
      ["Re-engagement suppressed", formatNumber(data.reengagements_suppressed)],
    ]);
    renderCompactList(dropoffBreakdown, data.dropoff_stage_distribution);
    renderCompactList(reasonBreakdown, data.disqualification_reason_distribution);
    renderCompactList(eventBreakdown, data.event_counts);
    renderCompactList(clarificationBreakdown, data.clarification_retry_counts);
    dashboard.hidden = false;
  }

  async function loadAnalytics() {
    setBusy(true);
    setMessage("Loading analytics…", "loading");
    try {
      const response = await fetch("/api/v1/internal/analytics", {
        headers: { Authorization: `Bearer ${keyInput.value}` },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.error?.message || "Unable to load analytics.");
      }
      if (!data || typeof data !== "object" || Array.isArray(data) || typeof data.total_screenings !== "number") {
        throw new Error("Analytics response was invalid.");
      }
      renderDashboard(data);
      setMessage("Analytics loaded.", "success");
    } catch (error) {
      if (dashboard) dashboard.hidden = true;
      setMessage(error instanceof Error ? error.message : "Unable to load analytics.", "error");
    } finally {
      setBusy(false);
    }
  }

  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    void loadAnalytics();
  });
})();
