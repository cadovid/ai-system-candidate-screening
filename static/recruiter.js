(() => {
  "use strict";

  const form = document.getElementById("recruiter-form");
  const keyInput = document.getElementById("internal-key");
  const status = document.getElementById("recruiter-status");
  const queue = document.getElementById("queue");
  const statusFilter = document.getElementById("status-filter");
  const detail = document.getElementById("detail");
  const reviewLayout = detail?.closest(".review-layout") || document.querySelector(".review-layout");
  const detailTitle = document.getElementById("detail-title");
  const detailContent = document.getElementById("detail-content");
  const closeDetail = document.getElementById("close-detail");
  const retrySummary = document.getElementById("retry-summary");
  const reviewForm = document.getElementById("review-form");
  const reviewerId = document.getElementById("reviewer-id");
  const reviewDecision = document.getElementById("review-decision");
  const reviewNotes = document.getElementById("review-notes");
  const reviewStatus = document.getElementById("review-status");
  let selectedSessionId = null;
  let queueItems = [];
  let queueLoaded = false;

  // The filter must remain unavailable until a valid queue response has been
  // rendered. Otherwise a fast interaction during the initial request can
  // render the empty initial `queueItems` array as if the queue were empty.
  if (statusFilter) statusFilter.disabled = true;

  const STATUS_PRESENTATION = Object.freeze({
    in_progress: Object.freeze({ label: "In progress", tone: "in-progress" }),
    qualified: Object.freeze({ label: "Qualified", tone: "qualified" }),
    disqualified: Object.freeze({ label: "Disqualified", tone: "disqualified" }),
    needs_review: Object.freeze({ label: "Needs review", tone: "needs-review" }),
    abandoned: Object.freeze({ label: "Abandoned", tone: "abandoned" }),
    error: Object.freeze({ label: "Error", tone: "error" }),
  });

  const STATUS_COLUMNS = Object.freeze([
    "in_progress",
    "needs_review",
    "qualified",
    "disqualified",
    "abandoned",
  ]);

  function headers() {
    return { Authorization: `Bearer ${keyInput.value}` };
  }

  async function jsonResponse(response) {
    return response.json().catch(() => ({}));
  }

  function errorText(data, fallback) {
    return data && data.error && data.error.message ? data.error.message : fallback;
  }

  function humanize(value, fallback = "Unknown") {
    const normalized = String(value ?? "").trim().replace(/[_-]+/g, " ");
    if (!normalized) return fallback;
    return normalized.replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function statusPresentation(value) {
    const key = String(value ?? "").trim().toLowerCase();
    const known = STATUS_PRESENTATION[key];
    if (known) return { key, ...known };
    return { key: "unknown", label: humanize(key), tone: "neutral" };
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function statusBadge(value, className = "status-badge") {
    const presentation = statusPresentation(value);
    const badge = element(
      "span",
      `${className} ${className}--${presentation.tone}`,
      presentation.label,
    );
    badge.dataset.status = presentation.key;
    badge.setAttribute("aria-label", `Status: ${presentation.label}`);
    return badge;
  }

  function setMessage(node, message, tone = "") {
    if (!node) return;
    node.classList.add("status-message");
    for (const state of ["loading", "success", "error", "muted"]) {
      node.classList.remove(`status-message--${state}`);
    }
    if (tone) node.classList.add(`status-message--${tone}`);
    node.dataset.state = tone;
    node.textContent = message || "";
  }

  function setBusy(node, busy) {
    if (!node) return;
    node.setAttribute("aria-busy", String(Boolean(busy)));
    node.classList.toggle("is-loading", Boolean(busy));
  }

  function sessionIdFor(item) {
    return String(item?.session_id ?? "");
  }

  function isSelected(item) {
    return selectedSessionId !== null && sessionIdFor(item) === String(selectedSessionId);
  }

  function selectedStatusFilter() {
    return String(statusFilter?.value ?? "").trim().toLowerCase();
  }

  function filteredQueueItems(items) {
    const filter = selectedStatusFilter();
    if (!filter) return items;
    return items.filter((item) => statusPresentation(item?.status).key === filter);
  }

  function updateQueueCount(total, visible) {
    if (!status) return;
    const filter = selectedStatusFilter();
    const message = filter && visible !== total
      ? `${visible} of ${total} screening(s)`
      : `${total} screening(s)`;
    setMessage(status, message, "success");
  }

  function syncSelectedCard() {
    const cards = queue.querySelectorAll("[data-session-id]");
    for (const card of cards) {
      const selected = selectedSessionId !== null && card.dataset.sessionId === String(selectedSessionId);
      card.classList.toggle("is-selected", selected);
      if (selected) card.setAttribute("aria-current", "true");
      else card.removeAttribute("aria-current");
    }
  }

  function renderQueueMessage(message, tone) {
    queue.replaceChildren();
    queue.classList.toggle("is-empty", tone === "empty");
    const node = element("div", `queue-message queue-message--${tone}`, message);
    if (tone === "error") node.setAttribute("role", "alert");
    else node.setAttribute("role", "status");
    queue.appendChild(node);
  }

  function renderQueue(items) {
    const screenings = Array.isArray(items) ? items : [];
    queueItems = screenings;
    const visibleScreenings = filteredQueueItems(screenings);
    queue.replaceChildren();
    queue.classList.toggle("is-empty", screenings.length === 0 || visibleScreenings.length === 0);
    queue.classList.toggle("is-filtered", Boolean(selectedStatusFilter()));

    if (!screenings.length) {
      const empty = element("div", "queue-empty");
      empty.setAttribute("role", "status");
      empty.append(
        element("h2", "queue-empty__title", "No screenings yet"),
        element("p", "queue-empty__copy", "New candidate screenings will appear here."),
      );
      queue.appendChild(empty);
      return;
    }

    if (!visibleScreenings.length) {
      const filteredEmpty = element("div", "queue-filter-empty");
      filteredEmpty.setAttribute("role", "status");
      filteredEmpty.append(
        element("strong", "queue-filter-empty__title", "No screenings in this status"),
        element("span", "queue-filter-empty__copy", "Choose another status to see more candidates."),
      );
      queue.appendChild(filteredEmpty);
      updateQueueCount(screenings.length, 0);
      return;
    }

    const filter = selectedStatusFilter();
    const columnKeys = filter ? [filter] : STATUS_COLUMNS;
    const grouped = new Map(columnKeys.map((key) => [key, []]));
    const unknown = [];
    for (const item of visibleScreenings) {
      const key = statusPresentation(item?.status).key;
      if (grouped.has(key)) grouped.get(key).push(item);
      else unknown.push(item);
    }
    if (!filter && unknown.length) grouped.set("unknown", unknown);

    for (const [columnKey, columnItems] of grouped) {
      const presentation = statusPresentation(columnKey);
      const column = element("section", `status-column status-column--${presentation.tone}`);
      column.setAttribute("aria-labelledby", `status-column-${columnKey}`);
      const columnHeader = element("header", "status-column__header");
      const columnTitle = element("h2", "status-column__title", presentation.label);
      columnTitle.id = `status-column-${columnKey}`;
      columnHeader.append(columnTitle, element("span", "status-column__count", String(columnItems.length)));
      const cards = element("div", "status-column__cards");
      if (!columnItems.length) {
        cards.appendChild(element("p", "status-column__empty", "No screenings"));
      } else {
        for (const item of columnItems) cards.appendChild(renderScreeningCard(item));
      }
      column.append(columnHeader, cards);
      queue.appendChild(column);
    }
    updateQueueCount(screenings.length, visibleScreenings.length);
  }

  function renderScreeningCard(item) {
    const sessionId = sessionIdFor(item);
    const card = element("article", "card screening-card");
    card.dataset.sessionId = sessionId;
    card.setAttribute("data-session-id", sessionId);
    if (isSelected(item)) {
      card.classList.add("is-selected");
      card.setAttribute("aria-current", "true");
    }

    const header = element("div", "screening-card__header");
    const title = element("h3", "screening-card__name", item.candidate_name || "Candidate");
    header.append(title, statusBadge(item.status));

    const metadata = element("p", "screening-card__meta");
    metadata.append(
      element("span", "screening-card__meta-label", "Session"),
      document.createTextNode(sessionId || "—"),
    );

    const summaryText = item.summary || `Summary ${humanize(item.summary_status || "pending", "pending").toLowerCase()}`;
    const summary = element("p", "screening-card__summary", summaryText);
    const action = element("button", "secondary screening-card__action", "View details");
    action.type = "button";
    action.dataset.sessionId = sessionId;
    action.setAttribute("aria-controls", "detail");
    action.addEventListener("click", () => { void loadDetail(sessionId); });

    card.append(header, metadata, summary, action);
    return card;
  }

  function detailMetaRow(label, value) {
    const row = element("div", "detail-meta__item");
    const term = element("dt", "detail-meta__label", label);
    const description = element("dd", "detail-meta__value", value == null || value === "" ? "—" : String(value));
    row.append(term, description);
    return row;
  }

  function detailSection(title, className = "detail-section") {
    const section = element("section", className);
    section.appendChild(element("h3", "detail-section__title", title));
    return section;
  }

  function listValue(value) {
    return Array.isArray(value) ? value.filter((entry) => entry !== null && entry !== undefined && entry !== "") : [];
  }

  function chipList(label, values) {
    const row = element("div", "detail-data-row");
    const title = element("dt", "detail-data-row__label", label);
    const content = element("dd", "detail-data-row__value");
    const valuesList = listValue(values);
    if (!valuesList.length) {
      content.appendChild(element("span", "detail-data-row__empty", "none"));
    } else {
      for (const value of valuesList) content.appendChild(element("span", "data-chip", String(value)));
    }
    row.append(title, content);
    return row;
  }

  function safeJson(value) {
    try {
      return JSON.stringify(value || {}, null, 2) || "{}";
    } catch (_error) {
      return "{}";
    }
  }

  function renderTranscript(messages) {
    const entries = Array.isArray(messages) ? messages : [];
    const transcript = document.createElement("details");
    transcript.className = "detail-disclosure transcript-disclosure";
    const transcriptSummary = element("summary", "detail-disclosure__summary", `Transcript (${entries.length} messages)`);
    const transcriptBody = element("div", "transcript");
    for (const entry of entries) {
      const direction = String(entry?.direction ?? "").trim().toLowerCase();
      const isAssistant = direction === "assistant";
      const isCandidate = direction === "user" || direction === "candidate";
      const speaker = isAssistant ? "Olivia" : isCandidate ? "Candidate" : "Message";
      const lineTone = isAssistant ? "assistant" : isCandidate ? "candidate" : "message";
      const line = element("div", `transcript-line transcript-line--${lineTone}`);
      line.append(
        element("span", "transcript-line__speaker", speaker),
        element("p", "transcript-line__content", entry?.content || ""),
      );
      transcriptBody.appendChild(line);
    }
    transcript.append(transcriptSummary, transcriptBody);
    return transcript;
  }

  function renderDetail(item) {
    const presentation = statusPresentation(item?.status);
    detailTitle.className = "detail-title";
    detailTitle.replaceChildren(
      document.createTextNode(item?.candidate_name || "Screening details"),
      statusBadge(item?.status, "detail-title__status"),
    );

    const summarySection = detailSection("Summary", "detail-section detail-summary");
    summarySection.appendChild(element("p", "detail-summary__text", item?.summary || "No summary is available yet."));

    const metadata = element("dl", "detail-meta");
    metadata.append(
      detailMetaRow("Status", presentation.label),
      detailMetaRow("Summary status", humanize(item?.summary_status || "pending")),
      detailMetaRow("Handoff", humanize(item?.handoff_status || "—")),
    );

    detailContent.replaceChildren(summarySection, metadata);

    if (item?.decision) {
      const decision = detailSection("Decision details");
      const decisionData = element("dl", "detail-data");
      decisionData.append(
        chipList("Reason codes", item.decision.reason_codes),
        chipList("Missing fields", item.decision.missing_fields),
      );
      decision.appendChild(decisionData);

      const trace = document.createElement("details");
      trace.className = "detail-disclosure rule-trace";
      trace.append(
        element("summary", "detail-disclosure__summary", "Deterministic rule trace"),
        element("pre", "rule-trace__content", safeJson(item.decision.rule_trace)),
      );
      decision.appendChild(trace);
      detailContent.appendChild(decision);
    }

    detailContent.appendChild(renderTranscript(item?.messages));
    retrySummary.hidden = item?.summary_status !== "pending";
    reviewForm.hidden = false;
    setMessage(reviewStatus, "", "");
    setDetailVisible(true);
    setBusy(detail, false);
  }

  function renderDetailError(message) {
    detailTitle.className = "detail-title detail-title--error";
    detailTitle.textContent = "Screening details";
    detailContent.replaceChildren(element("p", "detail-message detail-message--error", message));
    retrySummary.hidden = true;
    reviewForm.hidden = true;
    setMessage(reviewStatus, "", "");
  }

  function setDetailVisible(visible) {
    detail.hidden = !visible;
    if (reviewLayout) reviewLayout.classList.toggle("has-detail", visible);
  }

  function closeDetailPanel() {
    const previousSessionId = selectedSessionId;
    selectedSessionId = null;
    setDetailVisible(false);
    detailContent.replaceChildren();
    retrySummary.hidden = true;
    reviewForm.hidden = true;
    setMessage(reviewStatus, "", "");
    syncSelectedCard();
    if (previousSessionId !== null) {
      const selectedCard = [...queue.querySelectorAll("[data-session-id]")]
        .find((card) => card.dataset.sessionId === String(previousSessionId));
      selectedCard?.querySelector(".screening-card__action")?.focus?.();
    }
  }

  function isMobileViewport() {
    if (typeof window === "undefined") return false;
    if (typeof window.matchMedia === "function") {
      const media = window.matchMedia("(max-width: 800px)");
      if (typeof media.matches === "boolean") return media.matches;
    }
    return typeof window.innerWidth === "number" && window.innerWidth <= 800;
  }

  function revealDetailOnMobile() {
    if (!isMobileViewport() || typeof detail.scrollIntoView !== "function") return;
    const reducedMotion = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    detail.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  }

  async function loadDetail(sessionId) {
    selectedSessionId = sessionId;
    syncSelectedCard();
    setDetailVisible(true);
    setBusy(detail, true);
    detailTitle.className = "detail-title detail-title--loading";
    detailTitle.textContent = "Loading details…";
    detailContent.replaceChildren();
    retrySummary.hidden = true;
    reviewForm.hidden = true;
    setMessage(reviewStatus, "", "");
    revealDetailOnMobile();
    try {
      const response = await fetch(`/api/v1/internal/screenings/${encodeURIComponent(sessionId)}`, { headers: headers() });
      const data = await jsonResponse(response);
      if (!response.ok) throw new Error(errorText(data, "Unable to load screening details."));
      renderDetail(data);
    } catch (error) {
      renderDetailError(error instanceof Error ? error.message : "Unable to load screening details.");
    } finally {
      setBusy(detail, false);
    }
  }

  async function loadQueue() {
    queueLoaded = false;
    if (statusFilter) statusFilter.disabled = true;
    queue.replaceChildren();
    queue.classList.remove("is-empty");
    setBusy(queue, true);
    setMessage(status, "Loading…", "loading");
    try {
      const response = await fetch("/api/v1/internal/screenings?limit=100", { headers: headers() });
      const data = await jsonResponse(response);
      if (!response.ok) throw new Error(errorText(data, "Unable to load queue."));
      if (!Array.isArray(data.items)) throw new Error("Queue response was invalid.");
      const items = data.items;
      renderQueue(items);
      queueLoaded = true;
      if (statusFilter) statusFilter.disabled = false;
      setMessage(status, `${items.length} screening(s)`, "success");
      syncSelectedCard();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unable to load queue.";
      queueLoaded = false;
      if (statusFilter) statusFilter.disabled = true;
      renderQueueMessage(message, "error");
      setMessage(status, "", "");
      throw error;
    } finally {
      setBusy(queue, false);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void loadQueue().catch(() => {});
  });

  statusFilter?.addEventListener("change", () => {
    if (!queueLoaded) return;
    renderQueue(queueItems);
    syncSelectedCard();
  });

  closeDetail?.addEventListener("click", closeDetailPanel);

  retrySummary.addEventListener("click", async () => {
    if (!selectedSessionId) return;
    retrySummary.disabled = true;
    retrySummary.setAttribute("aria-busy", "true");
    setMessage(reviewStatus, "Retrying summary…", "loading");
    try {
      const response = await fetch(`/api/v1/internal/screenings/${encodeURIComponent(selectedSessionId)}/summary/retry`, {
        method: "POST",
        headers: headers(),
      });
      const data = await jsonResponse(response);
      if (!response.ok) throw new Error(errorText(data, "Unable to retry summary."));
      renderDetail(data);
      await loadQueue();
    } catch (error) {
      setMessage(reviewStatus, error instanceof Error ? error.message : "Unable to retry summary.", "error");
    } finally {
      retrySummary.disabled = false;
      retrySummary.setAttribute("aria-busy", "false");
    }
  });

  reviewForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!selectedSessionId) return;
    setBusy(reviewForm, true);
    setMessage(reviewStatus, "Saving review…", "loading");
    try {
      const response = await fetch(`/api/v1/internal/screenings/${encodeURIComponent(selectedSessionId)}/reviews`, {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({
          reviewer_id: reviewerId.value.trim(),
          decision: reviewDecision.value,
          notes: reviewNotes.value.trim() || null,
        }),
      });
      const data = await jsonResponse(response);
      if (!response.ok) throw new Error(errorText(data, "Unable to save review."));
      await loadDetail(selectedSessionId);
      await loadQueue();
      setMessage(reviewStatus, "Review saved.", "success");
    } catch (error) {
      setMessage(reviewStatus, error instanceof Error ? error.message : "Unable to save review.", "error");
    } finally {
      setBusy(reviewForm, false);
    }
  });
})();
