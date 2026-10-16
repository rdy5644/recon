const STATUS_LABELS = {
  pending: "Pending",
  partial: "Partial",
  fully_settled: "Fully settled",
  over_settled: "Over settled",
  refunded: "Refunded",
  not_applicable: "Not applicable",
};
const STATUS_COLORS = {
  pending: "#dfa83d",
  partial: "#e18b50",
  fully_settled: "#36a47b",
  over_settled: "#d94c5c",
  refunded: "#8974d8",
  not_applicable: "#b9c1cc",
};
const TRANSACTION_PAGE_SIZE = 100;
const TRANSACTION_FETCH_SIZE = 500;
const UNMATCHED_REASONS = {
  LIFECYCLE_NOT_FOUND: "Lifecycle ID not found",
  AMBIGUOUS_LIFECYCLE: "Lifecycle ID matches more than one transaction",
  LIFECYCLE_ATTRIBUTE_MISMATCH: "Transaction details do not match",
  FALLBACK_MATCH_NOT_FOUND: "No transaction matched these details",
  AMBIGUOUS_FALLBACK_MATCH: "More than one transaction matched these details",
};
function readLatestImport() {
  try {
    const saved = sessionStorage.getItem("ledgerline.latestImport");
    return saved ? JSON.parse(saved) : null;
  } catch {
    return null;
  }
}

const state = { transactions: [], allTransactions: [], overviewTransactions: [], transactionTotal: 0, transactionOffset: 0, overviewOffset: 0, transactionsLoaded: false, overviewFilter: "all", summary: null, latestImport: readLatestImport(), uploadLimitBytes: null, uploadLimitLabel: null };

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const titleCase = (value) => String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());

function formatCurrency(amount, currency = "USD") {
  if (amount === null || amount === undefined || Number.isNaN(Number(amount))) return "—";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency, minimumFractionDigits: 2 }).format(Number(amount));
  } catch {
    return `${currency} ${Number(amount).toFixed(2)}`;
  }
}

function showNotice(message, type = "info") {
  const notice = $("#notice");
  notice.className = `notice ${type}`;
  notice.textContent = message;
  notice.hidden = false;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, 9000);
}

async function loadUploadConfiguration() {
  const response = await fetch("/config");
  const configuration = await response.json();
  if (!response.ok || !Number.isFinite(configuration.max_upload_size_bytes) || configuration.max_upload_size_bytes <= 0) {
    throw new Error("Upload configuration is unavailable.");
  }
  state.uploadLimitBytes = configuration.max_upload_size_bytes;
  state.uploadLimitLabel = configuration.max_upload_size_label;
  $("#upload-limit").textContent = state.uploadLimitLabel;
}

function isUploadFileAllowed(file) {
  if (!file.name.toLowerCase().endsWith(".csv")) {
    showNotice("Choose a CSV file to continue.", "error");
    return false;
  }
  if (state.uploadLimitBytes !== null && file.size > state.uploadLimitBytes) {
    showNotice(`This file is larger than the ${state.uploadLimitLabel} upload limit.`, "error");
    return false;
  }
  return true;
}

function setActiveNavigation(section) {
  document.querySelectorAll(".nav-item[data-page-link]").forEach((link) => {
    if (link.dataset.pageLink === "results") link.hidden = !state.latestImport;
    const active = link.dataset.pageLink === section;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

const PAGE_NAMES = new Set(["overview", "imports", "results", "transactions"]);
function showPage(page, { updateAddress = true, scroll = true } = {}) {
  if (!PAGE_NAMES.has(page) || (page === "results" && !state.latestImport)) page = "imports";
  document.querySelectorAll(".page-view[data-page]").forEach((view) => {
    view.hidden = view.dataset.page !== page;
  });
  setActiveNavigation(page);
  if (updateAddress && window.location.hash !== `#${page}`) {
    window.history.pushState(null, "", `#${page}`);
  } else if (!updateAddress && window.location.hash && window.location.hash !== `#${page}`) {
    window.history.replaceState(null, "", `#${page}`);
  }
  if (scroll) window.scrollTo({ top: 0, behavior: "instant" });
}

document.querySelectorAll("[data-page-link]").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    showPage(link.dataset.pageLink);
  });
});
window.addEventListener("popstate", () => showPage(window.location.hash.slice(1), { updateAddress: false }));
window.addEventListener("hashchange", () => showPage(window.location.hash.slice(1), { updateAddress: false }));

function updateWorkflow() {
  const hasImportedRows = Boolean(state.latestImport);
  const needsReview = Number(state.latestImport?.unmatched_count || 0) > 0;
  const steps = [$("#workflow-upload"), $("#workflow-match"), $("#workflow-review")];
  steps.forEach((step) => step.classList.remove("current", "complete"));
  if (!hasImportedRows) {
    steps[0].classList.add("current");
    return;
  }
  steps[0].classList.add("complete");
  steps[1].classList.add("complete");
  if (needsReview) steps[2].classList.add("current");
  else steps[2].classList.add("complete");
}

