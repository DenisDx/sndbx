"use strict";

const $ = (id) => document.getElementById(id);
let refreshTimer = null;
let logTimer = null;
let term = null;
let fitAddon = null;
let termSocket = null;
let consoleLoaded = false;
let termDataDisposable = null;
let logPanelBound = false;
let restartInProgress = false;
let suppressAutoLogout = false;
let tablePanelsBound = false;
let monitorTimer = null;
let monitorInFlight = false;
let monitorData = {};
let monitorHost = null;
const monitoredSandboxIds = new Set();
let settingsTree = null;
let settingsDocumentsLoaded = false;

function showScreen(name) {
  $("login-screen").style.display = name === "login" ? "flex" : "none";
  $("app-screen").style.display = name === "app" ? "flex" : "none";
}

function showPage(name) {
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll("nav button[id^='nav-']").forEach((b) => b.classList.remove("active"));
  $("page-" + name).classList.add("active");
  $("nav-" + name).classList.add("active");

  if (name === "dashboard") {
    setupLogPanel();
    updateLogTimer();
    updateMonitorTimer();
    loadSystemLog();
  } else {
    clearInterval(logTimer);
    logTimer = null;
    clearInterval(monitorTimer);
    monitorTimer = null;
  }

  if (name === "console") {
    initConsole();
    // Resize to actual element size when tab becomes visible.
    if (fitAddon) fitAddon.fit();
  }

  if (name === "settings" && !settingsDocumentsLoaded) {
    loadSettingsDocuments();
  }
}

function settingsDocumentNote(documentId) {
  const note = $("settings-document-note");
  if (!note) return;
  note.textContent = documentId === "main"
    ? "Keys here override the same key in the VM overlay."
    : "VM defaults. The root config wins every key it defines.";
}

async function loadSettingsDocuments() {
  const select = $("settings-document");
  if (!select) return;
  const result = await apiGet("/api/settings/documents");
  const documents = result && result.documents ? result.documents : [];
  select.innerHTML = "";
  if (!documents.length) {
    $("settings-document-note").textContent = "Settings backend is unavailable.";
    return;
  }
  documents.forEach((doc) => {
    const option = document.createElement("option");
    option.value = doc.id;
    option.textContent = doc.label || doc.id;
    select.appendChild(option);
  });
  select.onchange = () => mountSettings(select.value);
  settingsDocumentsLoaded = true;
  await mountSettings(select.value || documents[0].id);
}

function mountSettings(documentId) {
  const target = $("settings-tree");
  if (!target) return;
  settingsDocumentNote(documentId);
  if (typeof aiset === "undefined" || !aiset.mountTree) {
    target.innerHTML = '<div class="muted">Settings editor assets are not available. Check the sndbx log and reload the page.</div>';
    return;
  }
  if (settingsTree) {
    settingsTree.destroy();
    settingsTree = null;
  }
  settingsTree = aiset.mountTree("#settings-tree", {
    document: documentId,
    apiBase: "/api/settings",
    view: "source",
    lifecycle: [{
      id: "restart",
      label: "Restart sndbx",
      url: "/api/service/restart",
      confirm: "Restart the service now?",
      reloadAfter: false,
    }],
  });
}

function isDashboardActive() {
  const page = $("page-dashboard");
  return !!(page && page.classList.contains("active"));
}

function setTablePanelExpanded(panelName, expanded, persist = true) {
  const panel = $(`${panelName}-panel`);
  if (!panel) return;

  panel.classList.toggle("advanced", expanded);
  panel.classList.toggle("condensed", !expanded);
  const button = document.querySelector(`[data-table-panel-toggle="${panelName}"]`);
  if (button) {
    button.textContent = expanded ? "Condense" : "Advance";
    button.setAttribute("aria-expanded", String(expanded));
  }

  if (persist) {
    localStorage.setItem(`sndbx.table-panel.${panelName}`, expanded ? "advanced" : "condensed");
  }
}

function setupTablePanels() {
  if (tablePanelsBound) return;

  document.querySelectorAll("[data-table-panel-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const panelName = button.dataset.tablePanelToggle || "";
      setTablePanelExpanded(panelName, button.getAttribute("aria-expanded") !== "true");
    });
  });

  ["containers", "images"].forEach((panelName) => {
    const savedMode = localStorage.getItem(`sndbx.table-panel.${panelName}`);
    setTablePanelExpanded(panelName, savedMode === "advanced", false);
  });
  tablePanelsBound = true;
}

async function apiPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    credentials: "include",
  });
  return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
}

async function apiGet(url) {
  try {
    const r = await fetch(url, { credentials: "include" });
    if (r.status === 401) {
      if (!suppressAutoLogout) {
        doLogout();
      }
      return null;
    }
    return r.ok ? r.json().catch(() => null) : null;
  } catch (_) {
    return null;
  }
}

function setServiceIndicator(text, state = "neutral") {
  const el = $("service-indicator");
  if (!el) return;
  el.textContent = text || "Unknown";
  el.className = "badge";
  if (state === "ok") {
    el.classList.add("ok");
  } else if (state === "bad") {
    el.classList.add("bad");
  }
}

function setRestartBanner(active, text) {
  const banner = $("restart-banner");
  const label = $("restart-banner-text");
  if (!banner) return;

  if (active) {
    banner.classList.add("active");
    if (label) {
      label.textContent = text || "Service is restarting...";
    }
    return;
  }

  banner.classList.remove("active");
  if (label) {
    label.textContent = "Service is restarting...";
  }
}

function readServiceState(data) {
  const state = String((data && data.service_state) || "").trim().toLowerCase();
  return state || "online";
}

function renderHealth(health) {
  const items = ["docker", "kata", "webui"];
  items.forEach((k) => {
    const badge = $("health-" + k);
    const detail = $("health-" + k + "-detail");
    const row = (health || {})[k] || {};
    const ok = !!row.ok;
    badge.textContent = ok ? "OK" : "FAIL";
    badge.className = "badge " + (ok ? "ok" : "bad");
    detail.textContent = row.detail || "";
  });
}

