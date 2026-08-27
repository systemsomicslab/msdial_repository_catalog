"use strict";

const state = { overview: null, matches: [], update: null, updateTimer: null };
const form = document.querySelector("#search-form");
const dialog = document.querySelector("#unit-dialog");

document.addEventListener("DOMContentLoaded", initialize);
form.addEventListener("submit", event => { event.preventDefault(); search(); });
document.querySelector("#clear-button").addEventListener("click", () => { form.reset(); search(); });
document.querySelector("#refresh-button").addEventListener("click", initialize);
document.querySelector("#dialog-close").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
document.querySelector("#update-start").addEventListener("click", startUpdate);
document.querySelector("#update-cancel").addEventListener("click", cancelUpdate);
document.querySelector("#update-mode").addEventListener("change", renderUpdateModeNote);

async function initialize() {
  setMessage("Loading local catalog...");
  try {
    state.overview = await fetchJson("/api/status");
    renderOverview(state.overview);
    populateFilters(state.overview.filters || {});
    renderUpdateModeNote();
    await refreshUpdateStatus();
    await search();
  } catch (error) {
    showError(error);
    setMessage("The catalog could not be loaded.");
  }
}

function renderUpdateModeNote() {
  const discover = document.querySelector("#update-mode").value === "discover";
  document.querySelector("#update-mode-note").textContent = discover
    ? "Ask each public repository for its current accession index, then fetch records. This can take hours for a full catalog."
    : "Re-check only records already stored locally. This is the bounded routine update.";
}

async function startUpdate() {
  const repositories = [...document.querySelectorAll('input[name="update_repository"]:checked')]
    .map(input => input.value);
  if (!repositories.length) { showError(new Error("Select at least one repository source.")); return; }
  const rawLimit = document.querySelector("#update-limit").value.trim();
  const payload = {
    repositories,
    mode: document.querySelector("#update-mode").value,
    limit: rawLimit ? Number(rawLimit) : null,
  };
  try {
    state.update = await postJson("/api/update/start", payload);
    renderUpdate(state.update);
    scheduleUpdatePoll();
  } catch (error) { showError(error); }
}

async function cancelUpdate() {
  try {
    state.update = await postJson("/api/update/cancel", {});
    renderUpdate(state.update);
    scheduleUpdatePoll();
  } catch (error) { showError(error); }
}

async function refreshUpdateStatus() {
  try {
    state.update = await fetchJson("/api/update/status");
    renderUpdate(state.update);
    if (isUpdateActive(state.update.state)) scheduleUpdatePoll();
  } catch (error) { showError(error); }
}

function scheduleUpdatePoll() {
  window.clearTimeout(state.updateTimer);
  state.updateTimer = window.setTimeout(pollUpdate, 1000);
}

async function pollUpdate() {
  const previous = state.update?.state;
  try {
    state.update = await fetchJson("/api/update/status");
    renderUpdate(state.update);
    if (isUpdateActive(state.update.state)) {
      scheduleUpdatePoll();
    } else if (isUpdateActive(previous)) {
      state.overview = await fetchJson("/api/status");
      renderOverview(state.overview);
      populateFilters(state.overview.filters || {});
      await search();
    }
  } catch (error) { showError(error); }
}

function renderUpdate(job) {
  const active = isUpdateActive(job.state);
  const progress = document.querySelector("#update-progress");
  progress.hidden = job.state === "idle";
  const badge = document.querySelector("#update-state");
  badge.textContent = displayStatus(job.state);
  badge.className = `job-state ${active ? "active" : job.state.includes("failed") || job.state.includes("error") ? "failed" : ""}`;
  document.querySelector("#update-start").disabled = active;
  document.querySelector("#update-cancel").disabled = !active || job.state === "cancelling";
  document.querySelectorAll('input[name="update_repository"], #update-mode, #update-limit')
    .forEach(control => { control.disabled = active; });
  document.querySelector("#update-message").textContent = job.message || displayStatus(job.stage);
  document.querySelector("#update-meter").value = Number(job.percent || 0);
  document.querySelector("#update-timing").textContent = `Elapsed ${formatDuration(job.elapsed_seconds)} / ETA ${job.eta_seconds == null ? "calculating" : formatDuration(job.eta_seconds)}`;
  document.querySelector("#update-position").textContent = `Repository ${number(job.repository_position)} / ${number(job.repository_count)}${job.repository ? `: ${displayRepository(job.repository)}` : ""}`;
  document.querySelector("#update-count").textContent = job.total
    ? `${number(job.completed)} / ${number(job.total)} accessions${job.accession ? `: ${job.accession}` : ""}`
    : (job.accession || "Accessions not counted yet");
  document.querySelector("#update-outcomes").textContent = `${number(job.hydrated)} updated / ${number(job.unchanged)} unchanged / ${number(job.failed)} failed`;
  const log = document.querySelector("#update-log");
  log.textContent = (job.logs || []).join("\n");
  log.scrollTop = log.scrollHeight;
  const failures = job.failures || [];
  const failureDetails = document.querySelector("#update-failures");
  failureDetails.hidden = failures.length === 0;
  document.querySelector("#update-failures-summary").textContent = `${number(failures.length)} failed accession(s): show details`;
  document.querySelector("#update-failure-list").replaceChildren(...failures.map(item => {
    const row = element("div", "failure-row");
    row.append(element("strong", "", item.accession || "Unknown accession"), element("span", "", item.error || "Unknown error"));
    return row;
  }));
}