function renderSummary(summary) {
  state.summary = summary;
  updateWorkflow();
  $("#transactions-processed").textContent = Number(summary.transactions_processed || 0).toLocaleString();
  $("#settlements-processed").textContent = Number(summary.settlements_processed || 0).toLocaleString();
  $("#critical-count").textContent = Number(summary.critical_issue_count || 0).toLocaleString();
  $("#warning-count").textContent = Number(summary.warning_count || 0).toLocaleString();
  const rate = Number(summary.settlement_rate_percent || 0);
  $("#settlement-rate").textContent = `${rate.toFixed(1)}%`;
  $("#rate-ring-label").textContent = `${Math.round(rate)}%`;
  const degrees = Math.max(0, Math.min(100, rate)) * 3.6;
  $("#rate-ring").style.background = `conic-gradient(#4178e8 ${degrees}deg, #eef1f5 ${degrees}deg)`;
  $("#avg-days").textContent = summary.average_days_to_settle === null ? "—" : `${summary.average_days_to_settle} days`;
  if (summary.total_outstanding_amount !== null && summary.total_outstanding_amount !== undefined) {
    $("#outstanding-total").textContent = formatCurrency(summary.total_outstanding_amount, summary.outstanding_currency || "USD");
  } else {
    const amounts = Object.entries(summary.outstanding_by_currency || {});
    $("#outstanding-total").textContent = amounts.length ? amounts.map(([currency, amount]) => formatCurrency(amount, currency)).join(" · ") : "—";
  }

  const statuses = summary.settlement_status_breakdown || {};
  const total = Object.values(statuses).reduce((sum, count) => sum + Number(count || 0), 0);
  $("#status-total").textContent = `${total.toLocaleString()} transactions`;
  const segments = Object.entries(STATUS_LABELS).map(([key]) => {
    const count = Number(statuses[key] || 0);
    return { key, count, percent: total ? (count / total) * 100 : 0 };
  });
  $("#status-chart").innerHTML = total ? segments.filter((item) => item.count).map((item) =>
    `<span class="status-segment" title="${escapeHtml(STATUS_LABELS[item.key])}: ${item.count}" style="width:${item.percent}%;background:${STATUS_COLORS[item.key]}"></span>`,
  ).join("") : "";
  $("#status-legend").innerHTML = segments.map((item) =>
    `<span class="legend-item"><i class="legend-dot" style="background:${STATUS_COLORS[item.key]}"></i>${escapeHtml(STATUS_LABELS[item.key])}<b class="legend-count">${item.count}</b></span>`,
  ).join("");
}

function transactionRowMarkup(item) {
    const status = String(item.settlement_status || "PENDING").toLowerCase();
    const issueClass = item.issue_severity === "critical" ? "critical" : item.issue_severity === "warning" ? "warning" : "none";
    const issueLabel = item.issue_severity === "critical" ? "Critical" : item.issue_severity === "warning" ? "Warning" : "Clear";
    return `<tr data-id="${escapeHtml(item.transaction_id)}" tabindex="0" aria-label="Open transaction ${escapeHtml(item.transaction_id)}">
      <td><span class="transaction-id">${escapeHtml(item.transaction_id)}</span><span class="transaction-sub">${escapeHtml(item.account_id)}</span></td>
      <td>${escapeHtml(item.transaction_date)}</td><td class="merchant-cell">${escapeHtml(item.merchant_name)}</td>
      <td class="amount-cell">${formatCurrency(item.transaction_amount, item.currency)}</td>
      <td><span class="status-badge status-${escapeHtml(status)}">${escapeHtml(titleCase(status))}</span></td>
      <td class="amount-cell">${formatCurrency(item.total_settled_amount, item.currency)}</td>
      <td><span class="issue-badge issue-${issueClass}">${issueLabel}</span></td><td class="row-arrow">›</td>
    </tr>`;
}

function renderTransactions(items, total) {
  state.transactions = items;
  state.transactionTotal = total;
  const body = $("#transaction-rows");
  const pageCount = Math.max(1, Math.ceil(total / TRANSACTION_PAGE_SIZE));
  const currentPage = Math.floor(state.transactionOffset / TRANSACTION_PAGE_SIZE) + 1;
  $("#result-count").textContent = `${total.toLocaleString()} transaction${total === 1 ? "" : "s"}`;
  $("#transactions-page-label").textContent = `Page ${currentPage} of ${pageCount}`;
  $("#transactions-previous").disabled = state.transactionOffset <= 0;
  $("#transactions-next").disabled = state.transactionOffset + items.length >= total;
  body.innerHTML = items.length
    ? items.map(transactionRowMarkup).join("")
    : '<tr><td colspan="8" class="empty-state">No transactions match these filters.</td></tr>';
}

