# Proposal: per-VM runtime monitoring

Status: implemented on 2026-10-22. Live endpoint and dashboard assets verified.

Date: 2026-10-22.

Primary objective: detect resource shortage for a sandbox - above all, an
insufficient memory allocation - before or immediately after the guest starts
struggling or is OOM-killed. The charts are a means to that end, not the goal.

## 1. Executive summary

Add an opt-in **Monitor** switch to each row of the dashboard
**Current containers** table. When a row is monitored, the table grows by one
detail row for that VM and displays:

- memory usage, memory limit, and a small history graph;
- CPU usage relative to the allocated vCPUs, with a small history graph;
- best-effort disk information;
- a resource-health badge and a short explanation;
- the top memory and CPU processes inside the guest.

The collection path is deliberately conservative:

1. **Docker's own stats** provide CPU, memory usage/limit, network I/O, and PID
   count. This requires no agent inside the guest.
2. **A small read-only guest probe** is run only while monitoring is enabled.
   It reads `/proc/meminfo` and the guest cgroup counters to distinguish real
   memory shortage from page-cache pressure, and detects OOM kills. The same
   probe can collect the top processes.
3. **Host memory pressure** is read from `/proc/meminfo` and PSI, if available.
   This separates "this VM needs more RAM" from "the host is out of RAM".

No guest daemon, exporter, image change, or privileged container is required.

## 2. Goals and non-goals

### Goals

- Show which running sandbox is short of memory, CPU, or disk.
- Make a too-small `memory` allocation visible before the next OOM kill.
- Distinguish guest memory pressure from host-wide memory pressure.
- Keep the dashboard useful with a short, readable history.
- Add monitoring without changing sandbox lifecycle, networking, images, or
  configuration schema.
- Keep the collection cost negligible when monitoring is off.
- Degrade gracefully when Docker, Kata, or the guest probe is unavailable.

### Non-goals

- Persistent metrics storage, Prometheus, Grafana, or a long-term time series.
- Continuous background monitoring or alerting while the dashboard is closed.
- Billing-grade accounting, per-process history, or historical process lists.
- Hard per-VM disk quotas. Current Kata/virtiofs behavior does not expose a
  reliable per-VM disk quota through `docker stats`.
- Network traffic accounting. `NetIO` can be displayed as a secondary value,
  but it is not a shortage signal in this proposal.

## 3. Observations from the current installation

These checks were performed on 2026-10-22 against running Kata 4.1 sandboxes.
They drive the design.

- `docker stats --no-stream` works for Kata containers and returns plausible
  guest CPU and memory values. Example: one VM reported
  `2.805GiB / 4GiB` while its guest cgroup reported about `1.28GiB` of
  `memory.current`; the difference is accounted for by guest page cache and
  the runtime's accounting. CPU percentages and PID counts were also present.
- The host cgroup of a Kata container has `memory.max = max`; the configured
  limit exists in the guest cgroup. Therefore host cgroup files must not be
  used to compute a VM memory limit.
- `docker stats` `BlockIO` was `0B / 0B` for Kata containers. It is not a
  reliable disk signal.
- Guest `/proc/meminfo`, `/sys/fs/cgroup/memory.events`,
  `/sys/fs/cgroup/memory.swap.current`, and `ps` are readable through a
  read-only `docker exec`.
- Guest PSI files (`/sys/fs/cgroup/*.pressure`) were absent on the tested
  guest kernel. PSI must be optional.
- `df -P /` inside the guest reports the host-backed VirtioFS filesystem, not
  the configured `disk_max` value. Per-VM disk percentage is therefore not
  trustworthy in the current setup. Host filesystem free space and the Docker
  writable layer are available instead.

## 4. User interface

### 4.1 Switch placement

Recommended: one switch per container row, in a new **Monitor** column next
to **Auto-start**. This matches the per-VM goal and limits probing to the VMs
the user explicitly selected.

If a single panel-level switch was intended, the same backend and detail row
can be used; only the UI state changes from a set of IDs to "all running VMs".
This is listed as an open question in section 14.

### 4.2 Behaviour

