const output = document.getElementById("output");
const statusList = document.getElementById("statusList");
const uploadForm = document.getElementById("uploadForm");
const processBtn = document.getElementById("processBtn");
const uploadBtn = document.getElementById("uploadBtn");
const exportAuditBtn = document.getElementById("exportAuditBtn");
const actionMessage = document.getElementById("actionMessage");
const statusPulse = document.getElementById("statusPulse");
const metricTracked = document.getElementById("metricTracked");
const metricResolved = document.getElementById("metricResolved");
const metricAttention = document.getElementById("metricAttention");
const timingSummary = document.getElementById("timingSummary");
const metricFilterButtons = Array.from(document.querySelectorAll(".metric-filter"));

const FILE_FIELDS = [
  { inputId: "tickets", formKey: "tickets" },
  { inputId: "customers", formKey: "customers" },
  { inputId: "orders", formKey: "orders" },
  { inputId: "products", formKey: "products" },
  { inputId: "knowledgeBase", formKey: "knowledge_base" },
];

let activeTicketIds = [];
const terminalStates = new Set(["resolved", "escalated", "dead_letter", "failed"]);
const statusStore = new Map();
let isUploadBusy = false;
let isProcessBusy = false;
let isExportBusy = false;
let activeMetricFilter = "all";
let processingStartedAtMs = null;

function normalizeApiBase(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) {
    return "";
  }
  return trimmed.replace(/\/+$/, "");
}

function apiBase() {
  const runtimeConfigBase = normalizeApiBase(window.__APP_CONFIG__?.apiBaseUrl);
  if (runtimeConfigBase) {
    return runtimeConfigBase;
  }

  const originBase = normalizeApiBase(window.location?.origin);
  if (originBase) {
    return originBase;
  }

  return "http://localhost:8000";
}

function log(message) {
  const existing = output.textContent.trim();
  const lines = existing ? existing.split("\n") : [];
  lines.unshift(`${new Date().toISOString()} ${message}`);
  output.textContent = lines.slice(0, 14).join("\n");
}

function setActionMessage(message, tone = "idle") {
  actionMessage.textContent = message;
  actionMessage.className = `action-message ${tone} full-width`;
}

function getFileInput(inputId) {
  return document.getElementById(inputId);
}

function updateFileClearButtons() {
  const disableFileEditing = isUploadBusy || isProcessBusy;
  FILE_FIELDS.forEach(({ inputId }) => {
    const input = getFileInput(inputId);
    if (!input) {
      return;
    }

    const clearButton = document.querySelector(`.file-clear-btn[data-file-input="${inputId}"]`);
    if (!clearButton) {
      return;
    }

    const hasFile = Boolean(input.files && input.files.length > 0);
    clearButton.disabled = disableFileEditing || !hasFile;
  });
}

function applyButtonState() {
  const disableFileEditing = isUploadBusy || isProcessBusy || isExportBusy;

  uploadBtn.disabled = disableFileEditing;
  processBtn.disabled = disableFileEditing;
  exportAuditBtn.disabled = disableFileEditing;
  uploadBtn.textContent = isUploadBusy ? "Uploading..." : "Upload Files";
  processBtn.textContent = isProcessBusy ? "Processing Tickets..." : "Process Tickets";
  exportAuditBtn.textContent = isExportBusy ? "Exporting..." : "Export Audit JSON";

  FILE_FIELDS.forEach(({ inputId }) => {
    const input = getFileInput(inputId);
    if (input) {
      input.disabled = disableFileEditing;
    }
  });

  updateFileClearButtons();
}

