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
    loadSystemLog();
  } else {
    clearInterval(logTimer);
    logTimer = null;
  }

  if (name === "console") {
    initConsole();
    // Resize to actual element size when tab becomes visible.
    if (fitAddon) fitAddon.fit();
  }
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

function renderContainers(containers) {
  const body = $("containers-body");
  body.innerHTML = "";

  if (!containers || containers.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8" class="muted">No managed containers found</td>`;
    body.appendChild(tr);
    return;
  }

  containers.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(c.sandbox_id || "")}</td>
      <td>${autoStartButton(c.sandbox_id || "", !!c.run_at_startup)}</td>
      <td>${escapeHtml(c.status || "")}</td>
      <td>${escapeHtml(c.image || "")}</td>
      <td>${escapeHtml(c.ports || "")}</td>
      <td>${c.ssh_port ? String(c.ssh_port) : "—"}</td>
      <td><div class="actions">${vmButtons(c.sandbox_id || "")}</div></td>
      <td><div class="actions">${sshButtons(c.sandbox_id || "")}</div></td>
    `;
    body.appendChild(tr);
  });
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
  refreshTimer = null;
  logTimer = null;
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