- The switch is a button (`aria-pressed`) with the labels `Off` / `On`.
- The selected sandbox IDs live only in the current page's JavaScript memory.
  They are not written to `localStorage` or the server. After any page reload
  or a new browser tab, all switches are off and the user enables what is
  needed. No root configuration or schema change is needed.
- Turning monitoring on adds a detail row immediately below the container row.
  The `Current containers` card gains a `monitoring` class and grows in height.
  If the table is condensed, it is expanded automatically.
- Turning monitoring off removes the detail row and stops polling for that
  sandbox.
- A stopped container keeps its switch state, but the detail row shows
  `not running` and no chart is sampled.
- Monitoring state is a UI preference. It does not survive a different browser
  profile unless that profile is synchronised.

### 4.3 Detail row

```text
Current containers                                             Advance

Sandbox   Monitor  Status   Image   Docker ports  SSH port  VM  SSH
paperclip  [ On ]  Up ...   ...     ...           ...       ... ...
  HEALTH: OK
  Memory  1.33 / 4.00 GiB  33%   [sparkline]   available 2.77 GiB
  CPU     5.7% of 200% (2 vCPU)   [sparkline]
  Disk    writable 108 KiB; data FS 1.5 TiB free
  Top memory: 1 node 512 MiB; 2 postgres 96 MiB; ...
  Top CPU:    1 node 12%; 2 postgres 3%; ...
```

The row must remain readable on a narrow screen: metrics wrap into a vertical
list, and the process lists become collapsible.

### 4.4 Badge semantics

| Badge | Meaning |
|---|---|
| `OK` | No warning or critical condition. |
| `MEM` | Guest memory is under pressure, but the host still has memory. Increase the VM allocation. |
| `MEM?` | Only coarse `docker stats` data is available; page cache may inflate usage. |
| `OOM` | The guest reported at least one OOM kill since boot. |
| `HOST` | Host memory is under pressure. The VM may be fine; the host is not. |
| `CPU` | CPU usage is close to the allocated vCPU capacity. |
| `DISK` | The backing host filesystem is low on free space. |
| `N/A` | No data. The container is stopped or Docker/probe failed. |

The badge is the worst active condition, but the detail row lists every active
reason. A critical condition must not be hidden by a lower-priority label.

### 4.5 Sparklines

- Pure inline SVG, about 96 x 26 pixels; no chart library, no CDN dependency.
- Memory: `used / limit` percentage, 0-100 %, with light guide lines at the
  warning and critical thresholds.
- CPU: percentage of allocated capacity, 0-100 %, with guide lines.
- 120 history points. At a 5 s primary interval this is 10 minutes.
- History starts when the browser starts polling. It is intentionally not
  persisted across a service restart.
- Disk in v1 is displayed as text, not as a graph, because there is no
  reliable per-VM disk ratio. A host-free-space graph can be added after the
  backend proves stable.

## 5. API

### 5.1 Endpoint

```text
GET /api/monitor?ids=sandbox-1,paperclip
Cookie: sndbx_session=<session>
```

- Authentication: the same session dependency as `/api/status`.
- `ids` is optional. When absent, all configured sandbox IDs may be returned
  for compatibility, but the UI always sends an explicit list.
- Validate each ID against the configured sandbox map and the existing sandbox
  ID pattern. Reject unknown IDs with HTTP 400 instead of turning them into
  container names.
- Deduplicate, preserve input order, and cap the list at 32 IDs.
- The endpoint is a normal `def` handler so FastAPI runs the blocking Docker
  calls in its thread pool. It must never block the event loop.
- Return partial data on per-sandbox errors; one broken guest must not fail the
  whole response.

### 5.2 Response