function isUpdateActive(value) { return ["queued", "running", "cancelling"].includes(value); }

async function search() {
  const params = new URLSearchParams(new FormData(form));
  params.set("limit", "200");
  setMessage("Searching local metadata...");
  try {
    const response = await fetchJson(`/api/search?${params.toString()}`);
    state.matches = response.matches || [];
    renderResults(state.matches);
  } catch (error) {
    showError(error);
    setMessage("Search failed. Review the filters and try again.");
  }
}

function renderOverview(data) {
  document.querySelector("#database-pill").textContent = data.database;
  const stats = [
    [data.studies, "Studies"], [data.analysis_units, "Analysis units"],
    [data.samples, "Samples"], [data.raw_files, "File records"],
    [data.class_proposals, "Class proposals"],
  ];
  const grid = document.querySelector("#stat-grid");
  grid.replaceChildren(...stats.map(([value, label]) => {
    const card = element("div", "stat-card");
    card.append(element("span", "stat-value", number(value)), element("span", "stat-label", label));
    return card;
  }));

  const repositories = data.repositories || [];
  const maximum = Math.max(1, ...repositories.map(item => Number(item.analysis_units || 0)));
  const bars = document.querySelector("#repository-bars");
  bars.replaceChildren(...repositories.map(item => {
    const row = element("div", "repo-row");
    const name = element("span", "repo-name", displayRepository(item.repository));
    const track = element("progress", "repo-meter");
    track.max = maximum;
    track.value = Number(item.analysis_units || 0);
    row.append(name, track, element("span", "repo-count", number(item.analysis_units)));
    return row;
  }));
  if (!repositories.length) bars.append(element("p", "section-note", "No repository metadata has been crawled yet."));
  renderCrawls(data.latest_crawls || []);
}

function populateFilters(filters) {
  const mapping = {
    repository: filters.repositories, separation: filters.separations,
    chromatography: filters.chromatographies, ion_mode: filters.ion_modes,
    acquisition_mode: filters.acquisition_modes, target_omics: filters.target_omics,
    review_status: filters.review_statuses,
  };
  Object.entries(mapping).forEach(([name, values]) => {
    const select = form.elements[name];
    const current = select.value;
    const first = select.options[0].cloneNode(true);
    select.replaceChildren(first, ...(values || []).map(value => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = name === "repository" ? displayRepository(value) : displayStatus(value);
      return option;
    }));
    select.value = current;
  });
}

function renderResults(matches) {
  const body = document.querySelector("#results-body");
  body.replaceChildren(...matches.map(match => {
    const row = document.createElement("tr");
    const repository = document.createElement("td");
    repository.append(element("span", "repository-name", displayRepository(match.repository)), element("span", "accession", match.accession));
    const study = document.createElement("td");
    study.append(element("div", "study-title", match.title || "Untitled study"), element("div", "unit-label", match.label || match.source_subrecord_id));
    const profile = document.createElement("td");
    const tags = element("div", "tag-list");
    [match.separation, match.chromatography, match.ion_mode, match.acquisition_mode, match.target_omics]
      .filter(value => value && value !== "Unknown").forEach(value => tags.append(element("span", "tag", value)));
    profile.append(tags);
    const samples = element("td", "numeric", number(match.sample_count));
    const download = element("td", "numeric", formatBytes(match.download_bytes));
    const review = document.createElement("td");
    review.append(statusBadge(match.review_status));
    const action = document.createElement("td");
    const button = element("button", "open-button", "Details");
    button.type = "button";
    button.addEventListener("click", () => openUnit(match.unit_id));
    action.append(button);
    row.append(repository, study, profile, samples, download, review, action);
    return row;
  }));
  const empty = document.querySelector("#empty-state");
  empty.hidden = matches.length > 0;
  document.querySelector("#result-count").textContent = `${number(matches.length)} results`;
  setMessage(matches.length ? `Showing ${number(matches.length)} local analysis units.` : "No analysis units match these filters.");
}

async function openUnit(unitId) {
  document.querySelector("#dialog-title").textContent = "Loading analysis unit...";
  document.querySelector("#dialog-content").replaceChildren();
  dialog.showModal();
  try {
    const unit = await fetchJson(`/api/unit/${encodeURIComponent(unitId)}`);
    renderUnit(unit);
  } catch (error) {
    showError(error);
    dialog.close();
  }
}