function containerActionTooltip(action, sandboxId) {
  const containerName = `sndbx-${sandboxId}`;
  const commands = {
    start: `Start the existing container.\ndocker start ${containerName}\nIf absent: docker run ... --name ${containerName}\nWarning: an existing container keeps its current image.`,
    stop: `Stop the existing container.\ndocker stop ${containerName}\nWarning: it remains stopped until Start is selected.`,
    restart: `Restart the existing container.\ndocker restart ${containerName}\nThen image on_system_start hook runs.\nWarning: this does not use a newly built image.`,
    recreate: `Stop and replace the container using the currently built image.\ndocker diff ${containerName}\ndocker rm -f ${containerName}\ndocker run ... --name ${containerName}\nWarning: rootfs changes are lost. User data in mounts and volumes is not touched. Rebuild and Update are not run.`,
    ssh_open: "Create or reuse an SSH tunnel to the container.\nsocat TCP-LISTEN:<allocated-port>,fork,reuseaddr TCP:<container-ip>:22",
    ssh_close: "Close the SSH tunnel and release its host port.\nkill -TERM <socat-pid>\nFallback: kill -KILL <socat-pid>",
  };
  return commands[action] || "";
}

function vmButtons(sandboxId) {
  const mk = (label, action) =>
    `<button title="${escapeHtml(containerActionTooltip(action, sandboxId))}" onclick="runAction('${sandboxId}','${action}')">${label}</button>`;

  return [
    mk("Start", "start"),
    mk("Stop", "stop"),
    mk("Restart", "restart"),
    mk("Recreate", "recreate"),
  ].join("");
}

function sshButtons(sandboxId) {
  const mk = (label, action) =>
    `<button title="${escapeHtml(containerActionTooltip(action, sandboxId))}" onclick="runAction('${sandboxId}','${action}')">${label}</button>`;

  return [
    mk("Open SSH", "ssh_open"),
    mk("Close SSH", "ssh_close"),
  ].join("");
}

function imageActionTooltip(action, imageRef, imagePath) {
  const noCache = action === "build" ? "" : " --no-cache";
  const context = imagePath || "<local-image-directory>";
  const buildCommand = `docker build -t ${imageRef}${noCache} --build-arg APT_MIRROR=<configured-mirror> ${context}`;
  if (action === "build") {
    return `Build the local image using Docker cache.\n${buildCommand}\nWarning: running containers are not changed; use Recreate afterwards to apply it.`;
  }
  if (action === "rebuild") {
    return `Build the local image without Docker cache.\n${buildCommand}\nWarning: running containers are not changed; use Recreate afterwards to apply it.`;
  }
  if (action === "update") {
    return `Pull fresh Docker base images, then rebuild without cache.\n${buildCommand.replace("docker build", "docker build --pull")}\nProtected: no Git command runs and data/ is not modified.\nWarning: running containers are not changed; use Recreate afterwards to apply it.`;
  }
  return `Fast-forward the image source, pull fresh Docker base images, then rebuild without cache.\ngit fetch --prune; git merge --ff-only <upstream>\n${buildCommand.replace("docker build", "docker build --pull")}\nProtected: data/ is never updated; dirty, divergent, or data-changing updates fail.\nWarning: running containers are not changed; use Recreate afterwards to apply it.`;
}

function imageButtons(imageRef, imagePath, building) {
  const mk = (label, action) =>
    `<button title="${escapeHtml(imageActionTooltip(action, imageRef, imagePath))}" onclick="runImageAction('${escapeHtml(imageRef)}','${action}')"${building ? " disabled" : ""}>${label}</button>`;
  return [
    mk("Build", "build"),
    mk("Build --no-cache", "rebuild"),
    mk("Update & rebuild", "update"),
    mk("Full update & rebuild", "full_update"),
  ].join("");
}

function renderBuildStatus(image) {
  if (image.build_status !== "building") {
    return `<span class="badge">IDLE</span>`;
  }
  const startedAt = Number(image.build_started_at || 0) * 1000;
  const elapsedSeconds = startedAt > 0 ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000)) : 0;
  const action = {
    build: "BUILDING",
    rebuild: "BUILDING NO-CACHE",
    update: "UPDATING",
    full_update: "FULL UPDATING",
  }[image.build_action] || "BUILDING";
  return `<span class="badge building">${action} ${elapsedSeconds}s</span>`;
}

function autoStartButton(sandboxId, enabled) {
  const action = enabled ? "auto_start_disable" : "auto_start_enable";
  const label = enabled ? "Enabled" : "Disabled";
  const stateClass = enabled ? "enabled" : "disabled";
  const title = enabled
    ? "Disable automatic start at sndbx startup.\nNo shell command. Saves run_at_startup=false in data/sandbox_startup_overrides.json"
    : "Enable automatic start at sndbx startup.\nNo shell command. Saves run_at_startup=true in data/sandbox_startup_overrides.json";
  return `<button class="autostart ${stateClass}" title="${title}" onclick="runAction('${escapeHtml(sandboxId)}','${action}')">${label}</button>`;
}

function monitorSwitch(sandboxId, enabled) {
  const state = enabled ? "on" : "off";
  const label = enabled ? "On" : "Off";
  return `<button class="monitor-switch ${state}" data-monitor-toggle="${escapeHtml(sandboxId)}" aria-pressed="${enabled ? "true" : "false"}" onclick="toggleMonitor('${escapeHtml(sandboxId)}')">${label}</button>`;
}

function buildMonitorRow(sandboxId) {
  const tr = document.createElement("tr");
  tr.className = "monitor-row";
  tr.dataset.monitorRow = sandboxId;
  const td = document.createElement("td");
  td.colSpan = 9;
  td.innerHTML = `<div class="monitor-detail" data-monitor-detail="${escapeHtml(sandboxId)}"><span class="monitor-empty">Collecting runtime data...</span></div>`;
  tr.appendChild(td);
  return tr;
}