function readAdvancedFilters(scope) {
  const panel = document.querySelector(`[data-filter-scope="${scope}"]`);
  return Object.fromEntries([...panel.querySelectorAll("[data-filter]")].map((input) => [input.dataset.filter, input.value.trim()]));
}

function matchesAdvancedFilters(item, filters) {
  const transactionDate = String(item.transaction_date || "");
  const lastSettlementDate = String(item.last_settlement_date || "");
  if (filters.transaction_date_from && transactionDate < filters.transaction_date_from) return false;
  if (filters.transaction_date_to && transactionDate > filters.transaction_date_to) return false;
  if (filters.last_settlement_date_from && (!lastSettlementDate || lastSettlementDate < filters.last_settlement_date_from)) return false;
  if (filters.last_settlement_date_to && (!lastSettlementDate || lastSettlementDate > filters.last_settlement_date_to)) return false;
  if (filters.amount_min && Number(item.transaction_amount) < Number(filters.amount_min)) return false;
  if (filters.amount_max && Number(item.transaction_amount) > Number(filters.amount_max)) return false;
  if (filters.currency && String(item.currency || "").toUpperCase() !== filters.currency.toUpperCase()) return false;
  return true;
}

function populateCurrencyFilters() {
  const currencies = [...new Set(state.allTransactions.map((item) => String(item.currency || "").toUpperCase()).filter(Boolean))].sort();
  ["overview-currency", "tx-currency"].forEach((id) => {
    const select = $(`#${id}`);
    const previous = select.value;
    select.innerHTML = '<option value="">All currencies</option>' + currencies.map((currency) => `<option value="${escapeHtml(currency)}">${escapeHtml(currency)}</option>`).join("");
    if (currencies.includes(previous)) select.value = previous;
  });
}

function renderOverviewTransactions() {
  const filterLabels = {
    all: "All transactions",
    settled: "With settlement history",
    critical: "Critical issues",
    warning: "Warnings",
  };
  const query = $("#overview-search-input").value.trim().toLocaleLowerCase();
  const status = $("#overview-status-filter").value;
  const issue = $("#overview-issue-filter").value;
  const advanced = readAdvancedFilters("overview");
  const items = state.allTransactions.filter((item) => {
    if (state.overviewFilter === "settled" && Number(item.settlement_count || 0) === 0) return false;
    if ((state.overviewFilter === "critical" || state.overviewFilter === "warning") && item.issue_severity !== state.overviewFilter) return false;
    if (status && String(item.settlement_status || "").toUpperCase() !== status) return false;
    if (issue && item.issue_severity !== issue) return false;
    if (query && ![item.transaction_id, item.lifecycle_id, item.merchant_name, item.account_id].some((value) => String(value || "").toLocaleLowerCase().includes(query))) return false;
    if (!matchesAdvancedFilters(item, advanced)) return false;
    return true;
  });
  state.overviewTransactions = items;
  const pageCount = Math.max(1, Math.ceil(items.length / TRANSACTION_PAGE_SIZE));
  const currentPage = Math.floor(state.overviewOffset / TRANSACTION_PAGE_SIZE) + 1;
  document.querySelectorAll("[data-overview-filter]").forEach((button) => {
    const selected = button.dataset.overviewFilter === state.overviewFilter;
    button.setAttribute("aria-pressed", String(selected));
  });
  $("#overview-filter-label").textContent = filterLabels[state.overviewFilter] || filterLabels.all;
  $("#overview-result-count").textContent = `${items.length.toLocaleString()} transaction${items.length === 1 ? "" : "s"}`;
  $("#overview-page-label").textContent = `Page ${currentPage} of ${pageCount}`;
  $("#overview-previous").disabled = state.overviewOffset <= 0;
  $("#overview-next").disabled = state.overviewOffset + TRANSACTION_PAGE_SIZE >= items.length;
  const pageItems = items.slice(state.overviewOffset, state.overviewOffset + TRANSACTION_PAGE_SIZE);
  $("#overview-transaction-rows").innerHTML = pageItems.length
    ? pageItems.map(transactionRowMarkup).join("")
    : '<tr><td colspan="8" class="empty-state">No transactions match this metric and these filters.</td></tr>';
}

function reportFilterLabels(source) {
  const values = source === "overview"
    ? {
      search: $("#overview-search-input").value.trim(),
      status: $("#overview-status-filter").value,
      issue: $("#overview-issue-filter").value,
      ...readAdvancedFilters("overview"),
      metric: state.overviewFilter === "all" ? "" : state.overviewFilter,
    }
    : {
      search: $("#search-input").value.trim(),
      status: $("#status-filter").value,
      issue: $("#issue-filter").value,
      ...readAdvancedFilters("transactions"),
    };
  const labels = {
    search: "Search",
    status: "Status",
    issue: "Issue",
    transaction_date_from: "Transaction from",
    transaction_date_to: "Transaction to",
    last_settlement_date_from: "Last settlement from",
    last_settlement_date_to: "Last settlement to",
    amount_min: "Minimum amount",
    amount_max: "Maximum amount",
    currency: "Currency",
    metric: "Overview tile",
  };
  return Object.entries(values)
    .filter(([, value]) => value !== "" && value !== null && value !== undefined)
    .map(([key, value]) => `${labels[key] || key}: ${key === "status" || key === "issue" || key === "metric" ? titleCase(value) : value}`);
}