function titleCase(value) {
  return String(value || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function formatDate(value) {
  if (!value) {
    return "n/a";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function toJsonString(payload) {
  return escapeHtml(JSON.stringify(payload ?? {}, null, 2));
}

function isTicketTerminal(status) {
  return terminalStates.has(String(status || "").toLowerCase());
}

function summaryForDisplay(status, action, summary) {
  if (summary && summary.trim()) {
    const actionPrefix = action ? `${titleCase(action)}: ` : "";
    return `${actionPrefix}${summary}`;
  }

  if (["pending", "queued", "processing", "retrying", "started"].includes(status)) {
    return "Agent is still gathering evidence and policies for this ticket.";
  }

  if (status === "dead_letter") {
    return "Ticket moved to dead letter after retry exhaustion. Open logs for the final error details.";
  }

  if (status === "resolved") {
    return "Decision completed successfully.";
  }

  if (status === "escalated") {
    return "Decision escalated to a specialist queue.";
  }

  return "No summary available yet.";
}

function updatePulse(pendingCount) {
  if (pendingCount > 0) {
    statusPulse.textContent = "Processing";
    statusPulse.className = "status-pulse active";
    return;
  }

  if (statusStore.size > 0) {
    statusPulse.textContent = "Completed";
    statusPulse.className = "status-pulse done";
    return;
  }

  statusPulse.textContent = "Idle";
  statusPulse.className = "status-pulse idle";
}

function updateMetrics() {
  let resolvedCount = 0;
  let attentionCount = 0;

  statusStore.forEach((status) => {
    if (status === "resolved") {
      resolvedCount += 1;
    }
    if (["escalated", "dead_letter", "failed"].includes(status)) {
      attentionCount += 1;
    }
  });

  metricTracked.textContent = String(statusStore.size);
  metricResolved.textContent = String(resolvedCount);
  metricAttention.textContent = String(attentionCount);
}

function matchesMetricFilter(status, filter) {
  if (filter === "resolved") {
    return status === "resolved";
  }
  if (filter === "attention") {
    return ["escalated", "dead_letter", "failed"].includes(status);
  }
  return true;
}

function applyTicketFilter() {
  const cards = statusList.querySelectorAll(".status-item");
  cards.forEach((card) => {
    const state = String(card.dataset.state || "pending").toLowerCase();
    card.hidden = !matchesMetricFilter(state, activeMetricFilter);
  });
}

function setMetricFilter(filter) {
  activeMetricFilter = filter;
  metricFilterButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.filter === filter);
  });
  applyTicketFilter();
}

function formatDuration(ms) {
  const totalMs = Math.max(0, Math.round(ms));
  const totalSeconds = Math.floor(totalMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;

  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

function showTimingSummary(totalMs, averageMs, ticketCount) {
  timingSummary.hidden = false;
  timingSummary.textContent =
    `Completed in ${formatDuration(totalMs)}. Average per ticket: ${formatDuration(averageMs)} (${ticketCount} ticket${ticketCount === 1 ? "" : "s"}).`;
}

function resetTimingSummary() {
  timingSummary.hidden = true;
  timingSummary.textContent = "";
}

function uploadSummaryText(payload) {
  const loaded =
    Number(payload.tickets_loaded || 0) +
    Number(payload.customers_loaded || 0) +
    Number(payload.orders_loaded || 0) +
    Number(payload.products_loaded || 0);
  const skipped =
    Number(payload.tickets_skipped_duplicates || 0) +
    Number(payload.customers_skipped_duplicates || 0) +
    Number(payload.orders_skipped_duplicates || 0) +
    Number(payload.products_skipped_duplicates || 0);
  const files = Array.isArray(payload.files_processed) ? payload.files_processed.length : 0;
  return `Upload successful: files=${files}, loaded=${loaded}, duplicates_skipped=${skipped}, kb_chunks=${Number(payload.kb_chunks_loaded || 0)}`;
}

function renderTicketStatus(ticketId, status, taskId, decisionSummary, decisionAction) {
  const existing = document.getElementById(`status-${ticketId}`);
  const normalizedStatus = String(status || "pending").toLowerCase();
  const prettyStatus = titleCase(normalizedStatus);
  const safeTask = taskId || "n/a";
  const safeSummary = summaryForDisplay(normalizedStatus, decisionAction, decisionSummary);
  statusStore.set(ticketId, normalizedStatus);

  if (existing) {
    const stateBadge = existing.querySelector(".state-chip");
    const taskText = existing.querySelector(".task-text");
    const summaryText = existing.querySelector(".summary-text");
    const detailsBtn = existing.querySelector('[data-action="details"]');
    const actionsBtn = existing.querySelector('[data-action="actions"]');
    const ticketExtra = existing.querySelector(".ticket-extra");
    const enableActions = isTicketTerminal(normalizedStatus);
    existing.dataset.state = normalizedStatus;

    stateBadge.textContent = prettyStatus;
    stateBadge.className = `state-chip ${normalizedStatus}`;
    taskText.textContent = `Task: ${safeTask}`;
    summaryText.textContent = safeSummary;
    detailsBtn.disabled = !enableActions;
    actionsBtn.disabled = !enableActions;
    detailsBtn.title = enableActions ? "Show ticket details" : "Available after ticket completion";
    actionsBtn.title = enableActions ? "Show ticket actions" : "Available after ticket completion";
    if (!enableActions) {
      ticketExtra.hidden = true;
      ticketExtra.innerHTML = "";
      ticketExtra.removeAttribute("data-view");
      detailsBtn.classList.remove("active");
      actionsBtn.classList.remove("active");
    } else {
      detailsBtn.classList.toggle(
        "active",
        !ticketExtra.hidden && ticketExtra.dataset.view === "details",
      );
      actionsBtn.classList.toggle(
        "active",
        !ticketExtra.hidden && ticketExtra.dataset.view === "actions",
      );
    }
    applyTicketFilter();
    return;
  }

  const enableActions = isTicketTerminal(normalizedStatus);

  const div = document.createElement("div");
  div.className = "status-item";
  div.id = `status-${ticketId}`;
  div.dataset.state = normalizedStatus;
  div.innerHTML = `
    <div class="status-head">
      <span class="ticket-chip">${ticketId}</span>
      <span class="state-chip ${normalizedStatus}">${prettyStatus}</span>
    </div>
    <p class="task-text">Task: ${safeTask}</p>
    <p class="summary-text">${safeSummary}</p>
    <div class="ticket-controls">
      <button
        class="ticket-action-btn"
        data-action="details"
        data-ticket-id="${ticketId}"
        title="${enableActions ? "Show ticket details" : "Available after ticket completion"}"
        ${enableActions ? "" : "disabled"}
      >Details</button>
      <button
        class="ticket-action-btn"
        data-action="actions"
        data-ticket-id="${ticketId}"
        title="${enableActions ? "Show ticket actions" : "Available after ticket completion"}"
        ${enableActions ? "" : "disabled"}
      >Actions</button>
    </div>
    <div class="ticket-extra" hidden></div>
  `;
  statusList.appendChild(div);
  applyTicketFilter();
}

function renderDetailsView(payload) {
  if (payload.source === "dead_letter_queue") {
    const deadLetterHtml = Array.isArray(payload.dead_letters) && payload.dead_letters.length > 0
      ? payload.dead_letters
          .map(
            (item) => `
              <article class="extra-row">
                <div class="extra-meta">
                  <span><strong>ticket_id:</strong> ${escapeHtml(item.ticket_id)}</span>
                  <span><strong>timestamp:</strong> ${escapeHtml(formatDate(item.timestamp))}</span>
                </div>
                <p><strong>error:</strong> ${escapeHtml(item.error)}</p>
                <p><strong>payload:</strong></p>
                <pre>${toJsonString(item.payload)}</pre>
              </article>
            `,
          )
          .join("")
      : `<p class="extra-empty">No dead letter entries found for this ticket.</p>`;

    const auditHtml = Array.isArray(payload.logs) && payload.logs.length > 0
      ? payload.logs
          .map(
            (item) => `
              <article class="extra-row">
                <div class="extra-meta">
                  <span><strong>step:</strong> ${escapeHtml(item.step)}</span>
                  <span><strong>action:</strong> ${escapeHtml(item.action)}</span>
                  <span><strong>timestamp:</strong> ${escapeHtml(formatDate(item.timestamp))}</span>
                </div>
                <p><strong>input:</strong></p>
                <pre>${toJsonString(item.input)}</pre>
                <p><strong>output:</strong></p>
                <pre>${toJsonString(item.output)}</pre>
              </article>
            `,
          )
          .join("")
      : `<p class="extra-empty">No audit logs found for this ticket.</p>`;

    return `
      <p class="extra-section-title">Dead Letter Entries</p>
      ${deadLetterHtml}
      <p class="extra-section-title">Audit Logs</p>
      ${auditHtml}
    `;
  }

  if (!Array.isArray(payload.logs) || payload.logs.length === 0) {
    return `<p class="extra-empty">No audit logs found for this ticket.</p>`;
  }

  return payload.logs
    .map(
      (item) => `
        <article class="extra-row">
          <div class="extra-meta">
            <span><strong>step:</strong> ${escapeHtml(item.step)}</span>
            <span><strong>action:</strong> ${escapeHtml(item.action)}</span>
            <span><strong>timestamp:</strong> ${escapeHtml(formatDate(item.timestamp))}</span>
          </div>
          <p><strong>input:</strong></p>
          <pre>${toJsonString(item.input)}</pre>
          <p><strong>output:</strong></p>
          <pre>${toJsonString(item.output)}</pre>
        </article>
      `,
    )
    .join("");
}

function renderActionsView(payload) {
  if (!Array.isArray(payload.actions) || payload.actions.length === 0) {
    return `<p class="extra-empty">No action log entries found for this ticket.</p>`;
  }

  return payload.actions
    .map(
      (item) => `
        <article class="extra-row">
          <div class="extra-meta">
            <span><strong>ticket_id:</strong> ${escapeHtml(item.ticket_id)}</span>
            <span><strong>action_type:</strong> ${escapeHtml(item.action_type)}</span>
            <span><strong>created_at:</strong> ${escapeHtml(formatDate(item.created_at))}</span>
          </div>
          <p><strong>payload:</strong></p>
          <pre>${toJsonString(item.payload)}</pre>
        </article>
      `,
    )
    .join("");
}

statusList.addEventListener("click", async (event) => {
  const button = event.target.closest(".ticket-action-btn");
  if (!button) {
    return;
  }

  const ticketId = button.dataset.ticketId;
  const actionType = button.dataset.action;
  const ticketCard = button.closest(".status-item");
  const ticketExtra = ticketCard?.querySelector(".ticket-extra");
  if (!ticketId || !ticketCard || !ticketExtra) {
    return;
  }

  const currentStatus = statusStore.get(ticketId);
  if (!isTicketTerminal(currentStatus)) {
    setActionMessage("Details and actions are available after ticket completion only.", "idle");
    return;
  }

  const peerButtons = ticketCard.querySelectorAll(".ticket-action-btn");

  if (ticketExtra.dataset.view === actionType && !ticketExtra.hidden) {
    ticketExtra.hidden = true;
    ticketExtra.removeAttribute("data-view");
    peerButtons.forEach((item) => {
      item.classList.remove("active");
    });
    return;
  }

  peerButtons.forEach((item) => {
    item.classList.toggle("active", item === button);
  });

  peerButtons.forEach((item) => {
    item.disabled = true;
  });

  const endpoint = actionType === "details" ? "details" : "actions";
  ticketExtra.hidden = false;
  ticketExtra.dataset.view = actionType;
  ticketExtra.innerHTML = `<p class="extra-loading">Loading ${escapeHtml(actionType)}...</p>`;

  try {
    const response = await fetch(`${apiBase()}/${endpoint}/${ticketId}`);
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || `${titleCase(actionType)} fetch failed`);
    }

    ticketExtra.innerHTML =
      actionType === "details" ? renderDetailsView(payload) : renderActionsView(payload);
  } catch (error) {
    ticketExtra.innerHTML = `<p class="extra-error">${escapeHtml(error.message)}</p>`;
  } finally {
    peerButtons.forEach((item) => {
      item.disabled = !isTicketTerminal(statusStore.get(ticketId));
    });
  }
});

metricFilterButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setMetricFilter(button.dataset.filter || "all");
  });
});