function renderContainers(containers) {
  const body = $("containers-body");
  body.innerHTML = "";

  if (!containers || containers.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="9" class="muted">No managed containers found</td>`;
    body.appendChild(tr);
    updateMonitorPanelState();
    return;
  }

  containers.forEach((c) => {
    const sandboxId = c.sandbox_id || "";
    const tr = document.createElement("tr");
    tr.dataset.containerRow = sandboxId;
    tr.innerHTML = `
      <td>${escapeHtml(sandboxId)}</td>
      <td>${monitorSwitch(sandboxId, monitoredSandboxIds.has(sandboxId))}</td>
      <td>${autoStartButton(sandboxId, !!c.run_at_startup)}</td>
      <td>${escapeHtml(c.status || "")}</td>
      <td>${escapeHtml(c.image || "")}</td>
      <td>${escapeHtml(c.ports || "")}</td>
      <td>${c.ssh_port ? String(c.ssh_port) : "—"}</td>
      <td><div class="actions">${vmButtons(sandboxId)}</div></td>
      <td><div class="actions">${sshButtons(sandboxId)}</div></td>
    `;
    body.appendChild(tr);
    if (monitoredSandboxIds.has(sandboxId)) {
      body.appendChild(buildMonitorRow(sandboxId));
    }
  });
  updateMonitorPanelState();
  renderMonitorDetails();
}

function toggleMonitor(sandboxId) {
  if (!sandboxId) return;
  if (monitoredSandboxIds.has(sandboxId)) {
    monitoredSandboxIds.delete(sandboxId);
    delete monitorData[sandboxId];
  } else {
    monitoredSandboxIds.add(sandboxId);
    setTablePanelExpanded("containers", true);
  }
  document.querySelectorAll("[data-monitor-toggle]").forEach((button) => {
    if (button.dataset.monitorToggle !== sandboxId) return;
    const on = monitoredSandboxIds.has(sandboxId);
    button.classList.toggle("on", on);
    button.classList.toggle("off", !on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.textContent = on ? "On" : "Off";
  });
  syncMonitorRows();
  updateMonitorPanelState();
  updateMonitorTimer();
  renderMonitorDetails();
}
window.toggleMonitor = toggleMonitor;

function syncMonitorRows() {
  document.querySelectorAll("[data-monitor-row]").forEach((row) => {
    if (!monitoredSandboxIds.has(row.dataset.monitorRow || "")) {
      row.remove();
    }
  });
  document.querySelectorAll("[data-container-row]").forEach((row) => {
    const sandboxId = row.dataset.containerRow || "";
    if (!monitoredSandboxIds.has(sandboxId)) return;
    const next = row.nextElementSibling;
    if (!next || next.dataset.monitorRow !== sandboxId) {
      row.insertAdjacentElement("afterend", buildMonitorRow(sandboxId));
    }
  });
}

function updateMonitorPanelState() {
  const panel = $("containers-panel");
  if (!panel) return;
  panel.classList.toggle("monitoring", monitoredSandboxIds.size > 0);
}

function updateMonitorTimer() {
  clearInterval(monitorTimer);
  monitorTimer = null;
  if (!isDashboardActive() || document.hidden || monitoredSandboxIds.size === 0) {
    return;
  }
  loadMonitoring();
  monitorTimer = setInterval(loadMonitoring, 5000);
}

async function loadMonitoring() {
  if (monitorInFlight || !isDashboardActive() || document.hidden || monitoredSandboxIds.size === 0) {
    return;
  }
  monitorInFlight = true;
  try {
    const ids = Array.from(monitoredSandboxIds).join(",");
    const data = await apiGet(`/api/monitor?ids=${encodeURIComponent(ids)}`);
    if (data && data.sandboxes) {
      monitorHost = data.host || null;
      Object.entries(data.sandboxes).forEach(([sandboxId, row]) => {
        monitorData[sandboxId] = row;
      });
      renderMonitorDetails();
    }
  } finally {
    monitorInFlight = false;
  }
}

function monitorDetailPlaceholder() {
  return `<span class="monitor-empty">Collecting runtime data...</span>`;
}

function renderMonitorDetails() {
  document.querySelectorAll("[data-monitor-detail]").forEach((target) => {
    const sandboxId = target.dataset.monitorDetail || "";
    const row = monitorData[sandboxId];
    target.innerHTML = row ? monitorDetailHtml(row) : monitorDetailPlaceholder();
  });
}

function monitorDetailHtml(row) {
  if (!row.running) {
    const reason = escapeHtml(row.error || "not running");
    return `<div class="monitor-head"><span class="badge">N/A</span><span class="monitor-source">${reason}</span></div>`;
  }
  const memory = row.memory || {};
  const cpu = row.cpu || {};
  const disk = row.disk || {};
  const history = Array.isArray(row.history) ? row.history : [];
  const memHistory = history.map((item) => item.mem_pct);
  const cpuHistory = history.map((item) => item.cpu_pct);
  const available = memory.available_pct == null
    ? "guest availability unavailable"
    : `available ${formatPct(memory.available_pct)} (${formatBytes(memory.available_bytes)})`;
  const memorySub = `${formatPct(memory.used_pct)} of limit; ${available}`;
  const cpuSub = `${formatPct(cpu.allocated_pct)} of ${cpu.allocated_vcpus || "?"} vCPU capacity`;
  const diskSub = `writable ${formatBytes(disk.writable_bytes)}; host free ${formatPct(disk.host_free_pct)}`;
  const reasons = monitorReasonLines(row);
  const processes = monitorProcessesHtml(row.processes);
  const source = row.health_source && row.health_source !== "none"
    ? `<span class="monitor-source">pressure source: ${escapeHtml(row.health_source)}</span>`
    : "";
  return `
    <div class="monitor-head">${monitorBadge(row)}${source}<span class="monitor-source">probe: ${escapeHtml(memory.probe || "unknown")}</span></div>
    <div class="monitor-metrics">
      ${monitorMetric("Memory", `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.limit_bytes)}`, memorySub, memHistory, [85, 95])}
      ${monitorMetric("CPU", formatPct(cpu.percent), cpuSub, cpuHistory, [80, 95])}
      ${monitorMetric("Disk", formatBytes(disk.writable_bytes), diskSub, [], [])}
    </div>
    ${reasons}
    ${processes}
  `;
}

function monitorBadge(row) {
  const reasons = new Set(row.reasons || []);
  const hostReasons = new Set((monitorHost && monitorHost.reasons) || []);
  const hostStatus = (monitorHost && monitorHost.status) || "unknown";
  let status = row.status || "unknown";
  if (hostStatus === "critical" || (hostStatus === "warn" && status === "ok")) {
    status = hostStatus;
  }
  let label = "OK";
  if (status === "unknown") {
    label = "N/A";
  } else if (reasons.has("guest_oom_kill")) {
    label = "OOM";
  } else if (reasons.has("guest_memory_available_low") || reasons.has("guest_memory_used_high") || reasons.has("guest_swap_used")) {
    label = "MEM";
  } else if (reasons.has("cpu_saturated") || reasons.has("cpu_pressure")) {
    label = "CPU";
  } else if (reasons.has("host_disk_low")) {
    label = "DISK";
  } else if (hostReasons.has("host_memory_low") || hostReasons.has("host_swap_used")) {
    label = "HOST";
  } else if (reasons.has("guest_probe_failed") || reasons.has("guest_probe_stale")) {
    label = "PARTIAL";
  } else if (status === "critical") {
    label = "CRIT";
  } else if (status === "warn") {
    label = "WARN";
  }
  let css = status === "critical" ? "bad" : (status === "warn" ? "warn" : (status === "ok" ? "ok" : ""));
  if (label === "PARTIAL" && css === "ok") css = "warn";
  return `<span class="badge ${css}">${label}</span>`;
}

function monitorReasonLines(row) {
  const labels = {
    guest_memory_available_low: "Guest memory is low; increase this VM's memory allocation.",
    guest_memory_used_high: "Guest memory usage is high.",
    guest_swap_used: "Guest is using swap.",
    guest_oom_kill: "Guest reported an OOM kill since boot.",
    host_memory_low: "Host memory is low; this may affect several VMs.",
    host_swap_used: "Host is using swap.",
    cpu_saturated: "CPU is close to the allocated capacity.",
    cpu_pressure: "Guest CPU pressure is elevated.",
    host_disk_low: "Host filesystem is low on free space.",
    guest_probe_failed: "Guest probe failed; only Docker-level data is available.",
    guest_probe_stale: "Guest probe data is stale.",
  };
  const items = [];
  (row.reasons || []).forEach((code) => {
    if (!labels[code]) return;
    const critical = row.status === "critical" && code !== "guest_probe_failed" && code !== "guest_probe_stale";
    const extra = code === "guest_oom_kill" && row.memory && row.memory.oom_kill_total != null
      ? ` (${row.memory.oom_kill_total} total)`
      : "";
    items.push(`<div class="${critical ? "critical" : ""}">${escapeHtml(labels[code] + extra)}</div>`);
  });
  (monitorHost && monitorHost.reasons || []).forEach((code) => {
    if (!labels[code]) return;
    items.push(`<div class="${monitorHost.status === "critical" ? "critical" : ""}">Host: ${escapeHtml(labels[code])}</div>`);
  });
  return items.length ? `<div class="monitor-reasons">${items.join("")}</div>` : "";
}

function monitorMetric(label, value, sub, history, guides) {
  return `
    <div class="monitor-metric">
      <div class="monitor-metric-head"><span>${escapeHtml(label)}</span><span>${escapeHtml(sub)}</span></div>
      <div class="monitor-metric-value">${escapeHtml(value)}</div>
      ${monitorSparkline(history, guides)}
    </div>
  `;
}

function monitorSparkline(values, guides) {
  const width = 96;
  const height = 26;
  const clean = (values || []).map((value) => Number(value)).filter((value) => Number.isFinite(value));
  const guideLines = (guides || []).map((guide) => {
    const y = height - 1 - Math.max(0, Math.min(100, guide)) / 100 * (height - 2);
    return `<line class="guide" x1="0" y1="${y.toFixed(2)}" x2="${width}" y2="${y.toFixed(2)}"></line>`;
  }).join("");
  if (!clean.length) {
    return `<svg class="monitor-spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">${guideLines}</svg>`;
  }
  const points = clean.map((value, index) => {
    const x = clean.length === 1 ? width / 2 : index / (clean.length - 1) * width;
    const y = height - 1 - Math.max(0, Math.min(100, value)) / 100 * (height - 2);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const area = `0,${height} ${points.join(" ")} ${width},${height}`;
  return `<svg class="monitor-spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">${guideLines}<polygon class="area" points="${area}"></polygon><polyline class="line" points="${points.join(" ")}"></polyline></svg>`;
}

function monitorProcessesHtml(processes) {
  if (!processes) {
    return `<div class="monitor-empty">Process list unavailable.</div>`;
  }
  const renderList = (title, items) => {
    const rows = (items || []).map((item) => `
      <div class="monitor-proc">
        <span>${escapeHtml(String(item.name || "?"))} <span class="muted">#${escapeHtml(String(item.pid || ""))}</span></span>
        <span>${formatBytes(item.rss_bytes)} · ${formatPct(item.cpu_pct)}</span>
      </div>
    `).join("");
    return `<div><div class="monitor-proc-title">${escapeHtml(title)}</div><div class="monitor-proc-list">${rows || '<span class="monitor-empty">none</span>'}</div></div>`;
  };
  const stale = processes.stale ? `<span class="monitor-source">stale</span>` : "";
  return `<div class="monitor-head"><span class="monitor-source">top processes</span>${stale}</div>
    <div class="monitor-processes">
      ${renderList("By memory", processes.top_memory)}
      ${renderList("By CPU", processes.top_cpu)}
    </div>`;
}

function formatBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = number;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : (size >= 100 ? 0 : (size >= 10 ? 1 : 2));
  return `${size.toFixed(digits)} ${units[unit]}`;
}

function formatPct(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)}%` : "—";
}

function yesNoBadge(flag) {
  return flag
    ? `<span class="badge ok">YES</span>`
    : `<span class="badge bad">NO</span>`;
}

function renderImages(images) {
  const body = $("images-body");
  if (!body) return;
  body.innerHTML = "";

  if (!images || images.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8" class="muted">No configured images found</td>`;
    body.appendChild(tr);
    return;
  }

  images.forEach((img) => {
    const tr = document.createElement("tr");
    const refs = Array.isArray(img.sandboxes) ? img.sandboxes.join(", ") : "";
    const building = img.build_status === "building";
    tr.innerHTML = `
      <td>${escapeHtml(img.image || "")}</td>
      <td>${escapeHtml(img.path || "—")}</td>
      <td>${yesNoBadge(!!img.built)}</td>
      <td>${renderBuildStatus(img)}</td>
      <td>${yesNoBadge(!!img.has_dockerfile)}</td>
      <td>${yesNoBadge(!!img.has_app_py)}</td>
      <td>${escapeHtml(refs || "—")}</td>
      <td><div class="actions">${imageButtons(img.image || "", img.path || "", building)}</div></td>
    `;
    body.appendChild(tr);
  });
}

function setDashMessage(text, ok = false) {
  const el = $("dash-message");
  el.textContent = text || "";
  el.style.color = ok ? "var(--ok)" : "var(--warn)";
}

async function runAction(sandboxId, action) {
  const actionNames = {
    start: "Starting",
    stop: "Stopping",
    restart: "Restarting",
    recreate: "Recreating",
  };
  if (action === "recreate" && !window.confirm(
    `Recreate sndbx-${sandboxId}?\n\nThe container root filesystem will be replaced with the currently built image. `
    + "Configured mounts and volumes, including user data, are not modified. Rebuild and Update are not run."
  )) {
    return;
  }
  const actionName = actionNames[action] || "Updating";
  setDashMessage(`${actionName} sndbx-${sandboxId}...`, true);

  let res;
  try {
    res = await apiPost(`/api/sandbox/${encodeURIComponent(sandboxId)}/action`, { action });
  } catch (error) {
    setDashMessage(`${actionName} sndbx-${sandboxId} failed: ${error.message || error}`, false);
    return;
  }
  const payloadOk = !!(res.data && res.data.ok);
  if (!res.ok || !payloadOk) {
    setDashMessage((res.data && (res.data.message || res.data.detail || res.data.error)) || "Action failed", false);
    return;
  }
  setDashMessage((res.data && res.data.message) || "Action executed", true);
  await loadDashboard();
}
window.runAction = runAction;

async function runImageAction(imageRef, action) {
  const actionName = (action || "").toLowerCase();
  const verb = {
    build: "Build",
    rebuild: "Build --no-cache",
    update: "Update & rebuild",
    full_update: "Full update & rebuild",
  }[actionName] || "Build";

  if (actionName === "update" && !window.confirm(
    `Update and rebuild '${imageRef}'?\n\nDocker pulls fresh base images and rebuilds without cache. `
    + "No Git command runs and data/ is not modified. Recreate remains a separate action."
  )) {
    return;
  }

  if (actionName === "full_update" && !window.confirm(
    `Update and rebuild '${imageRef}'?\n\nOnly the image source Git root is fast-forwarded. `
    + "data/ is protected and will not be changed. Recreate remains a separate action."
  )) {
    return;
  }

  setDashMessage(`${verb} started for image '${imageRef}'...`, false);
  appendLocalLogLine(`[webui] ${verb} started for image '${imageRef}'`);

  const res = await apiPost(`/api/image/${encodeURIComponent(imageRef)}/action`, { action });
  const payloadOk = !!(res.data && res.data.ok);
  if (!res.ok || !payloadOk) {
    const errMsg = (res.data && (res.data.message || res.data.detail || res.data.error)) || "Image action failed";
    setDashMessage(errMsg, false);
    appendLocalLogLine(`[webui] ${verb} failed for image '${imageRef}': ${errMsg}`);
    return;
  }
  const okMsg = (res.data && res.data.message) || "Image action started";
  setDashMessage(okMsg, true);
  appendLocalLogLine(`[webui] ${verb} accepted for image '${imageRef}'`);
  await loadDashboard();
}
window.runImageAction = runImageAction;

async function restartService() {
  if (!window.confirm("Restart sndbx service now?")) {
    return;
  }

  const restartBtn = $("restart-service-btn");
  if (restartBtn) restartBtn.disabled = true;
  restartInProgress = true;
  suppressAutoLogout = true;

  setServiceIndicator("Restarting...", "neutral");
  setRestartBanner(true, "Service is restarting. Reconnecting...");
  appendLocalLogLine("[webui] Service restart requested from UI");
  let requestAccepted = false;
  try {
    const res = await apiPost("/api/service/restart", {});
    requestAccepted = !!res.ok;
    if (requestAccepted) {
      setDashMessage("Service restart requested. Waiting for service (0s)...", true);
    } else {
      const msg = (res.data && (res.data.detail || res.data.error)) || "Restart request response was not OK";
      setDashMessage(`${msg}. Waiting for service state...`, false);
      appendLocalLogLine(`[webui] Restart request returned non-OK response: ${msg}`);
    }
  } catch (e) {
    setDashMessage("Connection dropped while requesting restart. Waiting for service state...", false);
    appendLocalLogLine("[webui] Restart request connection dropped; continuing reconnect wait");
  }

  clearInterval(refreshTimer);
  refreshTimer = null;
  clearInterval(monitorTimer);
  monitorTimer = null;

  const reconnectDeadlineMs = Date.now() + 90_000;
  let online = false;
  let attempt = 0;
  while (Date.now() < reconnectDeadlineMs) {
    attempt += 1;
    const data = await apiGet("/api/status");
    const state = readServiceState(data);
    if (data && state === "online") {
      online = true;
      break;
    }
    if (data && state === "restarting") {
      const elapsedSec = Math.floor((90_000 - (reconnectDeadlineMs - Date.now())) / 1000);
      setServiceIndicator(`Restarting... ${elapsedSec}s`, "neutral");
      setDashMessage(`Service is restarting, reconnect attempt ${attempt}...`, true);
      setRestartBanner(true, `Service is restarting. Reconnect attempt ${attempt} (${elapsedSec}s)`);
      await new Promise((resolve) => window.setTimeout(resolve, 1200));
      continue;
    }

    const elapsedSec = Math.floor((90_000 - (reconnectDeadlineMs - Date.now())) / 1000);
    setServiceIndicator(`Restarting... ${elapsedSec}s`, "neutral");
    const baseText = requestAccepted
      ? "Service is restarting"
      : "Restart requested (response uncertain), waiting for service";
    setDashMessage(`${baseText}, reconnect attempt ${attempt}...`, true);
    setRestartBanner(true, `Service is restarting. Reconnect attempt ${attempt} (${elapsedSec}s)`);
    // Retry quickly while service restarts; keep current page rendered.
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
  }

  if (!online) {
    restartInProgress = false;
    suppressAutoLogout = false;
    setRestartBanner(false);
    setServiceIndicator("Unavailable", "bad");
    setDashMessage("Service restart is taking longer than expected", false);
    appendLocalLogLine("[webui] Service restart timeout while waiting for reconnect");
    if (restartBtn) restartBtn.disabled = false;
    return;
  }

  restartInProgress = false;
  suppressAutoLogout = false;
  setRestartBanner(false);
  await loadDashboard();
  setServiceIndicator("Online", "ok");
  setDashMessage("Service restarted successfully", true);
  appendLocalLogLine("[webui] Service restarted successfully");
  refreshTimer = setInterval(loadDashboard, 3000);
  updateMonitorTimer();
  if (isDashboardActive()) {
    loadSystemLog();
  }
  if (restartBtn) restartBtn.disabled = false;
}

async function loadDashboard() {
  const data = await apiGet("/api/status");
  if (!data) {
    if (restartInProgress) {
      setServiceIndicator("Restarting...", "neutral");
      return;
    }
    setServiceIndicator("Unavailable", "bad");
    return;
  }

  const serviceState = readServiceState(data);
  if (serviceState === "restarting") {
    restartInProgress = true;
    suppressAutoLogout = true;
    setServiceIndicator("Restarting...", "neutral");
    setRestartBanner(true, "Service is restarting. Waiting for online state...");
    renderHealth(data.health || {});
    renderContainers(data.containers || []);
    renderImages(data.images || []);
    $("whoami").textContent = data.session && data.session.login ? `User: ${data.session.login}` : "";
    return;
  }

  if (restartInProgress) {
    restartInProgress = false;
  }
  suppressAutoLogout = false;
  setRestartBanner(false);

  setServiceIndicator("Online", "ok");
  renderHealth(data.health || {});
  renderContainers(data.containers || []);
  renderImages(data.images || []);
  $("whoami").textContent = data.session && data.session.login ? `User: ${data.session.login}` : "";
}

function setLogMeta(text, ok = true) {
  const el = $("system-log-meta");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = ok ? "var(--muted)" : "var(--err)";
}

function appendLocalLogLine(text) {
  const out = $("system-log-output");
  if (!out || !text) return;

  const ts = new Date().toISOString().replace("T", " ").slice(0, 19);
  const line = `[${ts}] ${text}`;
  const prefix = out.textContent && !out.textContent.endsWith("\n") ? "\n" : "";
  out.textContent += prefix + line;

  const autoScroll = $("log-auto-scroll");
  if (autoScroll && autoScroll.checked) {
    out.scrollTop = out.scrollHeight;
  }
}

async function loadSystemLog() {
  const out = $("system-log-output");
  if (!out || !isDashboardActive()) {
    return;
  }

  const lines = 400;
  const data = await apiGet(`/api/system-log?lines=${lines}`);
  if (!data) {
    if (restartInProgress) {
      setLogMeta("Service restarting... reconnecting log stream", true);
      return;
    }
    setLogMeta("Log endpoint unavailable", false);
    return;
  }
  if (!data.ok) {
    out.textContent = "";
    setLogMeta((data.error || "Unable to read logs") + ` (source: ${data.source || "none"})`, false);
    return;
  }

  out.textContent = data.text || "(empty log)";
  setLogMeta(`Source: ${data.source || "unknown"} | lines: ${data.lines || lines}`, true);

  const autoScroll = $("log-auto-scroll");
  if (autoScroll && autoScroll.checked) {
    out.scrollTop = out.scrollHeight;
  }
}

async function repairKataRuntime() {
  if (!window.confirm("Try to repair Docker kata runtime now?")) {
    return;
  }

  setDashMessage("Running kata runtime repair...", true);
  const out = $("system-log-output");
  const meta = $("system-log-meta");

  const res = await apiPost("/api/runtime/kata/repair", {});
  if (!res.ok) {
    setDashMessage((res.data && (res.data.detail || res.data.error)) || "Repair request failed", false);
    if (meta) meta.textContent = "Repair request failed";
    return;
  }

  const data = res.data || {};
  const lines = [];
  lines.push(`[repair] ${data.message || (data.ok ? "ok" : "failed")}`);
  (data.report || []).forEach((line) => lines.push(String(line)));
  if (Array.isArray(data.manual_commands) && data.manual_commands.length) {
    lines.push("");
    lines.push("Manual commands:");
    data.manual_commands.forEach((cmd) => lines.push(cmd));
  }

  if (out) {
    out.textContent = lines.join("\n");
    const autoScroll = $("log-auto-scroll");
    if (autoScroll && autoScroll.checked) {
      out.scrollTop = out.scrollHeight;
    }
  }

  if (meta) {
    meta.textContent = data.ok ? "Repair completed successfully" : "Repair did not complete automatically";
    meta.style.color = data.ok ? "var(--muted)" : "var(--warn)";
  }

  setDashMessage(data.ok ? "Kata runtime repair completed" : "Kata runtime repair needs manual step", data.ok);
  await loadDashboard();
}

function updateLogTimer() {
  clearInterval(logTimer);
  logTimer = null;

  if (!isDashboardActive()) {
    return;
  }

  const auto = $("log-auto-update");
  const freq = $("log-refresh-ms");
  if (!auto || !freq || !auto.checked) {
    return;
  }

  const intervalMs = Math.max(500, parseInt(freq.value || "3000", 10));
  logTimer = setInterval(() => {
    loadSystemLog();
  }, intervalMs);
}

function setupLogPanel() {
  if (logPanelBound) {
    return;
  }

  const auto = $("log-auto-update");
  const freq = $("log-refresh-ms");
  const refresh = $("log-refresh-btn");
  const repair = $("kata-repair-btn");
  const scroll = $("log-auto-scroll");
  const out = $("system-log-output");

  if (!auto || !freq || !refresh || !repair || !scroll || !out) {
    return;
  }

  auto.addEventListener("change", () => {
    updateLogTimer();
  });

  freq.addEventListener("change", () => {
    updateLogTimer();
    loadSystemLog();
  });

  refresh.addEventListener("click", () => {
    loadSystemLog();
  });

  repair.addEventListener("click", () => {
    repairKataRuntime();
  });

  scroll.addEventListener("change", () => {
    if (scroll.checked) {
      out.scrollTop = out.scrollHeight;
    }
  });

  logPanelBound = true;
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function onLogin() {
  $("login-error").textContent = "";
  const login = $("login-input").value || "";
  const password = $("pass-input").value || "";
  const res = await apiPost("/api/auth/login", { login, password });
  if (!res.ok) {
    $("login-error").textContent = (res.data && res.data.detail) || "Invalid credentials";
    return;
  }
  showScreen("app");
  showPage("dashboard");
  await loadDashboard();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(loadDashboard, 3000);
}

async function onLogout() {
  await apiPost("/api/auth/logout", {});
  doLogout();
}

function doLogout() {
  disconnectConsole();
  clearInterval(refreshTimer);
  clearInterval(logTimer);
  clearInterval(monitorTimer);
  refreshTimer = null;
  logTimer = null;
  monitorTimer = null;
  monitorInFlight = false;
  monitoredSandboxIds.clear();
  monitorData = {};
  monitorHost = null;
  if (settingsTree) {
    settingsTree.destroy();
    settingsTree = null;
  }
  settingsDocumentsLoaded = false;
  showScreen("login");
}

function setConsoleStatus(text, ok = false) {
  const el = $("console-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = ok ? "var(--ok)" : "var(--warn)";
}

function initConsole() {
  if (consoleLoaded) {
    return;
  }

  if (!window.Terminal) {
    setConsoleStatus("xterm.js failed to load", false);
    return;
  }

  term = new window.Terminal({
    cursorBlink: true,
    theme: {
      background: "#0a0c12",
      foreground: "#d0d4e8",
      cursor: "#5b8af7",
    },
    fontFamily: "monospace",
    fontSize: 13,
    scrollback: 2000,
  });

  // FitAddon: auto-resize terminal to container element size.
  const FitAddonClass = window.FitAddon?.FitAddon;
  if (FitAddonClass) {
    fitAddon = new FitAddonClass();
    term.loadAddon(fitAddon);
  }

  term.open($("console-terminal"));
  if (fitAddon) fitAddon.fit();

  // Notify backend whenever xterm reports a dimension change.
  term.onResize(({ cols, rows }) => {
    if (termSocket && termSocket.readyState === WebSocket.OPEN) {
      termSocket.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });

  term.writeln("sndbx console ready. Select sandbox and click Connect.\r\n");

  $("console-connect-btn").addEventListener("click", connectConsole);
  $("console-disconnect-btn").addEventListener("click", disconnectConsole);
  loadConsoleSandboxes();

  consoleLoaded = true;
}

async function loadConsoleSandboxes() {
  const data = await apiGet("/api/status");
  if (!data) return;

  const select = $("console-sandbox");
  const containers = data.containers || [];
  select.innerHTML = "";
  containers.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.sandbox_id || "";
    opt.textContent = c.sandbox_id || "";
    select.appendChild(opt);
  });

  if (!containers.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No sandboxes";
    select.appendChild(opt);
    setConsoleStatus("No managed sandboxes available", false);
  }
}

function connectConsole() {
  if (!term) return;
  if (termSocket && termSocket.readyState === WebSocket.OPEN) {
    setConsoleStatus("Console already connected", true);
    return;
  }

  const sandboxId = ($("console-sandbox").value || "").trim();
  if (!sandboxId) {
    setConsoleStatus("Select sandbox first", false);
    return;
  }

  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${window.location.host}/ws/console/${encodeURIComponent(sandboxId)}`;
  termSocket = new WebSocket(url);

  termSocket.onopen = () => {
    setConsoleStatus(`Connected to ${sandboxId}`, true);
    term.focus();
    if (termDataDisposable) {
      termDataDisposable.dispose();
      termDataDisposable = null;
    }
    termDataDisposable = term.onData((data) => {
      if (termSocket && termSocket.readyState === WebSocket.OPEN) {
        termSocket.send(data);
      }
    });

    // Force initial terminal size sync so first render is full-size without manual window resize.
    syncConsoleSizeToBackend();
    window.setTimeout(syncConsoleSizeToBackend, 120);
  };

  termSocket.onmessage = (evt) => {
    term.write(evt.data || "");
  };

  termSocket.onerror = () => {
    setConsoleStatus("Console connection error", false);
  };

  termSocket.onclose = () => {
    setConsoleStatus("Console disconnected", false);
    if (termDataDisposable) {
      termDataDisposable.dispose();
      termDataDisposable = null;
    }
    termSocket = null;
  };
}

function disconnectConsole() {
  if (termSocket) {
    termSocket.close();
    termSocket = null;
  }
  if (termDataDisposable) {
    termDataDisposable.dispose();
    termDataDisposable = null;
  }
}

function syncConsoleSizeToBackend() {
  if (!term || !termSocket || termSocket.readyState !== WebSocket.OPEN) {
    return;
  }

  if (fitAddon) {
    fitAddon.fit();
  }

  termSocket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
}

// MCP Test Functions
function mcpSetStatus(text, ok = false) {
  const status = $("mcp-status");
  if (status) {
    status.textContent = text;
    status.style.color = ok ? "#4caf86" : "#f0a500";
  }
}

function mcpLoadTemplate(template) {
  const editor = $("mcp-query-editor");
  const templates = {
    "tools-list": { jsonrpc: "2.0", id: 1, method: "tools/list" },
    "resources-list": { jsonrpc: "2.0", id: 1, method: "resources/list" },
    "prompts-list": { jsonrpc: "2.0", id: 1, method: "prompts/list" },
    "execute-ls-al": {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "execute_command",
        arguments: {
          command: "ls -al",
        },
      },
    },
    "custom": { jsonrpc: "2.0", id: 1, method: "", params: {} },
  };
  if (editor) {
    editor.value = JSON.stringify(templates[template] || templates["custom"], null, 2);
  }
}

function mcpFormatJSON() {
  const editor = $("mcp-query-editor");
  if (!editor) return;
  try {
    const obj = JSON.parse(editor.value);
    editor.value = JSON.stringify(obj, null, 2);
    mcpSetStatus("JSON formatted ✓", true);
  } catch (e) {
    mcpSetStatus("Invalid JSON: " + e.message, false);
  }
}

function mcpDisplayResults(data) {
  const results = $("mcp-results");
  if (results) {
    results.textContent = JSON.stringify(data, null, 2);
  }
}

function mcpGetCheckedValue(name, fallback) {
  const selected = document.querySelector(`input[name="${name}"]:checked`);
  return selected ? selected.value : fallback;
}

function mcpReadConnectionSettings() {
  const urlInput = $("mcp-server-url");
  const tokenInput = $("mcp-token");
  return {
    serverUrl: ((urlInput && urlInput.value) || "").trim(),
    token: ((tokenInput && tokenInput.value) || "").trim(),
    execution: mcpGetCheckedValue("mcp-execution", "backend"),
    transport: mcpGetCheckedValue("mcp-mode", "http"),
  };
}

function mcpBuildDefaultUrl(connectInfo) {
  const cfgPort = Number((connectInfo && connectInfo.port) || 30081);
  const port = Number.isFinite(cfgPort) && cfgPort > 0 ? cfgPort : 30081;

  const currentHost = (window.location.hostname || "").trim();
  let host = ((connectInfo && connectInfo.default_host) || "").trim();
  if (currentHost && currentHost !== "0.0.0.0" && currentHost !== "::" && currentHost !== "[::]") {
    host = currentHost;
  }
  if (!host || host === "0.0.0.0" || host === "::" || host === "[::]") {
    host = "127.0.0.1";
  }

  return `http://${host}:${port}`;
}

async function mcpInitConnectionDefaults() {
  const urlInput = $("mcp-server-url");
  if (!urlInput || (urlInput.value || "").trim()) {
    return;
  }

  const connectInfo = await apiGet("/api/mcp/connect-info");
  urlInput.value = mcpBuildDefaultUrl(connectInfo || {});
}

async function mcpCallBackend(request, settings) {
  const method = (request && request.method) || "";
  const params = (request && request.params) || {};
  const resp = await apiPost("/api/mcp/call", {
    request,
    // Compatibility fields for older backends.
    method,
    params,
    server_url: settings.serverUrl,
    token: settings.token,
  });
  if (!resp.ok && !(resp.data && resp.data.error)) {
    return { error: `Backend HTTP ${resp.status}` };
  }
  return resp.data;
}

async function mcpGetToolsBackend(settings) {
  const resp = await apiPost("/api/mcp/tools", {
    server_url: settings.serverUrl,
    token: settings.token,
  });
  if (!resp.ok && !(resp.data && resp.data.error)) {
    return { error: `Backend HTTP ${resp.status}` };
  }
  return resp.data;
}

async function mcpCallFrontendDirect(request, settings) {
  if (!settings.serverUrl) {
    return { error: "MCP server URL is required for frontend mode" };
  }

  const headers = {
    "Content-Type": "application/json",
  };
  if (settings.token) {
    headers.Authorization = /^Bearer\s+/i.test(settings.token)
      ? settings.token
      : `Bearer ${settings.token}`;
  }
  if (settings.transport === "sse") {
    headers.Accept = "text/event-stream, application/json";
  }

  try {
    const response = await fetch(settings.serverUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(request),
    });

    const contentType = (response.headers.get("content-type") || "").toLowerCase();
    let payload;
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => ({}));
    } else {
      const text = await response.text().catch(() => "");
      payload = { raw: text };
    }

    if (!response.ok) {
      return {
        error: `Frontend direct HTTP ${response.status}`,
        response: payload,
      };
    }
    return payload;
  } catch (e) {
    return { error: e.message || String(e) };
  }
}

