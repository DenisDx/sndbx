"use strict";

const $ = (id) => document.getElementById(id);
let refreshTimer = null;
let term = null;
let termSocket = null;
let consoleLoaded = false;
let termDataDisposable = null;

function showScreen(name) {
  $("login-screen").style.display = name === "login" ? "flex" : "none";
  $("app-screen").style.display = name === "app" ? "flex" : "none";
}

function showPage(name) {
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll("nav button[id^='nav-']").forEach((b) => b.classList.remove("active"));
  $("page-" + name).classList.add("active");
  $("nav-" + name).classList.add("active");

  if (name === "console") {
    initConsole();
  }
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
      doLogout();
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

function vmButtons(sandboxId) {
  const mk = (label, action) =>
    `<button onclick="runAction('${sandboxId}','${action}')">${label}</button>`;

  return [
    mk("Start", "start"),
    mk("Stop", "stop"),
    mk("Restart", "restart"),
  ].join("");
}

function sshButtons(sandboxId) {
  const mk = (label, action) =>
    `<button onclick="runAction('${sandboxId}','${action}')">${label}</button>`;

  return [
    mk("Open SSH", "ssh_open"),
    mk("Close SSH", "ssh_close"),
  ].join("");
}

function renderStartupFlag(enabled) {
  return enabled
    ? `<span class="badge ok">YES</span>`
    : `<span class="badge">NO</span>`;
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
      <td>${renderStartupFlag(!!c.run_at_startup)}</td>
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

function setDashMessage(text, ok = false) {
  const el = $("dash-message");
  el.textContent = text || "";
  el.style.color = ok ? "var(--ok)" : "var(--warn)";
}

async function runAction(sandboxId, action) {
  const res = await apiPost(`/api/sandbox/${encodeURIComponent(sandboxId)}/action`, { action });
  if (!res.ok) {
    setDashMessage((res.data && (res.data.detail || res.data.error)) || "Action failed", false);
    return;
  }
  setDashMessage("Action executed", true);
  await loadDashboard();
}
window.runAction = runAction;

async function restartService() {
  if (!window.confirm("Restart sndbx service now?")) {
    return;
  }

  setServiceIndicator("Restarting...", "neutral");
  const res = await apiPost("/api/service/restart", {});
  if (!res.ok) {
    setServiceIndicator("Online", "ok");
    setDashMessage((res.data && (res.data.detail || res.data.error)) || "Service restart failed", false);
    return;
  }

  setDashMessage("Service restart requested. Reconnecting...", true);
  clearInterval(refreshTimer);
  refreshTimer = null;
  window.setTimeout(() => {
    window.location.reload();
  }, 2500);
}

async function loadDashboard() {
  const data = await apiGet("/api/status");
  if (!data) {
    setServiceIndicator("Unavailable", "bad");
    return;
  }
  setServiceIndicator("Online", "ok");
  renderHealth(data.health || {});
  renderContainers(data.containers || []);
  $("whoami").textContent = data.session && data.session.login ? `User: ${data.session.login}` : "";
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
  refreshTimer = null;
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
  term.open($("console-terminal"));
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

$("login-btn").addEventListener("click", onLogin);
$("pass-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") onLogin();
});
$("login-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") onLogin();
});
$("logoff-btn").addEventListener("click", onLogout);
$("restart-service-btn").addEventListener("click", restartService);

(async () => {
  const me = await apiGet("/api/auth/me");
  if (me && me.ok) {
    showScreen("app");
    showPage("dashboard");
    await loadDashboard();
    refreshTimer = setInterval(loadDashboard, 3000);
  } else {
    showScreen("login");
  }
})();
