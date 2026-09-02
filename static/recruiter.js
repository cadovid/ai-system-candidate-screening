(() => {
  "use strict";

  const form = document.getElementById("recruiter-form");
  const keyInput = document.getElementById("internal-key");
  const status = document.getElementById("recruiter-status");
  const queue = document.getElementById("queue");
  const detail = document.getElementById("detail");
  const detailTitle = document.getElementById("detail-title");
  const detailContent = document.getElementById("detail-content");
  const retrySummary = document.getElementById("retry-summary");
  const reviewForm = document.getElementById("review-form");
  const reviewerId = document.getElementById("reviewer-id");
  const reviewDecision = document.getElementById("review-decision");
  const reviewNotes = document.getElementById("review-notes");
  const reviewStatus = document.getElementById("review-status");
  let selectedSessionId = null;

  function headers() {
    return { Authorization: `Bearer ${keyInput.value}` };
  }

  async function jsonResponse(response) {
    return response.json().catch(() => ({}));
  }

  function errorText(data, fallback) {
    return data && data.error && data.error.message ? data.error.message : fallback;
  }

  function paragraph(label, value) {
    const row = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = `${label}: `;
    row.append(strong, document.createTextNode(value == null ? "—" : String(value)));
    return row;
  }

  function renderQueue(items) {
    queue.replaceChildren();
    for (const item of items) {
      const card = document.createElement("article");
      card.className = "card";
      const title = document.createElement("h2");
      title.textContent = item.candidate_name || "Candidate";
      const state = document.createElement("p");
      state.className = "status";
      state.textContent = `${item.status} · ${item.session_id}`;
      const summary = document.createElement("p");
      summary.textContent = item.summary || `Summary ${item.summary_status || "pending"}`;
      const action = document.createElement("button");
      action.type = "button";
      action.className = "secondary";
      action.textContent = "View details";
      action.addEventListener("click", () => { void loadDetail(item.session_id); });
      card.append(title, state, summary, action);
      queue.appendChild(card);
    }
  }

  function renderDetail(item) {
    detailTitle.textContent = item.candidate_name || "Screening details";
    detailContent.replaceChildren(
      paragraph("Status", item.status),
      paragraph("Summary status", item.summary_status || "pending"),
      paragraph("Handoff", item.handoff_status || "—"),
      paragraph("Summary", item.summary || "No summary is available yet."),
    );
    if (item.decision) {
      detailContent.append(
        paragraph("Reason codes", (item.decision.reason_codes || []).join(", ") || "none"),
        paragraph("Missing fields", (item.decision.missing_fields || []).join(", ") || "none"),
      );
      const trace = document.createElement("details");
      const traceSummary = document.createElement("summary");
      traceSummary.textContent = "Deterministic rule trace";
      const pre = document.createElement("pre");
      pre.textContent = JSON.stringify(item.decision.rule_trace || {}, null, 2);
      trace.append(traceSummary, pre);
      detailContent.appendChild(trace);
    }
    const messages = document.createElement("details");
    const messagesSummary = document.createElement("summary");
    messagesSummary.textContent = `Transcript (${(item.messages || []).length} messages)`;
    for (const entry of item.messages || []) {
      const line = document.createElement("p");
      line.className = `transcript-line ${entry.direction || ""}`;
      line.textContent = `${entry.direction || "message"}: ${entry.content || ""}`;
      messages.appendChild(line);
    }
    messages.prepend(messagesSummary);
    detailContent.appendChild(messages);
    retrySummary.hidden = item.summary_status !== "pending";
    reviewForm.hidden = false;
    reviewStatus.textContent = "";
    detail.hidden = false;
  }

  async function loadDetail(sessionId) {
    selectedSessionId = sessionId;
    detail.hidden = false;
    detailTitle.textContent = "Loading details…";
    detailContent.replaceChildren();
    try {
      const response = await fetch(`/api/v1/internal/screenings/${encodeURIComponent(sessionId)}`, { headers: headers() });
      const data = await jsonResponse(response);
      if (!response.ok) throw new Error(errorText(data, "Unable to load screening details."));
      renderDetail(data);
    } catch (error) {
      detailTitle.textContent = "Screening details";
      detailContent.textContent = error instanceof Error ? error.message : "Unable to load screening details.";
    }
  }

  async function loadQueue() {
    queue.replaceChildren();
    status.textContent = "Loading…";
    const response = await fetch("/api/v1/internal/screenings?limit=100", { headers: headers() });
    const data = await jsonResponse(response);
    if (!response.ok) throw new Error(errorText(data, "Unable to load queue."));
    renderQueue(data.items || []);
    status.textContent = `${(data.items || []).length} screening(s)`;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void loadQueue().catch((error) => {
      status.textContent = error instanceof Error ? error.message : "Unable to load queue.";
    });
  });

  retrySummary.addEventListener("click", async () => {
    if (!selectedSessionId) return;
    retrySummary.disabled = true;
    reviewStatus.textContent = "Retrying summary…";
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
      reviewStatus.textContent = error instanceof Error ? error.message : "Unable to retry summary.";
    } finally {
      retrySummary.disabled = false;
    }
  });

  reviewForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!selectedSessionId) return;
    reviewStatus.textContent = "Saving review…";
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
      reviewStatus.textContent = "Review saved.";
      await loadDetail(selectedSessionId);
      await loadQueue();
    } catch (error) {
      reviewStatus.textContent = error instanceof Error ? error.message : "Unable to save review.";
    }
  });
})();