```json
{
  "generated_at": "2026-10-22T11:00:05Z",
  "sample_interval_seconds": 5,
  "host": {
    "mem_total_bytes": 64950796288,
    "mem_available_bytes": 38852403200,
    "swap_total_bytes": 35433480192,
    "swap_used_bytes": 232783872,
    "pressure_some_avg60": 0.0,
    "mem_available_pct": 59.8,
    "status": "ok",
    "reasons": []
  },
  "sandboxes": {
    "paperclip": {
      "sandbox_id": "paperclip",
      "running": true,
      "collected_at": "2026-10-22T11:00:05Z",
      "status": "ok",
      "reasons": [],
      "memory": {
        "used_bytes": 1433047040,
        "limit_bytes": 4294967296,
        "used_pct": 33.4,
        "available_bytes": 2904588288,
        "available_pct": 70.4,
        "swap_used_bytes": 0,
        "oom_kill_total": 0,
        "probe": "ok"
      },
      "cpu": {
        "percent": 5.65,
        "allocated_vcpus": 2,
        "allocated_pct": 2.83
      },
      "disk": {
        "writable_bytes": 110592,
        "host_free_bytes": 1554595741696,
        "host_total_bytes": 1884897116160,
        "host_free_pct": 82.5,
        "source": "images/paperclip"
      },
      "network": {
        "rx_bytes": 23800,
        "tx_bytes": 6150
      },
      "pids": 39,
      "uptime_seconds": 172800,
      "processes": {
        "probed_at": "2026-10-22T11:00:00Z",
        "top_memory": [
          {"pid": 2, "name": "MainThread", "rss_bytes": 536870912, "cpu_pct": 2.2}
        ],
        "top_cpu": [
          {"pid": 2, "name": "MainThread", "rss_bytes": 536870912, "cpu_pct": 2.2}
        ]
      },
      "history": [
        {
          "ts": "2026-10-22T11:00:05Z",
          "mem_pct": 33.4,
          "cpu_pct": 2.83,
          "available_pct": 70.4
        }
      ],
      "error": null
    }
  }
}
```

Notes:

- `percent` is Docker's `CPUPerc`, where 100 % is one fully busy CPU. The UI
  shows `allocated_pct` (Docker percent divided by allocated vCPUs) so the
  sparkline always has the same 0-100 % scale.
- `memory.used_pct` includes reclaimable page cache. `available_pct` from the
  guest probe is the primary shortage signal when it is available.
- `oom_kill_total` is cumulative since guest boot. It is not a per-sample delta,
  so an old OOM remains visible and can be explained by the user.
- The UI shows only the process name (`comm`), PID, RSS, and CPU. Full command
  lines and their arguments are not collected or returned, so tokens or secrets
  in argv are not exposed. Metrics must never be written to logs at info level.

### 5.3 History and caching

- Keep a per-sandbox ring buffer of 120 samples in memory.
- Append at most one primary sample per sandbox every 5 seconds.
- Cache the whole endpoint response for 2 seconds. Two browsers polling at the
  same time must not multiply Docker calls.
- Deep probe results are cached with the sample and refreshed every 15 seconds.
- Docker size probes run every 60 seconds.
- Evict sandbox history after 10 minutes without a request.
- No new database, file, or background service is introduced.

## 6. Collection design

### 6.1 New module

Add `src/monitoring.py` with a `SandboxMonitor` class:

- `collect(sandbox_ids) -> dict` - validate, sample, update history, return the
  API payload.
- `_primary_stats(ids)` - one Docker stats call for all requested running IDs.
- `_host_stats()` - host memory, swap, PSI, and optional disk.
- `_guest_probe(sandbox_id)` - one read-only `docker exec` per VM when due.
- `_disk_stats(sandbox_id)` - low-frequency Docker writable layer and host
  filesystem values.
- `_evaluate(sample)` - status, reasons, and thresholds.
- `_append_history(sandbox_id, sample)` - bounded ring buffer.

The monitor receives the sandbox manager and config. It does not call the
Docker CLI directly; the manager exposes small public helpers so tests can
replace them.

### 6.2 Sandbox manager additions

Add read-only methods to `DockerSandboxManager`:

```python
def get_container_stats(self, sandbox_ids: list[str]) -> tuple[bool, str]:
    """Return raw `docker stats --no-stream` JSON rows for running sandboxes."""

def probe_guest_metrics(self, sandbox_id: str) -> tuple[bool, str]:
    """Run the fixed read-only guest probe and return its raw output."""

def get_container_disk_size(self, sandbox_id: str) -> tuple[bool, str]:
    """Return Docker SizeRw in bytes for one container."""
```