async function fetchReportRows(source) {
  const params = new URLSearchParams();
  if (source === "overview") {
    const search = $("#overview-search-input").value.trim();
    const status = $("#overview-status-filter").value;
    if (search) params.set("search", search);
    if (status) params.set("settlement_status", status);
    Object.entries(readAdvancedFilters("overview")).forEach(([key, value]) => { if (value) params.set(key, value); });
  } else {
    const search = $("#search-input").value.trim();
    const status = $("#status-filter").value;
    const issue = $("#issue-filter").value;
    if (search) params.set("search", search);
    if (status) params.set("settlement_status", status);
    if (issue) params.set("issue", issue);
    Object.entries(readAdvancedFilters("transactions")).forEach(([key, value]) => { if (value) params.set(key, value); });
  }

  const rows = [];
  let total = 0;
  for (let offset = 0; offset === 0 || offset < total; offset += 500) {
    params.set("limit", "500");
    params.set("offset", String(offset));
    const response = await fetch(`/transactions?${params}`);
    if (!response.ok) throw new Error("The report could not be prepared. Review the filters and try again.");
    const page = await response.json();
    total = page.total;
    rows.push(...page.items);
    if (!page.items.length) break;
  }
  if (source === "overview") {
    const issue = $("#overview-issue-filter").value;
    return rows.filter((item) => {
      if (state.overviewFilter === "settled" && Number(item.settlement_count || 0) === 0) return false;
      if ((state.overviewFilter === "critical" || state.overviewFilter === "warning") && item.issue_severity !== state.overviewFilter) return false;
      if (issue && item.issue_severity !== issue) return false;
      return true;
    });
  }
  return rows;
}

async function fetchAllTransactions() {
  const items = [];
  let total = 0;
  for (let offset = 0; offset === 0 || offset < total; offset += TRANSACTION_FETCH_SIZE) {
    const response = await fetch(`/transactions?limit=${TRANSACTION_FETCH_SIZE}&offset=${offset}`);
    if (!response.ok) throw new Error("The complete transaction list could not be loaded. Refresh and try again.");
    const page = await response.json();
    total = page.total;
    items.push(...page.items);
    if (!page.items.length) break;
  }
  return items;
}

function csvCell(value) {
  const text = String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1200);
}