async function mcpGetTools() {
  try {
    await mcpInitConnectionDefaults();
    const settings = mcpReadConnectionSettings();
    mcpSetStatus(`Loading tools (${settings.execution}/${settings.transport})...`, false);

    const resp = settings.execution === "frontend"
      ? await mcpCallFrontendDirect({ jsonrpc: "2.0", id: 1, method: "tools/list" }, settings)
      : await mcpGetToolsBackend(settings);

    mcpDisplayResults(resp);
    mcpSetStatus(resp && resp.error ? `Error: ${resp.error}` : "Tools loaded", !(resp && resp.error));
  } catch (e) {
    mcpSetStatus("Error: " + e.message, false);
    mcpDisplayResults({ error: e.message });
  }
}

async function mcpSendRequest() {
  const editor = $("mcp-query-editor");
  if (!editor) return;

  try {
    const request = JSON.parse(editor.value);
    await mcpInitConnectionDefaults();
    const settings = mcpReadConnectionSettings();
    mcpSetStatus(`Sending request (${settings.execution}/${settings.transport})...`, false);

    const resp = settings.execution === "frontend"
      ? await mcpCallFrontendDirect(request, settings)
      : await mcpCallBackend(request, settings);

    mcpDisplayResults(resp);
    mcpSetStatus(resp && resp.error ? `Error: ${resp.error}` : "Request sent", !(resp && resp.error));
  } catch (e) {
    mcpSetStatus("Error: " + e.message, false);
    mcpDisplayResults({ error: e.message });
  }
}