Each method uses an explicit argument list and a short timeout. No method
accepts a shell command from the caller.

### 6.3 Primary Docker stats

One call for all monitored running containers:

```bash
docker stats --no-stream --format '{{json .}}' \
  sndbx-paperclip sndbx-open-webui
```

Fields used:

| Field | Use |
|---|---|
| `MemUsage` | Used and limit bytes. |
| `MemPerc` | Cross-check; not trusted alone. |
| `CPUPerc` | CPU percentage. |
| `NetIO` | Optional secondary display. |
| `PIDs` | Process count. |

`BlockIO` is parsed but not displayed as a health signal because it is zero on
the tested Kata setup.

`docker stats` is started only for running sandboxes. Pass only names built
from validated IDs (`sndbx-<id>`). If the command fails or times out, return a
per-sandbox `error` and keep the previous history.

### 6.4 Guest probe

Run only for running, monitored VMs, at most every 15 seconds. The command is
fixed and read-only:

```bash
docker exec sndbx-paperclip sh -c '
printf "@@MEMINFO\n"; cat /proc/meminfo 2>/dev/null || true
printf "@@MEMEVENTS\n"; cat /sys/fs/cgroup/memory.events 2>/dev/null || true
printf "@@SWAP\n"; cat /sys/fs/cgroup/memory.swap.current 2>/dev/null || true
printf "@@PSIMEM\n"; cat /sys/fs/cgroup/memory.pressure 2>/dev/null || true
printf "@@PSICPU\n"; cat /sys/fs/cgroup/cpu.pressure 2>/dev/null || true
printf "@@UPTIME\n"; cat /proc/uptime 2>/dev/null || true
printf "@@PROCS\n"; ps -eo pid=,comm=,%cpu=,rss= 2>/dev/null || true
'
```

The output is parsed in Python. Missing sections are allowed. A guest without
`ps` or `/sys/fs/cgroup` still provides the Docker-primary metrics.

Derived values:

- `MemAvailable` / `MemTotal` - primary guest memory-shortage signal;
- `SwapTotal`, `SwapFree`;
- `memory.events` `oom`, `oom_kill`, `max` counters;
- `/sys/fs/cgroup/memory.swap.current` when present;
- optional PSI `some avg60` values;
- process list sorted in Python: top 5 by `rss`, top 5 by `%cpu`.

`ps` `%cpu` is the process lifetime average, not an instantaneous sample. It is
good enough for a short "top processes" hint. Deriving true instantaneous CPU
would require two `/proc/<pid>/stat` reads and an extra delay; that is not part
of v1.

The probe is skipped for a sandbox after a failure for 60 seconds to avoid
repeated Kata attach overhead. The primary metrics continue to update.

### 6.5 Host probe

Read directly in Python; no shell:

- `/proc/meminfo`: `MemTotal`, `MemAvailable`, `SwapTotal`, `SwapFree`;
- `/proc/pressure/memory`: `some avg60` when the host kernel exposes it;
- `os.statvfs` for the host filesystem backing the sandbox data directory.

Host values are included in every response, even when only one VM is monitored.
They identify host-wide pressure as the cause of several VMs struggling at once.

### 6.6 Disk (best effort)

- `writable_bytes`: `docker inspect --size` -> `SizeRw`. This is the container's
  writable layer, not the whole rootfs and not bind mounts.
- `host_free_bytes` / `host_total_bytes`: `statvfs` of
  `images/<local_image_id>` when it exists, otherwise the sandbox root.
- No per-VM percentage and no disk sparkline in v1.
- A low host free-space percentage is shown as `DISK` warning. The
  configured `disk_max` is not compared with `SizeRw`, because those values
  describe different things and are not a reliable quota under Kata/virtiofs.

If a true per-VM disk metric is required later, it needs a separate design
(quota enforcement plus a guest filesystem view), not a dashboard-only change.

### 6.7 Sampling schedule