function makeCsvReport(items) {
  const columns = [
    ["transaction_id", "Transaction ID"], ["lifecycle_id", "Lifecycle ID"], ["account_id", "Account ID"],
    ["merchant_name", "Merchant"], ["transaction_date", "Transaction date"], ["transaction_amount", "Transaction amount"],
    ["currency", "Currency"], ["settlement_status", "Settlement status"], ["total_settled_amount", "Net settled amount"],
    ["gross_debit_amount", "Gross debit amount"], ["total_credits", "Total credits"], ["outstanding_amount", "Outstanding amount"],
    ["last_settlement_date", "Last settlement date"], ["settlement_count", "Settlement events"], ["issue_severity", "Issue"],
  ];
  const lines = [columns.map(([, label]) => csvCell(label)).join(",")];
  items.forEach((item) => lines.push(columns.map(([key]) => csvCell(item[key])).join(",")));
  return new Blob(["\uFEFF", lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
}

function pdfAscii(value) {
  return String(value ?? "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/[^\x20-\x7E]/g, "?").replace(/[\\()]/g, "\\$&").replace(/[\r\n]+/g, " ");
}

function pdfFit(value, width, size = 7) {
  const text = pdfAscii(value).replace(/\\([\\()])/g, "$1");
  const maxChars = Math.max(1, Math.floor(width / (size * .52)));
  return text.length > maxChars ? `${text.slice(0, Math.max(1, maxChars - 3))}...` : text;
}

function makePdfReport(items, filterLabels) {
  const pageWidth = 842;
  const pageHeight = 595;
  const margin = 32;
  const columns = [
    { title: "TRANSACTION", width: 72, value: (item) => item.transaction_id },
    { title: "DATE", width: 76, value: (item) => item.transaction_date },
    { title: "MERCHANT", width: 196, value: (item) => item.merchant_name },
    { title: "ACCOUNT", width: 72, value: (item) => item.account_id },
    { title: "STATUS", width: 98, value: (item) => titleCase(item.settlement_status) },
    { title: "AMOUNT", width: 105, value: (item) => `${Number(item.transaction_amount || 0).toFixed(2)} ${item.currency || ""}` },
    { title: "NET SETTLED", width: 105, value: (item) => `${Number(item.total_settled_amount || 0).toFixed(2)} ${item.currency || ""}` },
    { title: "ISSUE", width: 54, value: (item) => titleCase(item.issue_severity) },
  ];
  const rowsPerPage = 21;
  const pages = [];
  for (let start = 0; start < items.length; start += rowsPerPage) pages.push(items.slice(start, start + rowsPerPage));
  const totalPages = Math.max(1, pages.length);
  if (!pages.length) pages.push([]);
  const filterText = filterLabels.length ? filterLabels.join("  |  ") : "No filters applied";
  const generatedAt = new Date().toLocaleString();
  const pageStreams = pages.map((rows, pageIndex) => {
    const commands = [];
    const text = (value, x, y, size, bold = false, color = "0.16 0.22 0.31") => {
      commands.push(`BT /F${bold ? 2 : 1} ${size} Tf ${color} rg 1 0 0 1 ${x} ${y} Tm (${pdfAscii(value)}) Tj ET`);
    };
    commands.push("q");
    text("RECON | Transaction reconciliation report", margin, 560, 17, true, "0.09 0.15 0.24");
    text(`Generated ${generatedAt}  |  ${items.length} transactions`, margin, 541, 8, false, "0.42 0.48 0.57");
    text(`Filters: ${pdfFit(filterText, pageWidth - margin * 2 - 42, 7)}`, margin, 526, 7, false, "0.42 0.48 0.57");
    const tableTop = 506;
    const headerHeight = 21;
    commands.push(`0.94 0.96 0.98 rg ${margin} ${tableTop - headerHeight} ${pageWidth - margin * 2} ${headerHeight} re f`);
    let x = margin;
    columns.forEach((column) => {
      text(column.title, x + 5, tableTop - 14, 6.5, true, "0.42 0.48 0.57");
      x += column.width;
    });
    let yTop = tableTop - headerHeight;
    rows.forEach((item, rowIndex) => {
      const rowHeight = 20;
      const yBottom = yTop - rowHeight;
      if (rowIndex % 2 === 1) commands.push(`0.985 0.989 0.995 rg ${margin} ${yBottom} ${pageWidth - margin * 2} ${rowHeight} re f`);
      let cellX = margin;
      columns.forEach((column) => {
        text(pdfFit(column.value(item), column.width - 10, 7), cellX + 5, yBottom + 7, 7, false, "0.2 0.26 0.35");
        cellX += column.width;
      });
      commands.push(`0.90 0.92 0.95 RG ${margin} ${yBottom} m ${pageWidth - margin} ${yBottom} l S`);
      yTop = yBottom;
    });
    commands.push(`0.87 0.89 0.92 RG ${margin} 37 m ${pageWidth - margin} 37 l S`);
    text("Recon | Filtered transaction report", margin, 23, 7, false, "0.53 0.58 0.65");
    text(`Page ${pageIndex + 1} of ${totalPages}`, pageWidth - margin - 52, 23, 7, false, "0.53 0.58 0.65");
    commands.push("Q");
    return commands.join("\n");
  });

  const objects = [];
  objects[1] = "<< /Type /Catalog /Pages 2 0 R >>";
  const firstPageObject = 5;
  const pageIds = pageStreams.map((_, index) => firstPageObject + index * 2);
  objects[2] = `<< /Type /Pages /Kids [${pageIds.map((id) => `${id} 0 R`).join(" ")}] /Count ${pageIds.length} >>`;
  objects[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>";
  objects[4] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>";
  pageStreams.forEach((stream, index) => {
    const pageId = pageIds[index];
    const streamId = pageId + 1;
    objects[pageId] = `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageWidth} ${pageHeight}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents ${streamId} 0 R >>`;
    objects[streamId] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
  });
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  for (let objectId = 1; objectId < objects.length; objectId += 1) {
    offsets[objectId] = pdf.length;
    pdf += `${objectId} 0 obj\n${objects[objectId]}\nendobj\n`;
  }
  const xrefOffset = pdf.length;
  pdf += `xref\n0 ${objects.length}\n0000000000 65535 f \n`;
  for (let objectId = 1; objectId < objects.length; objectId += 1) pdf += `${String(offsets[objectId]).padStart(10, "0")} 00000 n \n`;
  pdf += `trailer\n<< /Size ${objects.length} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF`;
  return new Blob([pdf], { type: "application/pdf" });
}

async function exportTransactions(format, source, button) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Preparing...";
  try {
    const items = await fetchReportRows(source);
    if (!items.length) {
      showNotice("There are no transactions matching these filters to export.", "info");
      return;
    }
    const filterLabels = reportFilterLabels(source);
    const extension = format === "csv" ? "csv" : "pdf";
    const filename = `recon-transactions-${new Date().toISOString().slice(0, 10)}.${extension}`;
    const report = format === "csv" ? makeCsvReport(items) : makePdfReport(items, filterLabels);
    downloadBlob(report, filename);
    showNotice(`${items.length.toLocaleString()} filtered transaction${items.length === 1 ? "" : "s"} exported as ${extension.toUpperCase()}.`, "success");
  } catch (error) {
    showNotice(error.message || "The report could not be prepared.", "error");
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function renderUnmatched(data) {
  const panel = $("#unmatched-panel");
  panel.hidden = data.total === 0;
  $("#unmatched-count").textContent = data.total;
  $("#unmatched-rows").innerHTML = data.items.map((item) => `<tr>
    <td><span class="transaction-id">${escapeHtml(item.settlement_id)}</span></td>
    <td>${escapeHtml(item.lifecycle_id || "—")}</td><td>${escapeHtml(item.account_id)}</td><td class="merchant-cell">${escapeHtml(item.merchant_name)}</td>
    <td class="amount-cell">${formatCurrency(item.settlement_amount, item.currency)}</td><td><span class="reason-code">${escapeHtml(UNMATCHED_REASONS[item.reason] || titleCase(item.reason))}</span></td><td>${escapeHtml(item.last_seen_at || "—")}</td>
  </tr>`).join("");
}

function renderLatestImport(result) {
  if (!result) return;
  const received = Number(result.settlements_received || 0);
  const added = Number(result.settlements_added || 0);
  const duplicates = Number(result.duplicates_skipped || 0);
  const unmatched = Number(result.unmatched_count || 0);
  $("#result-file-name").textContent = result.file_name
    ? `Processed file: ${result.file_name}`
    : "Review the results and any unmatched provider records.";
  $("#result-received").textContent = received.toLocaleString();
  $("#result-added").textContent = added.toLocaleString();
  $("#result-duplicates").textContent = duplicates.toLocaleString();
  $("#result-unmatched").textContent = unmatched.toLocaleString();
  $("#result-banner").classList.toggle("has-exceptions", unmatched > 0);
  $(".result-banner-icon").textContent = unmatched > 0 ? "!" : "✓";
  $("#result-banner-title").textContent = unmatched > 0 ? "Import complete — review needed" : "All settlements matched";
  const summary = `${added} settlement${added === 1 ? "" : "s"} added, ${duplicates} duplicate${duplicates === 1 ? "" : "s"} skipped`;
  $("#result-banner-copy").textContent = unmatched > 0
    ? `${summary}, with ${unmatched} unmatched record${unmatched === 1 ? "" : "s"} to review below.`
    : `${summary}. No unmatched records need review.`;
  $("#result-clear-note").hidden = unmatched > 0;
  const resultsLink = document.querySelector('.nav-item[data-page-link="results"]');
  resultsLink.hidden = false;
}

async function loadDashboard({ refreshAll = false, offset = 0 } = {}) {
  state.transactionOffset = offset;
  const params = new URLSearchParams();
  const search = $("#search-input").value.trim();
  const status = $("#status-filter").value;
  const issue = $("#issue-filter").value;
  const advanced = readAdvancedFilters("transactions");
  if (search) params.set("search", search);
  if (status) params.set("settlement_status", status);
  if (issue) params.set("issue", issue);
  Object.entries(advanced).forEach(([key, value]) => { if (value) params.set(key, value); });
  params.set("limit", String(TRANSACTION_PAGE_SIZE));
  params.set("offset", String(state.transactionOffset));
  const allTransactionsRequest = state.transactionsLoaded && !refreshAll ? Promise.resolve(null) : fetchAllTransactions();
  const [summaryResponse, transactionsResponse, unmatchedResponse, allTransactions] = await Promise.all([
    fetch("/dashboard/summary"), fetch(`/transactions?${params}`), fetch("/unmatched-settlements?limit=100"), allTransactionsRequest,
  ]);
  if (!transactionsResponse.ok) {
    const problem = await transactionsResponse.json().catch(() => ({}));
    throw new Error(problem.detail || "The transaction list could not load. Review the filters and try again.");
  }
  if (!summaryResponse.ok || !unmatchedResponse.ok) throw new Error("The dashboard could not load. Check that the service is running and refresh.");
  const [summary, transactions, unmatched] = await Promise.all([
    summaryResponse.json(), transactionsResponse.json(), unmatchedResponse.json(),
  ]);
  if (allTransactions !== null) {
    state.allTransactions = allTransactions;
    state.transactionsLoaded = true;
    state.overviewOffset = 0;
    populateCurrencyFilters();
  }
  renderSummary(summary);
  renderTransactions(transactions.items, transactions.total);
  renderOverviewTransactions();
  renderUnmatched(unmatched);
}

function renderDetail(transaction) {
  const issueHtml = transaction.issues.length ? transaction.issues.map((issue) => `
    <div class="issue-card ${escapeHtml(issue.severity)}"><strong>${escapeHtml(titleCase(issue.code))}</strong><p>${escapeHtml(issue.message)}</p><small>Recommended: ${escapeHtml(issue.recommendation)}</small></div>`).join("") : '<div class="issue-card warning" style="background:#f1faf6;border-color:#d8efe3"><strong style="color:#26805f">No active issues</strong><p>The recorded settlement activity is within the reconciliation checks.</p></div>';
  const timeline = transaction.timeline.length ? transaction.timeline.map((event) => `
    <div class="timeline-item ${event.settlement_type === "CREDIT" ? "refund" : ""}"><div class="timeline-top"><span>${escapeHtml(event.date)}</span><span>${escapeHtml(event.settlement_id)}</span></div><div class="timeline-event"><strong>${escapeHtml(event.settlement_type === "CREDIT" ? "Credit / refund" : "Debit settlement")}</strong> · ${formatCurrency(event.amount, transaction.currency)}</div><div class="timeline-total">Net settled after event: ${formatCurrency(event.net_settled_after_event, transaction.currency)}</div></div>`).join("") : '<div class="timeline-empty">No settlement events have been recorded.</div>';
  const status = String(transaction.settlement_status || "PENDING").toLowerCase();
  $("#detail-title").textContent = transaction.transaction_id;
  $("#detail-content").innerHTML = `
    <div class="detail-hero"><div><strong>${formatCurrency(transaction.transaction_amount, transaction.currency)}</strong><span>${escapeHtml(transaction.merchant_name)}</span></div><span class="status-badge status-${escapeHtml(status)}">${escapeHtml(titleCase(status))}</span></div>
    <div class="detail-info-grid">
      <div class="detail-info"><label>Transaction date</label><strong>${escapeHtml(transaction.transaction_date)}</strong></div><div class="detail-info"><label>Transaction state</label><strong>${escapeHtml(transaction.status)}</strong></div>
      <div class="detail-info"><label>Account</label><strong>${escapeHtml(transaction.account_id)}</strong></div><div class="detail-info"><label>Lifecycle ID</label><strong>${escapeHtml(transaction.lifecycle_id || "—")}</strong></div>
      <div class="detail-info"><label>Net settled</label><strong>${formatCurrency(transaction.total_settled_amount, transaction.currency)}</strong></div><div class="detail-info"><label>Unsettled debit</label><strong>${formatCurrency(transaction.outstanding_amount, transaction.currency)}</strong></div>
      <div class="detail-info"><label>Last settlement</label><strong>${escapeHtml(transaction.last_settlement_date || "—")}</strong></div><div class="detail-info"><label>Settlement events</label><strong>${transaction.settlement_count}</strong></div>
    </div>
    <h3 class="drawer-section-title">Issue review</h3>${issueHtml}
    <h3 class="drawer-section-title">Settlement timeline</h3><div class="timeline">${timeline}</div>
    <h3 class="drawer-section-title">Settlement history</h3>
    ${transaction.settlements.length ? `<div class="table-wrap"><table><thead><tr><th>SETTLEMENT</th><th>DATE</th><th>TYPE</th><th>AMOUNT</th></tr></thead><tbody>${transaction.settlements.map((item) => `<tr><td>${escapeHtml(item.settlement_id)}</td><td>${escapeHtml(item.settlement_date)}</td><td>${escapeHtml(item.settlement_type)}</td><td>${formatCurrency(item.settlement_amount, item.currency)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="timeline-empty">No settlement history.</div>'}`;
}

async function openDetail(transactionId) {
  const response = await fetch(`/transactions/${encodeURIComponent(transactionId)}`);
  if (!response.ok) {
    showNotice("Transaction details could not be loaded.", "error");
    return;
  }
  renderDetail(await response.json());
  $("#detail-backdrop").hidden = false;
  $("#close-detail").focus();
}

["#transaction-rows", "#overview-transaction-rows"].forEach((selector) => {
  const body = $(selector);
  body.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row) openDetail(row.dataset.id);
  });
  body.addEventListener("keydown", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openDetail(row.dataset.id);
    }
  });
});

document.querySelectorAll("[data-overview-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.overviewFilter = button.dataset.overviewFilter;
    state.overviewOffset = 0;
    renderOverviewTransactions();
    $("#overview-transactions").scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

function updateOverviewFilters() {
  state.overviewOffset = 0;
  renderOverviewTransactions();
}

$("#overview-previous").addEventListener("click", () => {
  state.overviewOffset = Math.max(0, state.overviewOffset - TRANSACTION_PAGE_SIZE);
  renderOverviewTransactions();
});
$("#overview-next").addEventListener("click", () => {
  if (state.overviewOffset + TRANSACTION_PAGE_SIZE < state.overviewTransactions.length) {
    state.overviewOffset += TRANSACTION_PAGE_SIZE;
    renderOverviewTransactions();
  }
});
$("#overview-search-input").addEventListener("input", updateOverviewFilters);
$("#overview-status-filter").addEventListener("change", updateOverviewFilters);
$("#overview-issue-filter").addEventListener("change", updateOverviewFilters);
document.querySelectorAll('[data-filter-scope="overview"] [data-filter]').forEach((control) => {
  control.addEventListener(control.type === "number" ? "input" : "change", updateOverviewFilters);
});
$("#transactions-previous").addEventListener("click", () => {
  loadDashboard({ offset: Math.max(0, state.transactionOffset - TRANSACTION_PAGE_SIZE) }).catch((error) => showNotice(error.message, "error"));
});
$("#transactions-next").addEventListener("click", () => {
  if (state.transactionOffset + TRANSACTION_PAGE_SIZE < state.transactionTotal) {
    loadDashboard({ offset: state.transactionOffset + TRANSACTION_PAGE_SIZE }).catch((error) => showNotice(error.message, "error"));
  }
});
document.querySelectorAll("[data-export-format]").forEach((button) => {
  button.addEventListener("click", () => exportTransactions(button.dataset.exportFormat, button.dataset.exportSource, button));
});
$("#close-detail").addEventListener("click", () => { $("#detail-backdrop").hidden = true; });
$("#detail-backdrop").addEventListener("click", (event) => { if (event.target === $("#detail-backdrop")) $("#detail-backdrop").hidden = true; });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") $("#detail-backdrop").hidden = true; });

let searchTimer;
$("#search-input").addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => loadDashboard().catch((error) => showNotice(error.message, "error")), 180);
});
$("#status-filter").addEventListener("change", () => loadDashboard().catch((error) => showNotice(error.message, "error")));
$("#issue-filter").addEventListener("change", () => loadDashboard().catch((error) => showNotice(error.message, "error")));
document.querySelectorAll('[data-filter-scope="transactions"] [data-filter]').forEach((control) => {
  control.addEventListener("change", () => loadDashboard().catch((error) => showNotice(error.message, "error")));
});