function mcpClearResults() {
  const results = $("mcp-results");
  const editor = $("mcp-query-editor");
  if (results) results.textContent = "";
  if (editor) editor.value = "";
  mcpSetStatus("Cleared", true);
}

// Init MCP test page
function initMCPTest() {
  const getToolsBtn = $("mcp-get-tools-btn");
  const sendBtn = $("mcp-send-btn");
  const formatBtn = $("mcp-format-btn");
  const clearBtn = $("mcp-clear-results-btn");
  const serverUrlInput = $("mcp-server-url");

  if (getToolsBtn) getToolsBtn.addEventListener("click", mcpGetTools);
  if (sendBtn) sendBtn.addEventListener("click", mcpSendRequest);
  if (formatBtn) formatBtn.addEventListener("click", mcpFormatJSON);
  if (clearBtn) clearBtn.addEventListener("click", mcpClearResults);
  if (serverUrlInput) {
    serverUrlInput.addEventListener("focus", () => {
      if (!serverUrlInput.value.trim()) {
        mcpInitConnectionDefaults();
      }
    });
  }

  // Set default template
  mcpLoadTemplate("tools-list");
  mcpInitConnectionDefaults();
}

$("login-btn").addEventListener("click", onLogin);
$("pass-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") onLogin();
});
$("login-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") onLogin();
});
$("logoff-btn").addEventListener("click", onLogout);
$("restart-service-btn").addEventListener("click", restartService);
setupTablePanels();

// Re-fit terminal on window resize so cols/rows stay in sync.
window.addEventListener("resize", () => {
  if (fitAddon) fitAddon.fit();
});

document.addEventListener("visibilitychange", () => {
  updateMonitorTimer();
});

(async () => {
  const me = await apiGet("/api/auth/me");
  if (me && me.ok) {
    showScreen("app");
    showPage("dashboard");
    initMCPTest();  // Initialize MCP test page
    await loadDashboard();
    refreshTimer = setInterval(loadDashboard, 3000);
  } else {
    showScreen("login");
  }
})();