FILE_FIELDS.forEach(({ inputId }) => {
  const input = getFileInput(inputId);
  if (!input) {
    return;
  }
  input.addEventListener("change", () => {
    updateFileClearButtons();
  });
});

uploadForm.addEventListener("click", (event) => {
  const clearButton = event.target.closest(".file-clear-btn");
  if (!clearButton || clearButton.disabled) {
    return;
  }

  const inputId = clearButton.dataset.fileInput;
  if (!inputId) {
    return;
  }

  const input = getFileInput(inputId);
  if (!input) {
    return;
  }

  input.value = "";
  updateFileClearButtons();
});

async function pollStatuses() {
  if (activeTicketIds.length === 0) {
    return;
  }

  const base = apiBase();
  let pending = 0;

  for (const ticketId of activeTicketIds) {
    try {
      const res = await fetch(`${base}/status/${ticketId}`);
      if (!res.ok) {
        throw new Error(`status fetch failed: ${res.status}`);
      }

      const body = await res.json();
      renderTicketStatus(ticketId, body.status, body.task_id, body.decision_summary, body.decision_action);

      if (!terminalStates.has(String(body.status).toLowerCase())) {
        pending += 1;
      }
    } catch (error) {
      log(`Status polling error for ${ticketId}: ${error.message}`);
      pending += 1;
    }
  }

  if (pending > 0) {
    updateMetrics();
    updatePulse(pending);
    setActionMessage("Tickets are still processing. Please wait for completion.", "busy");
    setTimeout(pollStatuses, 3000);
  } else {
    updateMetrics();
    updatePulse(0);

    if (processingStartedAtMs !== null) {
      const totalDurationMs = Math.max(0, Date.now() - processingStartedAtMs);
      const averageDurationMs =
        activeTicketIds.length > 0
          ? totalDurationMs / activeTicketIds.length
          : 0;
      showTimingSummary(totalDurationMs, averageDurationMs, activeTicketIds.length);
    }

    isProcessBusy = false;
    applyButtonState();
    setActionMessage("Processing completed. Review summaries and statuses below.", "success");
    log("All ticket workflows reached a terminal state.");
  }
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  if (isUploadBusy || isProcessBusy) {
    return;
  }

  isUploadBusy = true;
  applyButtonState();
  setActionMessage("Uploading files... Please wait until the upload is finished.", "busy");

  try {
    const formData = new FormData();
    let selectedFileCount = 0;
    FILE_FIELDS.forEach(({ inputId, formKey }) => {
      const input = getFileInput(inputId);
      if (input && input.files && input.files[0]) {
        formData.append(formKey, input.files[0]);
        selectedFileCount += 1;
      }
    });

    if (selectedFileCount === 0) {
      throw new Error("Select at least one file before uploading.");
    }

    const response = await fetch(`${apiBase()}/upload`, {
      method: "POST",
      body: formData,
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Upload failed");
    }

    log(uploadSummaryText(payload));
    setActionMessage("Upload completed successfully. You can now process tickets.", "success");

    FILE_FIELDS.forEach(({ inputId }) => {
      const input = getFileInput(inputId);
      if (input) {
        input.value = "";
      }
    });
    updateFileClearButtons();
  } catch (error) {
    log(`Upload error: ${error.message}`);
    setActionMessage(`Upload failed: ${error.message}`, "error");
  } finally {
    isUploadBusy = false;
    applyButtonState();
  }
});