$("#csv-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (file && !isUploadFileAllowed(file)) {
    event.target.value = "";
    $("#file-name").textContent = "Drop your CSV here";
    $("#file-dropzone").classList.remove("file-selected");
    return;
  }
  $("#file-name").textContent = file ? file.name : "Drop your CSV here";
  $("#file-dropzone").classList.toggle("file-selected", Boolean(file));
});
$("#file-dropzone").addEventListener("dragover", (event) => {
  event.preventDefault();
  $("#file-dropzone").classList.add("drag-over");
});
$("#file-dropzone").addEventListener("dragleave", () => $("#file-dropzone").classList.remove("drag-over"));
$("#file-dropzone").addEventListener("drop", (event) => {
  event.preventDefault();
  const zone = $("#file-dropzone");
  zone.classList.remove("drag-over");
  const file = event.dataTransfer.files[0];
  if (!file) return;
  if (!isUploadFileAllowed(file)) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  $("#csv-file").files = transfer.files;
  $("#csv-file").dispatchEvent(new Event("change", { bubbles: true }));
});
$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("#csv-file").files[0];
  if (!file) return;
  const button = $("#upload-button");
  button.disabled = true;
  button.textContent = "Validating and matching…";
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await fetch("/reconcile", { method: "POST", body: form });
    const result = await response.json();
    if (!response.ok) {
      const detail = result.errors?.map((item) => `${item.row ? `Row ${item.row}: ` : ""}${item.message}`).join(" ") || result.detail || "CSV import failed.";
      throw new Error(detail);
    }
    state.latestImport = { ...result, file_name: file.name, completed_at: new Date().toISOString() };
    try {
      sessionStorage.setItem("ledgerline.latestImport", JSON.stringify(state.latestImport));
    } catch {
      // Keep the current result available in this page even if session storage is unavailable.
    }
    renderLatestImport(state.latestImport);
    $("#upload-form").reset();
    $("#file-name").textContent = "Drop your CSV here";
    $("#file-dropzone").classList.remove("file-selected");
    try {
      await loadDashboard({ refreshAll: true });
    } catch (error) {
      showNotice(error.message, "error");
    }
    showPage("results");
  } catch (error) {
    showNotice(error.message || "CSV import failed.", "error");
  } finally {
    button.disabled = false;
    button.innerHTML = 'Upload and reconcile <span>→</span>';
  }
});

renderLatestImport(state.latestImport);
showPage(window.location.hash.slice(1) || "imports", { updateAddress: false });
loadUploadConfiguration().catch(() => {
  $("#upload-limit").textContent = "enforced by server";
});
loadDashboard().catch((error) => {
  $("#transaction-rows").innerHTML = `<tr><td colspan="8" class="empty-state">${escapeHtml(error.message)}</td></tr>`;
  $("#overview-transaction-rows").innerHTML = `<tr><td colspan="8" class="empty-state">${escapeHtml(error.message)}</td></tr>`;
  showNotice(error.message, "error");
});