| Data | Interval | Cost |
|---|---:|---|
| Docker stats | 5 s | One Docker CLI process for all monitored VMs |
| Host memory | 5 s | Two small file reads |
| Guest probe | 15 s | One `docker exec` per running monitored VM |
| Docker writable size | 60 s | One `docker inspect --size` per VM |
| API response cache | 2 s | Prevents duplicate work |

The first request for a VM samples immediately, including a deep probe, so the
detail row is populated without waiting 15 seconds.

## 7. Resource-shortage rules

The default thresholds below are starting values and should be constants in
`monitoring.py`, not hard-coded repeatedly in the UI or API.

### 7.1 Memory

| Signal | Warning | Critical |
|---|---|---|
| Guest `MemAvailable / MemTotal` | < 20 % | < 10 % |
| Guest cgroup usage / limit | >= 85 % | >= 95 % |
| Guest swap used / memory limit | >= 10 % | >= 25 % |
| Guest OOM kill counter | - | > 0 since boot |
| Host `MemAvailable / MemTotal` | < 15 % | < 8 % |

Rules:

- When the guest probe is available, `available_pct` drives the memory status.
  If only Docker stats are available, use `used_pct` with the `MEM?` label
  because reclaimable cache can make it look worse than it is.
- A warning must persist for two consecutive samples before it is shown, to
  avoid flicker. A critical condition is shown immediately.
- Hysteresis: a warning clears below 80 % usage (or above 22 % available); a
  critical condition clears below 90 % usage (or above 12 % available).
- `memory.events` counters are cumulative. `oom_kill_total > 0` is critical and
  remains so until the guest is recreated, so a past incident is not lost.
- If host memory is low while guest memory is healthy, set
  `health_source = "host"` and show `HOST`, not `MEM`, with the hint
  "host memory pressure; inspect all VMs". If both are low, show both reasons.
- If guest memory is low while host memory is plentiful, set
  `health_source = "guest"` and show `MEM` with the hint "increase this VM's
  memory allocation".

### 7.2 CPU

- `allocated_pct = CPUPerc / (allocated_vcpus * 100) * 100`.
- Warning: `allocated_pct >= 80 %` for three consecutive samples.
- Critical: `allocated_pct >= 95 %` for three consecutive samples.
- If guest PSI `cpu.pressure some avg60` is available and exceeds 20 %, add a
  `cpu pressure` reason. PSI is optional because the tested guest kernel did
  not expose it.
- A CPU warning explains high demand, not necessarily a too-small allocation.
  The UI wording must not tell the user to increase `cpus` unless CPU pressure
  or sustained saturation is present.

### 7.3 Disk

- Warning: host free space below 15 %.
- Critical: host free space below 5 %.
- Show writable-layer size as information only.

### 7.4 Status aggregation

The per-sandbox status is the worst active condition across memory, CPU, disk,
and probe errors. The `reasons` list contains stable machine-readable codes
such as:

```text
guest_memory_available_low
guest_memory_used_high
guest_swap_used
guest_oom_kill
host_memory_low
cpu_saturated
cpu_pressure
host_disk_low
guest_probe_failed
docker_stats_failed
```

The UI maps each code to a short human-readable line. Unknown codes are shown
verbatim but never inserted as raw HTML.

## 8. Frontend implementation

### 8.1 Files

- `src/webui/frontend/index.html`
  - add the **Monitor** column header;
  - add styles for the detail row, metric blocks, badges, and sparklines;
  - add an empty template or class hooks; rows are still rendered from JS.
- `src/webui/frontend/app.js`
  - maintain `monitoredSandboxIds` in page memory (no persistence);
  - render the switch in each container row;
  - insert/remove the detail `<tr>` under the matching container row;
  - start/stop the monitor polling loop;
  - render metrics, badges, sparklines, and process lists;
  - pause polling when the dashboard is inactive or the document is hidden.
- No new asset or npm dependency.

### 8.2 Rendering rules

- Do not rebuild the entire table on every monitor poll; update only the
  monitor rows. The existing dashboard status poll can still rebuild the main
  rows every 3 seconds.
- Keep the monitor poll independent from `loadDashboard`, with its own
  in-flight guard.
- Use `textContent` for process names, or an `escapeHtml` helper. Process names
  are untrusted container data.