processBtn.addEventListener("click", async () => {
  if (isUploadBusy || isProcessBusy || isExportBusy) {
    return;
  }

  isProcessBusy = true;
  applyButtonState();
  setActionMessage("Starting ticket processing...", "busy");
  resetTimingSummary();
  processingStartedAtMs = Date.now();

  try {
    const response = await fetch(`${apiBase()}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Process request failed");
    }

    activeTicketIds = Object.keys(payload.tasks || {});
    if (activeTicketIds.length === 0) {
      log("No tickets queued. Upload data first or check statuses.");
      isProcessBusy = false;
      applyButtonState();
      setActionMessage("No tickets were queued. Upload files or verify pending tickets.", "idle");
      processingStartedAtMs = null;
      resetTimingSummary();
      return;
    }

    log(`Queued ${payload.queued} tickets.`);
    statusList.innerHTML = "";
    statusStore.clear();
    updateMetrics();
    applyTicketFilter();
    activeTicketIds.forEach((ticketId) => {
      renderTicketStatus(ticketId, "queued", payload.tasks[ticketId], null, null);
    });

    updatePulse(activeTicketIds.length);
    updateMetrics();
    setActionMessage("Processing tickets... live updates will appear below.", "busy");
    setTimeout(pollStatuses, 1000);
  } catch (error) {
    log(`Process error: ${error.message}`);
    updatePulse(0);
    isProcessBusy = false;
    applyButtonState();
    setActionMessage(`Processing failed: ${error.message}`, "error");
    processingStartedAtMs = null;
    resetTimingSummary();
  }
});

exportAuditBtn.addEventListener("click", async () => {
  if (isUploadBusy || isProcessBusy || isExportBusy) {
    return;
  }

  isExportBusy = true;
  applyButtonState();
  setActionMessage("Exporting audit logs JSON...", "busy");

  try {
    const response = await fetch(`${apiBase()}/audit/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Audit export failed");
    }

    const count = Number(payload.audit_logs_count || 0);
    const path = payload.file_path || "audit_json/audit_logs.json";

    const downloadResponse = await fetch(`${apiBase()}/audit/export/download`, {
      method: "GET",
    });
    if (!downloadResponse.ok) {
      throw new Error("Audit JSON download failed");
    }

    const blob = await downloadResponse.blob();
    const blobUrl = URL.createObjectURL(blob);
    const downloadLink = document.createElement("a");
    downloadLink.href = blobUrl;
    downloadLink.download = "audit_logs.json";
    document.body.appendChild(downloadLink);
    downloadLink.click();
    downloadLink.remove();
    URL.revokeObjectURL(blobUrl);

    log(`Audit JSON exported: rows=${count}, path=${path}`);
    setActionMessage(`Audit JSON exported and downloaded. Rows: ${count}`, "success");
  } catch (error) {
    log(`Audit export error: ${error.message}`);
    setActionMessage(`Audit export failed: ${error.message}`, "error");
  } finally {
    isExportBusy = false;
    applyButtonState();
  }
});

setMetricFilter("all");
resetTimingSummary();
applyButtonState();