function renderUnit(unit) {
  document.querySelector("#dialog-repository").textContent = `${displayRepository(unit.repository)} / ${unit.accession}`;
  document.querySelector("#dialog-title").textContent = unit.title || unit.label || "Analysis unit";
  const content = document.querySelector("#dialog-content");
  content.replaceChildren();
  const lead = element("p", "section-note", unit.label || unit.source_subrecord_id);
  const grid = element("div", "detail-grid");
  [
    ["Separation", unit.separation], ["Chromatography", unit.chromatography],
    ["Ion mode", unit.ion_mode], ["Acquisition", unit.acquisition_mode],
    ["Ion mobility", unit.ion_mobility], ["Instrument", unit.instrument || "Not declared"],
    ["Target omics", unit.target_omics], ["Review status", displayStatus(unit.review_status)],
  ].forEach(([label, value]) => {
    const item = element("div", "detail-item");
    item.append(element("span", "", label), element("strong", "", value || "Unknown"));
    grid.append(item);
  });
  content.append(lead, grid);
  if (unit.public_url) {
    const link = element("a", "external-link", "Open repository record");
    link.href = unit.public_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const section = detailSection("Source record", "Repository metadata and provenance");
    section.append(link);
    content.append(section);
  }
  if ((unit.warnings || []).length) {
    const section = detailSection("Review notes", `${unit.warnings.length} item(s)`);
    const list = element("ul", "warning-list");
    unit.warnings.forEach(value => list.append(element("li", "", value)));
    section.append(list);
    content.append(section);
  }
  content.append(sampleSection(unit.samples || []), fileSection(unit.files || []));
}

function sampleSection(samples) {
  const shown = samples.slice(0, 100);
  const section = detailSection("Samples", shown.length < samples.length ? `First 100 of ${number(samples.length)}` : `${number(samples.length)} total`);
  if (!samples.length) { section.append(element("p", "section-note", "No sample rows are indexed for this unit.")); return section; }
  const table = compactTable(["Sample ID", "Source", "Raw file", "Metadata fields"], shown.map(sample => [
    sample.sample_id, sample.source_name, sample.raw_file, number(Object.keys(sample.attributes || {}).length),
  ]));
  section.append(table);
  return section;
}

function fileSection(files) {
  const shown = files.slice(0, 150);
  const section = detailSection("File manifest", shown.length < files.length ? `First 150 of ${number(files.length)}` : `${number(files.length)} total`);
  if (!files.length) { section.append(element("p", "section-note", "No downloadable raw-file records were identified.")); return section; }
  const table = compactTable(["Path", "Role", "Size", "Checksum"], shown.map(file => [
    file.path, file.role, formatBytes(file.size_bytes), file.checksum || "Not declared",
  ]), 0);
  section.append(table);
  return section;
}

function compactTable(headers, rows, pathColumn = -1) {
  const wrap = element("div", "table-scroll");
  const table = element("table", "compact-table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach(value => headRow.append(element("th", "", value)));
  head.append(headRow);
  const body = document.createElement("tbody");
  rows.forEach(values => {
    const row = document.createElement("tr");
    values.forEach((value, index) => row.append(element("td", index === pathColumn ? "file-path" : "", String(value || ""))));
    body.append(row);
  });
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function detailSection(title, note) {
  const section = element("section", "detail-section");
  const header = element("div", "detail-section-header");
  header.append(element("h3", "", title), element("span", "", note));
  section.append(header);
  return section;
}

function renderCrawls(crawls) {
  const target = document.querySelector("#crawl-list");
  if (!crawls.length) {
    target.replaceChildren(element("p", "section-note", "No crawl history is stored in this catalog."));
    return;
  }
  target.replaceChildren(...crawls.slice(0, 6).map(crawl => {
    const card = element("div", "crawl-card");
    card.append(
      element("strong", "", displayRepository(crawl.repository)),
      element("span", "", `${displayStatus(crawl.status)} / v${crawl.crawler_version}`),
      element("span", "", `${number(crawl.hydrated_count)} hydrated, ${number(crawl.failed_count)} failed`),
      element("span", "", formatDate(crawl.completed_at || crawl.started_at)),
    );
    return card;
  }));
}

function statusBadge(status) {
  return element("span", `status status-${String(status || "unreviewed").toLowerCase()}`, displayStatus(status));
}
function setMessage(value) { document.querySelector("#results-message").textContent = value; }
function displayRepository(value) { return ({ metabolomics_workbench: "Metabolomics Workbench", metabolights: "MetaboLights", mb_post: "MB-POST", metabobank: "MetaboBank" })[value] || value || "Unknown"; }
function displayStatus(value) { return String(value || "Unknown").replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase()); }
function number(value) { return new Intl.NumberFormat().format(Number(value || 0)); }
function formatBytes(value) {
  let bytes = Number(value || 0);
  if (!bytes) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index += 1; }
  return `${bytes.toFixed(index > 1 ? 2 : 0)} ${units[index]}`;
}
function formatDate(value) { return value ? new Date(value).toLocaleString() : "Not completed"; }
function formatDuration(value) {
  const total = Math.max(0, Math.round(Number(value || 0)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours} hr ${minutes} min`;
  if (minutes) return `${minutes} min ${seconds} sec`;
  return `${seconds} sec`;
}
function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}
async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
function showError(error) {
  const toast = document.querySelector("#toast");
  toast.textContent = error.message || String(error);
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 7000);
}