- A failed request must not clear previous graphs; show a stale/error label.
- When a container stops, clear the graph and show `not running`; keep the
  switch on so it resumes after a manual start.

### 8.3 Polling lifecycle

```text
dashboard visible && monitoredSandboxIds.size > 0
    -> GET /api/monitor?ids=... every 5 s
otherwise
    -> no monitor requests
```

- Start polling when monitoring is enabled or the dashboard becomes active.
- Stop on logout, on navigation away from the dashboard, and when the document
  is hidden.
- Resume immediately on visibility change.
- Use one timer for all monitored VMs.

## 9. Kata and Docker compatibility

- `docker stats` is the supported, non-invasive path. The Kata shim reports
  guest cgroup values to Docker for CPU, memory, and PID count.
- Do not read the host cgroup's `memory.max` for a Kata VM: it is `max` even
  when the guest has a limit.
- Do not use `BlockIO` as a health signal for Kata.
- `docker exec` is the only guest-level operation. It is opt-in, read-only,
  time-limited, and failure-isolated. If a Kata guest cannot accept `exec`, the
  detail row still shows Docker-primary metrics and a `guest_probe_failed`
  reason.
- No Kata agent API is used directly. It is not a stable public integration
  surface for this project and would add version coupling.
- No image changes are required, so existing sandboxes do not need to be
  recreated.

## 10. Security and safety

- The monitor never starts, stops, restarts, recreates, or modifies a sandbox.
- All commands are fixed argument lists; no `shell=True`; no user-provided
  command text.
- Sandbox IDs are validated against configured IDs before constructing
  container names.
- `docker exec` runs a fixed POSIX `sh` script. It does not write files, change
  configuration, or install anything.
- Process names are untrusted data. Full command lines and arguments are never
  collected, returned, or logged, so tokens in argv are not exposed.
- Polling is rate-limited by the 2-second response cache and the 5-second
  sampler. The API caps the number of requested IDs.
- Per-sandbox failures are contained. A slow or broken guest must not make the
  dashboard or the service unhealthy.
- Metrics are in-memory only and disappear when the service restarts.
- No metrics are sent to an external service.

## 11. Implementation plan

### Phase 1 - backend primary metrics

1. Add parser utilities in `src/monitoring.py`:
   - Docker human-readable byte sizes;
   - `docker stats` JSON rows;
   - `/proc/meminfo`;
   - guest cgroup counters;
   - `ps` output.
2. Add `get_container_stats()` to `DockerSandboxManager`.
3. Add host memory and disk helpers.
4. Add focused unit tests with captured sample outputs.

### Phase 2 - guest memory and OOM probe

1. Add `probe_guest_metrics()` with a fixed script and timeout.
2. Parse `MemAvailable`, swap, `memory.events`, and optional PSI.
3. Add the failure circuit breaker and per-sandbox error reporting.
4. Add threshold and hysteresis tests.

### Phase 3 - API and history

1. Instantiate `SandboxMonitor` in `WebUIServer`.
2. Add the authenticated `/api/monitor` endpoint.
3. Add the 120-sample ring buffer, 2-second response cache, and ID validation.
4. Add HTTP tests for auth, unknown IDs, partial failures, and caching.

### Phase 4 - dashboard UI

1. Add the Monitor column and detail-row styles.
2. Implement the in-page switch state (no persistence) and polling lifecycle.
3. Render memory/CPU values and SVG sparklines.
4. Render the health badge, reasons, disk information, and stopped state.
5. Verify keyboard operation, `aria-pressed`, and narrow-screen wrapping.

### Phase 5 - top processes (included in v1)

1. Parse the process section from the same guest probe.
2. Show top 5 by RSS and top 5 by CPU, with PID, name, RSS, and CPU.
3. Show only the process name; do not collect or display full command lines.
4. If `ps` is unavailable or parsing fails, omit the lists without an error
   badge.

### Phase 6 - documentation and acceptance

1. Add the monitor procedure to `TEST.md`.
2. Update `README.md` with the dashboard feature and its limitations.
3. Update `DOCUMENTS.MD` with this proposal.
4. Update `memory/context.md` and `memory/history.md`.
5. Run the full test suite and a live check on a disposable VM.

## 12. Files expected to change

| File | Change |
|---|---|
| `src/monitoring.py` | New: sampling, parsing, history, thresholds, host metrics. |
| `src/sandbox.py` | Add read-only stats/probe/size helpers. |
| `src/webui_server.py` | Instantiate the monitor and add `/api/monitor`. |
| `src/webui/frontend/index.html` | Monitor column, detail-row and sparkline styles. |
| `src/webui/frontend/app.js` | Switches, polling, rendering, history graphs. |
| `tests/test_monitoring.py` | New parser, threshold, cache, and API tests. |
| `TEST.md` | Manual monitoring protocol. |
| `README.md` | Dashboard documentation and limitations. |
| `DOCUMENTS.MD` | Register this proposal. |
| `memory/context.md` | Durable design notes and open questions. |
| `memory/history.md` | Request and result record. |

No changes to `SPEC.md`, `AGENTS.md`, `config.descriptor.json5`, or individual
image repositories are planned for v1.

## 13. Testing plan

### Automated tests

Use captured fixture strings, not a live Docker daemon.

- Parse Docker sizes (`B`, `KiB`, `MiB`, `GiB`, `kB`, `MB`, `TB`).
- Parse `docker stats` rows, including malformed lines and missing fields.
- Parse `/proc/meminfo`, v2 `memory.events`, and optional missing files.
- Parse `ps` output with spaces in `comm` and a truncated `comm`.
- Compute memory/CPU/disk statuses and reasons.
- Verify host-vs-guest attribution.
- Verify warning debounce and critical hysteresis.
- Verify `oom_kill_total > 0` produces critical status.
- Verify the ring buffer keeps the newest 120 samples.
- Verify response caching prevents duplicate collection.
- Verify unknown IDs are rejected and per-sandbox failures are isolated.
- Verify `/api/monitor` requires authentication and returns 401 otherwise.
- Verify the manager uses argument lists and timeouts (mocked subprocess).

### Manual acceptance

Use a disposable sandbox, not a production VM.

1. Enable Monitor for one running VM. Confirm the detail row appears and the
   panel grows without breaking the table.
2. Compare memory and CPU values with `docker stats` and guest `free -m`.
3. Run a bounded memory workload inside the disposable VM and confirm the
   memory graph rises and `MEM` appears at the warning threshold.
4. If safe for the disposable VM, trigger a small OOM and confirm `OOM` and the
   OOM counter. Never do this on a production VM.
5. Run a CPU workload and confirm the CPU graph and `CPU` status.
6. Disable the switch and confirm requests stop in the browser network panel.
7. Hide the tab and confirm polling pauses; show it again and confirm resume.
8. Stop the VM and confirm the detail row shows `not running`; start it and
   confirm collection resumes.
9. Break the guest probe (for example, use a disposable image without `/bin/sh`)
   and confirm Docker-primary metrics remain and `guest_probe_failed` is shown.
10. Open two authenticated dashboards and confirm both show the same history
    without multiplying `docker stats` calls.

## 14. Resolved decisions

Confirmed by the user on 2026-10-22.

1. **Switch scope.** One switch per container row. All switches start off after
   the application/page is opened; the user enables only the VMs they need.
2. **Switch persistence.** No persistence. Switches reset on every page reload;
   state lives only in the current page's JavaScript memory.
3. **Collection lifetime.** Metrics are collected only while the Dashboard is
   open and at least one switch is on. No background sampler and no alerting
   while the UI is closed.
4. **Guest probe.** A fixed read-only `docker exec` every 15 seconds is
   approved. It provides `MemAvailable`, OOM counters, swap, uptime, and the
   process list.
5. **Top processes.** Included in v1: top 5 by RSS and top 5 by CPU. Only the
   process name is shown; full command lines are not collected.
6. **Thresholds.** Constant defaults in `src/monitoring.py`: memory usage
   85 % / 95 %, `MemAvailable` 20 % / 10 %, CPU 80 % / 95 %, host
   `MemAvailable` 15 % / 8 %, disk 15 % / 5 %. No per-sandbox configuration in
   v1.
7. **Notifications.** UI only: badge, reasons, values, and graphs. No system-log
   entries and no browser notifications.
8. **Disk.** Show host filesystem free space and Docker `SizeRw` as
   information. Do not attempt a per-VM disk percentage in v1.

Deliberately deferred: an always-on server sampler, persistent metric history,
configurable thresholds, browser notifications, and per-VM disk quotas.

## 15. Definition of done

- Monitoring is off by default and causes no Docker or guest calls when off.
- A monitored running VM shows memory, CPU, disk information, health status,
  and a 10-minute in-memory graph.
- Guest memory availability and OOM counters are used when available; coarse
  Docker values are clearly marked when they are not.
- Guest and host memory pressure are distinguished in the UI.
- Top processes are shown if the guest probe supports them.
- A stopped VM, a failed Docker call, and a failed guest probe each degrade
  gracefully without breaking the dashboard.
- No sandbox is modified, restarted, or recreated by monitoring.
- All automated tests pass, and the manual protocol is recorded in `TEST.md`.

## Appendix A - Why not the alternatives

| Alternative | Reason not chosen |
|---|---|
| Guest Prometheus node exporter in each VM | Requires image changes, a listening service, and per-VM lifecycle management. Too invasive. |
| cAdvisor or a host metrics daemon | Adds a service and does not reliably separate Kata guest memory from host cgroups. |
| Kata agent API directly | Not a stable project integration surface; adds Kata version coupling. |
| Poll everything with `docker exec` | High overhead and fragile if a guest cannot accept exec. Docker stats is cheaper for the primary path. |
| `docker stats` only | Cannot distinguish page cache from a real shortage and cannot report OOM kills. |
| Host cgroup files | Kata host cgroups show `memory.max = max`; the configured limit is in the guest. |
| `docker stats` `BlockIO` for disk | Reported `0B / 0B` on the tested Kata setup. |
| Persistent time-series database | Out of scope; 10 minutes of in-memory history is enough for the dashboard goal. |

## Appendix B - Example wireframe

```text
+--------------------------------------------------------------------------------------+
| CURRENT CONTAINERS                                                          [Advance] |
+----------+---------+---------+---------+---------+---------+------+------+-----------+
| Sandbox  | Monitor | Status  | Image   | Ports   | SSH     | VM   | SSH  |           |
+----------+---------+---------+---------+---------+---------+------+------+-----------+
| paperclip| [ On ]  | Up 2 d  | paper.. | 12022.. | 30201   | ...  | ...  |           |
|  +--------------------------------------------------------------------------------+  |
|  | HEALTH: OK   MEM 1.33/4.00 GiB 33%  [____/‾‾‾]  available 2.77 GiB             |  |
|  | CPU 5.7% of 200% (2 vCPU) [__/‾‾]   Disk writable 108 KiB; data FS 1.5 TiB free |  |
|  | Top memory: node 512 MiB | postgres 96 MiB | ...                                |  |
|  | Top CPU:    node 12%     | postgres 3%     | ...                                |  |
|  +--------------------------------------------------------------------------------+  |
| open-webui [ Off ]  | Up 2 d  | open..  | ...     | ...     | ...  | ...  |           |
+----------+---------+---------+---------+---------+---------+------+------+-----------+
```

## Appendix C - Exact probe command rationale

- `/proc/meminfo` is available in every Linux guest and gives `MemAvailable`,
  which includes reclaimable cache. This is the best signal that the guest is
  actually short of memory.
- `/sys/fs/cgroup/memory.events` gives cumulative `oom`, `oom_kill`, and `max`
  counters. A nonzero `oom_kill` is a definitive shortage event.
- `/sys/fs/cgroup/memory.swap.current` is useful only if the guest has swap;
  absence or `0` is normal.
- PSI files are optional and are not assumed to exist.
- `ps` is used for a human-readable culprit list; memory ranking uses `rss`,
  not `%mem`, because `%mem` is relative to the guest's total memory and can be
  misleading when the limit changes.
- The probe emits section markers so partial output can still be parsed.
