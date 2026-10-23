# Proposal: integrating `aiset` into sndbx

File version 1.4.0

Status: **implemented and deployed (2026-10-22).** The design below is the
plan of record; the actual delivery, live rollout, commands, deviations and
remaining operational notes are recorded in `execution_aiset_integration.md`.
aiset is the only configuration backend and the service has been restarted with
the trimmed root plus per-image overlays.

Pinned revision: `11d8838efe9e5cc447a9211c655a431fab810e8e` (`11d8838`,
"documents update + fix"), package version `0.1.0`.

Submodule layout:

```text
sndbx/
└── aiset/                 git submodule, https://github.com/DenisDx/aiset.git
    ├── AGENT_INTEGRATION.md
    ├── SPEC_service.md
    └── src/aiset/         importable package and assets
```

The controls for this work are the host rules in `AGENTS.md`, the integration
guide `aiset/AGENT_INTEGRATION.md`, and the normative interface
`aiset/SPEC_service.md`. When this plan and the library documentation disagree,
the library documentation wins and this plan is corrected.

The central deliverable of phase 1 is the **descriptor**: an exact, reviewed
schema for the root configuration and every per-image overlay. The descriptor is
not documentation; it is the completeness and type contract that decides whether
sndbx starts at all.

---

## 1. Executive summary

sndbx currently reads `config.json5` once at startup with a hand-written
substitution + `json5.loads` loader (`src/config.py`). There is no schema, no
revision check, no atomic write, no comment-preserving editor and no settings
UI (`src/webui/frontend/index.html:464` is a placeholder).

This proposal replaces that mechanism with `aiset` and introduces a per-image
overlay configuration:

| Concern | Today | After |
|---|---|---|
| Parse | regex substitution, then `json5.loads` | `aiset` source-preserving CST |
| Schema | none | `config.descriptor.json5` + `config.types.json5`, enforced on every load/write |
| Root config | one file with every sandbox's full settings | registry + global settings + **override values** (root wins) + host-owned `run_at_startup` |
| Per-VM config | `images/<id>/config.sndbx.json5`, merged into the root by hand | `images/<image-dir>/sndbx.overlay.json5`, the VM's defaults, merged under the root at startup |
| Writes | only `install.sh` rotates files; no app write path | atomic, revision-checked, comment-preserving store |
| Concurrency | none | `.lock` sidecar + `expected_revision` |
| Recovery | `install.sh` `.bak` rotation | `.back` chain, LKG, conflicted copies, rollback |
| Secrets | resolved into a plain dict, shown to every GUI user | `${VAR}` stays in the file; `secret: true` subtrees are permission-gated |
| Settings UI | placeholder card | generated `aiset-tree.js` editor with a document selector (root + per-image overlays) |
| In-memory config | `load_config()["config"]` dict | the same dict, produced by the root effective config merged with overlay effective configs |

The delivery is phased and **every phase leaves sndbx working**. The old loader
stays the default until a parity test proves the new backend produces the same
effective runtime configuration.

### 1.1 Resolved decisions

These are confirmed by the user and are not open questions:

| # | Decision |
|---|---|
| D1 | Auto-start moves from `data/sandbox_startup_overrides.json` to the root `config.json5`; the old file is imported once and retired. |
| D2 | Overlay files are `images/<image-dir>/sndbx.overlay.json5`; existing `images/<id>/config.sndbx.json5` are renamed and reconciled. |
| D3 | One overlay file per **image directory**; it may declare several sandbox ids. `images/default_sandbox_1` covers `sandbox-1` and `default-sandbox-1`. |
| D4 | An overlay is restricted to top-level `version`, `sandboxes` and optional `envids`; the sandbox object carries the VM defaults (including `run_at_startup`). The **root config wins every conflict**, like the aidir worker merge. |
| D5 | Merge: `aiset.merge(root_sandbox, overlay_sandbox, prefer="base")`. Objects deep-merge; on a non-object conflict the root wins. Keys present only in the overlay are added. |
| D6 | The host reads and merges overlays; the VM receives the already merged runtime configuration. |
| D7 | The overlay carries the VM's defaults; the root is the override layer and the sandbox registry. Auto-start remains in the root. |
| D8 | All 9 configured sandbox ids are covered by 8 overlay files, in waves. |
| D9 | aiset registers `main` plus one document per overlay file; the Web UI has a document selector. |
| D10 | The same full descriptor validates the root and every overlay; overlays are partial documents. Unknown keys are strict errors. |
| D11 | Secret: passwords, MCP/user tokens, the whole `envids` subtree, and `runtime_contract.environment`. |
| D12 | `logging.*` is live; `run_at_startup` is live and host-updated; everything else is `restart_required`. |
| D13 | `version` and `root` are read-only in the editor. |
| D14 | An authenticated session without `"all"` gets read-only access and no secret subtrees. |

---

## 2. Goals and non-goals

### Goals

1. Make `aiset` the single owner of every sndbx configuration document:
   `config.json5` and each `sndbx.overlay.json5`.
2. Preserve the current effective runtime configuration exactly. Overlays are a
   change of *where* VM-specific values live, not of *what* the host runs.
3. Replace the Web UI settings placeholder with the library editor, mounted over
   the real FastAPI application and authenticated by the existing session store.
4. Make the descriptor reviewable and testable: exact key inventory, exact types,
   exact `env.allowed` / `secret` / `apply` policy.
5. Keep the change inside one new package (`src/settings/`) plus descriptor,
   overlay and migration files. Do not spread transport or validation code
   through the host.
6. Leave a measurable gate: descriptor completeness, effective-config parity,
   revision conflicts, secret denial, asset delivery.

### Non-goals

- Reimplementing snapshots, operations, validation, revisions or the editor in
  sndbx (`aiset/AGENT_INTEGRATION.md` section 18).
- Changing documented sndbx parameter names or semantics from `SPEC.md`.
- Adding a second configuration format or a second source of truth per VM.
- Copying `aiset.js`, `aiset-tree.js` or `aiset.css` into `src/webui/frontend/`.
- Editing anything inside `aiset/` from this repository. Library changes are
  made in the aiset repository and pulled by advancing the submodule.
- Making the overlay override global sections (`logging`, `webui`, `mcp`,
  `users`, `ssh`). Overlays are per-VM, not per-process.
- Adding a live reload mechanism for the whole process. sndbx already has
  `POST /api/service/restart`; most settings legitimately require it.

---

## 3. Current state of the sndbx configuration subsystem

### 3.1 One loader, one call site

`src/config.py` owns everything:

| Member | Location | Contract used by the host |
|---|---|---|
| `load_env_file(path)` | `src/config.py:37` | parses `.env` into a `dict[str, str]`; no quotes/export handling |
| `expand_placeholders(value, env_vars)` | `src/config.py:55` | substitutes a single string value |
| `load_config_json5(path, env_vars)` | `src/config.py:83` | substitutes the **whole file text**, then `json5.loads` |
| `apply_sandbox_startup_overrides(config, root_dir)` | `src/config.py:19` | merges `data/sandbox_startup_overrides.json` into `sandboxes.items.*.run_at_startup` |
| `load_config(root_dir)` | `src/config.py:123` | returns `{"env": ..., "config": ...}` |

The only production call site is `src/app.py:73`. Tests and documentation use
`load_config()` as a smoke check (`TEST.md:58-70`).

### 3.2 What the rest of sndbx expects

- `config` is a plain nested `dict` with **resolved** placeholders.
- `config.get("sandboxes", {}).get("items", {})` is read repeatedly at runtime
  (`src/app.py:121,136,160`, `src/webui_server.py:390,639`).
- `self.config` is passed by reference to `DockerSandboxManager`, `MCPServer`,
  `ToolHandlers`, `BackupManager` and injected into `WebUIServer`.
- `self.config` is occasionally mutated in place
  (`WebUIServer._set_sandbox_auto_start`, `src/webui_server.py:395-410`).
- `config["webui"]["auth"]["users"]` is the login table
  (`src/webui_server.py:365`); `config["ssh"]["port_range"]` feeds the SSH pool
  (`src/webui_server.py:371`).

### 3.3 Write paths today

| Path | Writer | Notes |
|---|---|---|
| `config.json5` | `install.sh:35-86` | copies a `*.json5.example` template, rotates up to three `.bak` files |
| `data/sandbox_startup_overrides.json` | `WebUIServer._set_sandbox_auto_start` | persists the dashboard Auto-start toggle without rewriting JSON5 |
| `config.json5` | nobody at runtime | the settings page is a placeholder |

### 3.4 Existing settings UI

`src/webui/frontend/index.html:464` contains only:

```html
<div id="page-settings" class="page">
  <div class="card">
    <div class="card-title">Settings</div>
    <div class="muted">Settings editor will be added in next iteration.</div>
  </div>
</div>
```

`app.js:21 showPage(name)` switches pages; there is no settings logic and no
settings stylesheet.

### 3.5 Configuration shape

`config.json5` is 22 KB and contains:

- fixed sections: `version`, `title`, `instance`, `root`, `logging`, `webui`,
  `mcp`, `sandboxes`, `envids`, `users`, `ssh`;
- dynamic maps: `sandboxes.items.<id>`, `envids.<token>`, `logging.levels.<tag>`,
  `runtime_contract.environment.<VAR>`;
- heterogeneous sandbox objects (9 in the live file, 3 in the example);
- quoted and unquoted `${VAR}` / `${VAR:-default}` in strings, integers and
  integer arrays;
- comments, trailing commas and unquoted keys.

The descriptor is therefore a **completeness contract**: any undeclared key
makes the whole document invalid (`undeclared_field`), and any placeholder on a
node without `env.allowed` makes it invalid (`env_not_allowed`).

### 3.6 Existing per-image templates

Every image directory except `default_sandbox_1` and `mcp_toolbox_1` already has
a mergeable template:

| File | Declared sandbox ids | State |
|---|---|---|
| `images/langvm/config.sndbx.json5` | `langvm` | valid template |
| `images/aibusiness/config.sndbx.json5` | `langvm` | **stale copy of langvm; does not describe `aibusiness`** |
| `images/clawvm/config.sndbx.json5` | `clawvm` | valid template |
| `images/denis_obsidian_assistant/config.sndbx.json5` | `denis-obsidian-assistant` | valid template |
| `images/open-webui/config.sndbx.json5` | `open-webui` | valid template |
| `images/paperclip/config.sndbx.json5` | `paperclip` | valid template |
| `images/default_sandbox_1/` | `sandbox-1`, `default-sandbox-1` | **no template** |
| `images/mcp_toolbox_1/` | `mcp-toolbox-1` | **no template** |

Two traps:

1. `images/aibusiness/config.sndbx.json5` declares `langvm` and points at
   `LANGVM_*` variables. It must not be renamed blindly; the `aibusiness`
   overlay is generated from the current root entry.
2. `images/<id>/config.json5` (where present, for example `langvm` and
   `aibusiness`) is the **in-VM application config**, not the sndbx sandbox
   overlay. The new overlay file is host-only and is not mounted into the VM.

The image directories other than `default_sandbox_1` and `mcp_toolbox_1` are
ignored by the root `.gitignore`:

```text
images/*/
!images/default_sandbox_1/
!images/default_sandbox_1/**
!images/mcp_toolbox_1/
!images/mcp_toolbox_1/**
```

If overlays for those images must be versioned, a narrow unignore rule is
required; see 11.1. This matters because renaming `config.sndbx.json5` does not
make it tracked by itself.

---

## 4. Target architecture

```text
  config.json5 (document "main")
  registry + global settings + override values + run_at_startup
                 │
                 ▼
  ┌──────────────────────────────────────────────────────────┐
  │ aiset.service.Service                                    │
  │  register("main", ...)                                   │
  │  register("sandbox:<id>", overlay_path, ...)             │
  │  register("sandbox-group:<dir>", overlay_path, ...)      │
  │  reread / tree / apply_operations / rollback             │
  └───────────────┬──────────────────────────────────────────┘
                  │
  ┌───────────────┴──────────────────────────────────────────┐
  │ src/settings/  (host adapter, new package)               │
  │  aiset_path.py   import-path bootstrap                   │
  │  runtime.py      service + resolver + overlay merge      │
  │  principals.py   session -> Principal                    │
  │  facade.py       load_config compatibility               │
  └───────────────┬──────────────────────────────────────────┘
                  │
        ┌─────────┴──────────┐
        ▼                    ▼
  src/app.py         WebUIServer / FastAPI
  merged config      /api/settings + document selector
```

### 4.1 New module layout

| File | Responsibility |
|---|---|
| `src/settings/__init__.py` | exports `get_runtime`, `SettingsRuntime`, `load_config` |
| `src/settings/aiset_path.py` | puts `<root>/aiset/src` on `sys.path`, detects the namespace-package trap, raises a clear error if the package is missing |
| `src/settings/runtime.py` | builds `Resolver`, `Settings`, `Service`; registers `main` and every overlay; validates startup; merges overlays; exposes `service`, `config`, `env`, `documents`, `revision`; registers callbacks |
| `src/settings/principals.py` | maps a sndbx session (or an anonymous request) to `aiset.service.Principal` |
| `src/settings/facade.py` | `load_config(root_dir)` compatibility wrapper returning `{"env", "config", "settings"}` |
| `src/settings/overlays.py` | overlay discovery, path/id mapping, merge policy, overlay-specific validation |

No transport code of our own: the Web UI mounts `aiset.asgi.create_asgi_app`.
No error translation: the library's `{"ok": false, "error": ...}` envelope is
already what the generated editor understands.

### 4.2 The overlay model

| Property | Rule |
|---|---|
| File location | `images/<image-dir>/sndbx.overlay.json5` |
| Scope | `sandboxes.items.<id>` (VM defaults) + optional `envids` and `run_at_startup` |
| Precedence | **root wins every conflict**, like `merge(root, worker_local, prefer="base")` in aidir; overlay-only keys are added |
| Merge | objects deep-merge; any non-object conflict keeps the root value (this includes arrays) |
| Reader | the host; the VM receives the merged runtime config |
| Validation | the same full descriptor plus host-level overlay rules (7.4) |
| Registration | one aiset document per overlay file |
| Editing | every overlay is editable from the Settings page |

The overlay files:

| File | Sandbox ids | Source |
|---|---|---|
| `images/default_sandbox_1/sndbx.overlay.json5` | `sandbox-1`, `default-sandbox-1` | generated from current root |
| `images/mcp_toolbox_1/sndbx.overlay.json5` | `mcp-toolbox-1` | generated from current root |
| `images/langvm/sndbx.overlay.json5` | `langvm` | rename + reconcile existing template |
| `images/aibusiness/sndbx.overlay.json5` | `aibusiness` | generated from current root; the stale template is not used |
| `images/clawvm/sndbx.overlay.json5` | `clawvm` | rename + reconcile |
| `images/denis_obsidian_assistant/sndbx.overlay.json5` | `denis-obsidian-assistant` | rename + reconcile |
| `images/open-webui/sndbx.overlay.json5` | `open-webui` | rename + reconcile |
| `images/paperclip/sndbx.overlay.json5` | `paperclip` | rename + reconcile |

Nine sandbox ids, eight files. The root `config.json5` keeps the `sandboxes.items`
keys as the registry and as the **override layer**: any key present there wins
over the overlay value. A VM can exist before its overlay is populated.

The practical consequence: if the operator wants the root to override a VM
setting, that key stays in the root; if the VM should own the setting, the key is
removed from the root and lives only in the overlay. An empty root array or a
root scalar (including `false`, `0` or `""`) is still a present key and wins; to
let the overlay supply the value, remove the key from the root rather than set
it to an empty placeholder.

### 4.3 Document ids and labels

| Document id | File | UI label |
|---|---|---|
| `main` | `config.json5` | Root configuration (overrides) |
| `sandbox:<id>` | one-id overlay | `Sandbox: <id>` |
| `sandbox-group:<dir>` | multi-id overlay | `Image: <dir> (id1, id2)` |

`default_sandbox_1` is the only group today: `sandbox-group:default_sandbox_1`
shows both `sandbox-1` and `default-sandbox-1` in one tree. This is the
consequence of the confirmed "one file per image directory" decision.

### 4.4 Startup flow

1. `SNDBXApp.start()` calls `load_config(root_dir)`.
2. `load_config` obtains the cached `SettingsRuntime` for `root_dir`.
3. `SettingsRuntime.start()`:
   - ensures the aiset package is importable;
   - reads `.env` with the existing `load_env_file` (host-owned parser);
   - injects `SNDBX_ROOT` into the mapping when absent, exactly as today;
   - builds `Resolver(overrides={}, dotenv=env_vars, environ=os.environ)`;
   - registers `main` with `config.descriptor.json5` + `config.types.json5`;
   - discovers every `images/*/sndbx.overlay.json5`, validates its ids and
     registers it as `sandbox:<id>` or `sandbox-group:<dir>`;
   - calls `service.reread(...)` for every document (no principal required);
   - refuses to start if any document is absent, invalid on disk, or loaded from
     LKG rather than from its file (7.3);
   - merges overlays under the root into a **runtime config** (7.4), root wins;
   - stores the merged dict and each document's revision.
4. `load_config` returns
   `{"env": env_vars, "config": runtime_config, "settings": runtime}`.
5. `SNDBXApp` passes `settings=runtime` to `WebUIServer`.

The runtime is cached per absolute root path, so a second `load_config()` in the
same process reuses one `Service` and one file-lock owner.

### 4.5 Request flow

- Reads and writes go through the mounted ASGI app; the browser names a document
  id, never a filesystem path.
- Authentication is the existing `sndbx_session` cookie and `SessionStore`
  (`src/webui_server.py:56-119`).
- `principal_from_scope` is async, reads the cookie from the ASGI scope, loads
  the session and returns a `Principal`.
- The library runs dispatch in a worker thread, so a large tree snapshot cannot
  stall the Web UI event loop.

---

## 5. Mapping the aiset integration checklist to sndbx

| `AGENT_INTEGRATION.md` section 0 item | sndbx action |
|---|---|
| 1. Pin the library | submodule at `11d8838e`; record the revision in this plan and in `README.md` |
| 2. Register every document at startup | `main` + one document per overlay file; descriptor `config.descriptor.json5`; no client path |
| 3. Authenticate every request | `principal_from_scope` backed by `SessionStore`; never return `Principal.demo()` |
| 4. Mount one transport at `{base}` | `create_asgi_app(service, base="")` mounted at `/api/settings` in the FastAPI app |
| 5. Serve shipped assets | the same mount serves `/api/settings/assets/*`; no copies in `src/webui/frontend/` |
| 6. Mount the editor and theme by variables | `aiset.mountTree("#settings-tree", {...})`; host sheet overrides `--aiset-*`; load host sheet last |
| 7. Implement the apply contract | logging and run_at_startup live; everything else `restart_required`; lifecycle button calls `/api/service/restart` |
| 8. Write with revisions | the editor does this; the host Auto-start write must call `reread` first and send both revisions |
| 9. Test the integration | descriptor gate, overlay merge parity, permission denial, one end-to-end write, asset path, class coverage |
| 10. Know the failure map | assets 404/HTML vs script order vs missing stylesheet; see 9.4 |

---

## 6. Descriptor plan

### 6.1 Files

| File | Tracked | Purpose |
|---|---|---|
| `config.descriptor.json5` | yes | schema for the root and every overlay |
| `config.types.json5` | yes | typedefs reused by the root descriptor |

The root descriptor is **not** called `descriptor.json5`: that name is reserved
for per-component descriptors if sndbx later gains worker-local documents. The
`config.descriptor.json5` convention follows the aidir integration.

`Service.register(document_id, path, descriptor_text, typedef_sources=(types_text,), resolver=...)`
for every document. The descriptor text is the same for the root and every
overlay; only the path and document id differ.

### 6.2 Root map

| Path | Node | `apply` | `secret` | Notes |
|---|---|---|---|---|
| `version` | string, `const: "1.0"`, `read_only` | restart | - | identity; optional in an overlay |
| `title` | string | restart | - | |
| `instance` | string, `env.allowed` | restart | - | `${SNDBX_INSTANCE:-sndbx-dev}` |
| `root` | string, `env.allowed`, `read_only` | restart | - | derived from `${SNDBX_ROOT}`; editing it breaks paths |
| `logging` | object | **live** | - | subtree apply |
| `logging.level` | union string/int, `env.allowed` | live | - | string enum or integer 0-7 |
| `logging.timezone` | string, `env.allowed` | live | - | IANA name / `local` / `UTC` |
| `logging.levels` | object, `additional_children` union string/int | live | - | dynamic tags |
| `logging.wipe_period` | int, `env.allowed`, `env.convert: "int"` | live | - | |
| `logging.log_mcp_requests` | bool | live | - | |
| `webui` | object | restart | - | bind/port/auth |
| `webui.auth.users[]` | array of object | restart | - | |
| `webui.auth.users[].password` | string, `env.allowed` | restart | **yes** | `${ROOT_PASSWORD:-changeme}` |
| `webui.auth.users[].permissions[]` | array of string, enum incl. `all` | restart | - | |
| `mcp` | object | restart | - | |
| `mcp.auth.tokens[]` | array of string, `env.allowed` | restart | **yes** | static API tokens |
| `mcp.items[]` | array of `any` | restart | - | reserved/empty today |
| `sandboxes.items.<id>` | dynamic object of typedef `sandbox` | restart | - | key pattern; all known fields declared |
| `envids` | object, dynamic `envid` entries | restart | **yes** | **the keys themselves are session tokens** |
| `envids.<token>.sandbox` | string | restart | inherited | |
| `envids.<token>.description` | string | restart | inherited | |
| `users.items[]` | array of object | restart | - | |
| `users.items[].password` | string, `env.allowed` | restart | **yes** | |
| `users.items[].token` | string, `env.allowed` | restart | **yes** | |
| `users.items[].envids[]` | array of string | restart | - | |
| `ssh` | object | restart | - | |
| `ssh.port_range[]` | array of int, item `env.allowed`, `env.convert: "int"` | restart | - | unquoted `${SSH_PORT_MIN:-30200}` |
| `ssh.session_ttl` | int | restart | - | |

`apply` is stated on `logging` as `live` and on the root as `restart`; all
children inherit. `run_at_startup` overrides to `live` (6.7).

### 6.3 `sandbox` typedef

One permissive object typedef covers all nine live sandboxes and all three
example sandboxes. Every known field is declared; a new field is a descriptor
change, enforced by the completeness test. Do **not** add a catch-all
`type: "any"` to the sandbox object: a future field containing `${VAR}` would
then silently stay literal.

| Field | Type / constraint | `env` |
|---|---|---|
| `title` | string | - |
| `image` | string | `allowed`, `${VM_IMAGE:-...}` |
| `local_image_id` | string | - |
| `memory`, `disk_max`, `network_traffic_max`, `shm_size` | string | `allowed` |
| `cpus` | int | `allowed`, `convert: "int"` |
| `persistent`, `run_at_startup`, `read_only_rootfs`, `configure_apt_mirror`, `ssh_setup_via_image_hook`, `virtiofs_profile` | bool / string | see 6.7 for `run_at_startup` |
| `ssh_user` | string | - |
| `ssh_keys` | array of string, item `env.allowed` | `${MY_SSH_PUBLIC_KEY}` |
| `tmpfs` | array of string | - |
| `shared_directories[]` | typedef `shared_dir` | - |
| `managed_volumes[]` | typedef `managed_volume` | - |
| `port_bindings[]` | typedef `port_binding` | - |
| `mcp_bindings[]` | typedef `mcp_binding` | - |
| `runtime_contract` | typedef `runtime_contract` | - |
| `backup` | object: `enabled` bool, `interval_hours` int, `full_every` int | - |

Nested typedefs:

| Typedef | Fields | Env-bearing leaves |
|---|---|---|
| `shared_dir` | `host_path`, `guest_path`, `permission` enum `ro/rw`, `mount_type` enum `dir/file`, `source_type` enum `directory/file`, `required` bool, `create_if_missing` bool, `host_mode` string | `host_path` (`${SNDBX_ROOT}`, `${VM_SHARED_DIR:-...}`) |
| `managed_volume` | `name` string, `guest_path` string | - |
| `port_binding` | `bind_host` string, `vm_port` int, `publish_port` int | `bind_host` (`allowed`), both ports (`allowed`, `convert: "int"`; quoted placeholders are converted too) |
| `mcp_binding` | `backend_id`, `transport` enum `tcp-jsonl`, `tools[]` string, `auth_mode` enum `token_passthrough`, `vm_port` int, `startup_timeout_sec` int, `read_timeout_sec` int | - |
| `readiness` | `type` enum `http/tcp`, `url` string, `attempts` int, `interval_seconds` int | `url` (`allowed`; it embeds `${...PORT}`) |
| `runtime_contract` | `version` int, `capability_hook` bool, `environment` dynamic map of string, `readiness[]` typedef `readiness` | `environment.*` (`allowed`, secret) |
| `envid` | `sandbox` string, `description` string | - |

`mcp_bindings` fields are all present in the live config except `transport`,
`auth_mode`, timeouts; declare them optional so both shipped files validate.

### 6.4 Overlay document shape

An overlay is a **partial** document with the same descriptor:

```json5
// images/langvm/sndbx.overlay.json5
{
  sandboxes: {
    items: {
      "langvm": {
        title: "LangVM sandbox",
        image: "langvm:latest",
        memory: "${LANGVM_MEMORY:-2G}",
        cpus: ${LANGVM_CPUS:-2},
        // ...
      },
    },
  },
  // Optional. The root config wins on conflicts; new entries are added.
  envids: {
    "langvm-env-token": { sandbox: "langvm", description: "LangVM environment" },
  },
}
```

Host-level overlay rules (not expressible in the descriptor alone):

1. Top-level keys are limited to `version`, `sandboxes`, `envids`. Any other
   section (`logging`, `webui`, `mcp`, `users`, `ssh`, `title`, `instance`,
   `root`) is rejected at startup.
2. `sandboxes.items` is required and must be non-empty.
3. Every sandbox id in the overlay must already exist in the root registry
   (`main`). An overlay may not invent a new sandbox.
4. The ids in a file must belong to the image directory that contains it:
   `local_image_id` if present, else the image name without its tag, else the id
   with `-` normalized to `_`. `default_sandbox_1` is the only multi-id file.
5. **Every conflict is resolved in favour of the root**
   (`merge(root, overlay, prefer="base")`): scalars, arrays, `null` and type
   mismatches keep the root value; object members deep-merge; keys present only
   in the overlay are added. This applies to `envids` and `run_at_startup` too.
6. A root array (even an empty one) wins over an overlay array. To let the
   overlay supply the list, remove the key from the root; do not set it to `[]`.
7. The migration removes `run_at_startup` from overlays because the root owns
   it. If the root omits the key, an overlay value is used; if both define it,
   the root wins. The shipped inventory asserts the root defines it for every
   sandbox and no overlay does.

The host validator runs at startup and in the descriptor/overlay test. It does
not replace aiset validation; it adds only the host policy that the generic
grammar cannot express.

### 6.5 Placeholder policy

> Every node that may contain a `${VAR}` or `${VAR:-default}` — quoted or
> unquoted — must set `env.allowed: true`. Integer and boolean nodes also set
> `env.convert` to `int` / `bool`.

A quoted placeholder is **not** exempt: the resolver still checks
`node.env_allowed` before substituting. The live files contain placeholders in
at least these locations:

- root: `instance`, `root`;
- `logging.level`, `logging.timezone`, `logging.wipe_period`;
- `webui.port`, `webui.bind`, `webui.auth.users[].login`, `.password`;
- `mcp.port`, `mcp.bind`, `mcp.auth.tokens[]`;
- `sandboxes.items.*`: `image`, `memory`, `cpus`, `disk_max`,
  `network_traffic_max`, `shm_size`, `ssh_keys[]`,
  `shared_directories[].host_path`, `port_bindings[].bind_host`,
  `port_bindings[].vm_port`, `port_bindings[].publish_port`,
  `runtime_contract.environment.*`, `runtime_contract.readiness[].url`;
- `ssh.port_range[]`;
- `users.items[].login`, `.password`, `.token` when the user uses placeholders.

`config.json5.example` uses `${MY_SSH_PUBLIC_KEY}` with an empty `.env` value.
The resolver's POSIX rule returns the empty string, which is the current
behaviour (`load_env_file` returns the key with an empty value).

### 6.6 Secret policy

| Node | `secret` | Reason |
|---|---|---|
| `webui.auth.users[].password` | yes | login credential |
| `mcp.auth.tokens[]` | yes | MCP bearer tokens |
| `users.items[].password`, `users.items[].token` | yes | MCP/SSH users |
| `envids` subtree (root and overlay) | yes | **the dynamic keys are the session tokens** |
| `runtime_contract.environment` | yes | environment maps tend to accumulate credentials |
| `ssh_keys` | no | public keys |
| `local_image_id`, image names, paths | no | not credentials |

A session without `settings_read_secret` receives the whole secret subtree as
denied, not redacted. The only Web UI permission today is `["all"]`; it maps to
the full permission set, so administrators keep the current visibility.

### 6.7 Apply policy

| Node | `apply` | Host action |
|---|---|---|
| `logging.*` | `live` | `on_application` calls `configure_logging(new_effective, root_dir)` |
| `run_at_startup` | `live` | `on_after_save` updates the in-memory runtime config and `sandbox_manager.sandbox_configs`; it only affects the next start |
| everything else | `restart` | the editor shows `restart_required`; the user presses the lifecycle button `POST /api/service/restart` |

No `reload_required` node exists in v1: sndbx has no SIGHUP-style reload. If a
future subsystem gains one, set `apply: "reload"` on that node and add a host
lifecycle action.

### 6.8 Descriptor completeness gate

`tests/test_config_descriptor.py` must fail the build unless:

1. `config.descriptor.json5` passes `validate_descriptor(..., mode="strict")`;
2. `Service.register` + `Service.reread` load `config.json5`,
   `config.json5.example` and every `images/*/sndbx.overlay.json5` with
   `invalid_on_disk == False` and no error issues;
3. every leaf of every document is reachable through `descriptor.node_at`;
4. every node holding a placeholder has `env.allowed`;
5. every value declared secret in 6.6 is still secret;
6. the frozen path inventory matches: adding a key to any document without
   updating the descriptor fails the test;
7. every configured sandbox id is covered by exactly one overlay file;
8. every overlay passes the host-level rules of 6.4;
9. no overlay contains `run_at_startup` (after migration; the root owns it).

The inventory is built by an **independent** reference reader (regex
substitution + `json5.loads`), not by aiset, so a descriptor bug cannot hide
itself.

### 6.9 Descriptor skeleton

This is the shape, not the final file. The real file is reviewed line by line
against Appendix A and validated with `--strict` before phase 1 is closed.

```json5
{
  descriptor_version: "1.0",
  project: "sndbx",
  config_version: "1.0",
  root: {
    type: "object",
    description: "sndbx root configuration and per-image overlay",
    apply: "restart",
    children: {
      version: { type: "string", const: "1.0", read_only: true },
      title:   { type: "string" },
      instance:{ type: "string", env: { allowed: true } },
      root:    { type: "string", env: { allowed: true }, read_only: true },
      logging: {
        type: "object",
        apply: "live",
        children: {
          level: { type: "union", env: { allowed: true }, children_options: [
            { type: "string", enum: ["trace","debug","info","warning","warn","error","critical","fatal"] },
            { type: "int", minimum: 0, maximum: 7 },
          ]},
          timezone: { type: "string", env: { allowed: true } },
          levels: {
            type: "object",
            additional_children: { type: "union", children_options: [
              { type: "string", enum: ["trace","debug","info","warning","warn","error","critical","fatal"] },
              { type: "int", minimum: 0, maximum: 7 },
            ]},
          },
          wipe_period: { type: "int", env: { allowed: true, convert: "int" } },
          log_mcp_requests: { type: "bool" },
        },
      },
      // webui, mcp, users, ssh: see Appendix A
      sandboxes: {
        type: "object",
        children: {
          items: {
            type: "object",
            additional_children: { type: "sandbox" },
            key_pattern: "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          },
        },
      },
      envids: {
        type: "object",
        secret: true,
        additional_children: { type: "envid" },
      },
    },
  },
}
```

`config.types.json5` declares `sandbox`, `shared_dir`, `managed_volume`,
`port_binding`, `mcp_binding`, `readiness`, `runtime_contract`, `envid` and
`log_level`.

---

## 7. Runtime and compatibility facade

### 7.1 Importing the submodule safely

`src/settings/aiset_path.py`:

1. try `import aiset.resolver`;
2. if it fails, remove `aiset` and every `aiset.*` entry from `sys.modules`
   (the namespace-package trap described in `AGENT_INTEGRATION.md` 2a);
3. insert `<root>/aiset/src` at the front of `sys.path`;
4. import `aiset.resolver`, `aiset.service`, `aiset.store`, `aiset.asgi`,
   `aiset.merge` and fail with a message naming the missing path and the two
   install commands if any is absent.

The root directory is derived from `Path(__file__).resolve().parents[2]`, not
from the process working directory.

### 7.2 Resolver precedence

Current order in `src/config.py` and the target order are identical:

| Priority | Current | aiset |
|---|---|---|
| 1 | `os.environ` | host `overrides` (empty for sndbx) |
| 2 | — | `os.environ` |
| 3 | `.env` mapping | `dotenv` mapping (the same `load_env_file` result) |
| 4 | inline `:-` default | inline `:-` default |
| 5 | raise `ConfigError` | empty string / descriptor default |

To keep the order exact, pass `overrides={}`, `dotenv=env_vars`,
`environ=os.environ`. `SNDBX_ROOT` is injected into `env_vars` only when absent,
exactly as `src/config.py:131-133` does today.

The one deliberate difference is the last row: a missing variable with no inline
default becomes `""` instead of raising. sndbx keeps fail-fast behaviour by an
explicit startup preflight for required values:

- `SNDBX_ROOT` (injected, so normally present);
- `root` is non-empty;
- `webui.port`, `mcp.port`, `ssh.port_range` are within their declared ranges;
- `webui.auth.users` is non-empty.

If a required value is empty, `SettingsRuntime` raises `ConfigError` with the
path and the variable name.

### 7.3 Startup failure policy

`Service.reread` may return one of three outcomes:

| Outcome | `LoadResult` | sndbx action |
|---|---|---|
| valid file | `loaded == "file"`, `invalid_on_disk == False` | start |
| invalid file, valid `.back` exists | `loaded == "lkg"` | **refuse to start**; print `reason` and the issue list; an operator must fix the file or explicitly run an LKG recovery |
| invalid file, no valid backup | `invalid_on_disk == True`, `effective == {}` | **refuse to start** with the issue list |

Silently running on an old `.back` would hide a typo until the next write. The
error text must name the document (`main` or the overlay path), the descriptor,
and `aiset doctor` / `.back` as recovery options.

A missing `config.json5` keeps today's `ConfigError("Config file not found: ...")`
message, so `install.sh` and `TEST.md` instructions remain valid. A missing
overlay is allowed (the root values are used and a warning is logged), so a new
sandbox can be added before its overlay exists; the shipped inventory test still
requires every configured id to be covered.

### 7.4 Overlay discovery and merge

Discovery:

1. list `images/*/sndbx.overlay.json5`;
2. read each file's `sandboxes.items` keys;
3. apply the host rules of 6.4;
4. build the document id (`sandbox:<id>` or `sandbox-group:<dir>`) and the
   label;
5. require every root sandbox id to be covered by exactly one overlay in the
   test inventory (startup only warns for a missing overlay).

Merge:

```python
runtime = copy.deepcopy(main.effective)

for overlay in overlays:
    ov = overlay.effective
    for sandbox_id, overlay_sandbox in ov.get("sandboxes", {}).get("items", {}).items():
        root_sandbox = runtime["sandboxes"]["items"].get(sandbox_id, {})
        # root wins every conflict, exactly like aidir's worker merge
        runtime["sandboxes"]["items"][sandbox_id] = aiset.merge(
            root_sandbox, overlay_sandbox, prefer="base"
        )

    if "envids" in ov:
        runtime["envids"] = aiset.merge(
            runtime.get("envids", {}), ov["envids"], prefer="base"
        )
```

Why this matches the decisions:

- `prefer="base"` makes the root win every conflict, so a key present in both
  places uses the root value;
- `merge` deep-merges objects, so an overlay may add object members the root
  does not declare;
- a non-object conflict (scalar, array, `null`, type mismatch) keeps the root
  value; a root array wins over the overlay array as a whole;
- keys present only in the overlay are added;
- `envids` uses the same rule: root entries win, overlay-only entries are added;
- `run_at_startup` therefore needs no special case, because root presence wins;
- `copy.deepcopy` ensures the runtime config is independent of aiset's cached
  state and of the root dict that components may mutate.

The runtime config is what `DockerSandboxManager`, `MCPServer`, `ToolHandlers`,
`BackupManager` and `WebUIServer` receive. The root `config.json5` on disk is
the override document; overlays are separate base/default documents. A root key
always wins over the same overlay key. To let the overlay supply a value, the
key must be absent from the root; the Settings page explains this for both
documents (8.3).

### 7.5 Auto-start migration

`data/sandbox_startup_overrides.json` is retired:

1. On the first start of the new backend, if the file exists, log a warning and
   use its values only for the migration step (not silently forever).
2. `scripts/migrate_sandbox_overlays.py --import-startup-overrides` applies each
   override to the root document with `apply_operations` on
   `/sandboxes/items/<id>/run_at_startup`.
3. Rename the old file to `data/sandbox_startup_overrides.json.migrated` after a
   successful import.
4. Remove `apply_sandbox_startup_overrides` and the file read from `src/config.py`.

The dashboard `_set_sandbox_auto_start` (`src/webui_server.py:395-410`) changes
to:

1. `result = service.reread("main")` to obtain the current revision;
2. `service.apply_operations("main", [{"op": "set",
   "path": "/sandboxes/items/<id>/run_at_startup", "value_text": "true"|"false"}],
   expected_revision=result.revision,
   expected_descriptor_revision=result.descriptor_revision,
   principal=editor_principal)`;
3. on `409`, re-read and retry once; never overwrite blindly;
4. update `runtime.config` and `sandbox_manager.sandbox_configs` in place (the
   `on_after_save` callback does this as well, so both paths converge);
5. report the library's error message to the UI on failure.

### 7.6 Compatibility facade

`src/config.py` keeps its public names but becomes an address:

| Current name | New implementation |
|---|---|
| `load_config(root_dir)` | `get_runtime(root_dir)`; returns `{"env", "config", "settings"}` |
| `load_env_file(path)` | unchanged (host-owned `.env` parser) |
| `ConfigError` | unchanged exception class |
| `apply_sandbox_startup_overrides` | removed after the migration (7.5) |
| `expand_placeholders`, `load_config_json5` | removed at cutover (phase 5) |

`get_runtime` returns the cached object, so `load_config` is idempotent.
`runtime.config` is the **same dict object** for the whole process, satisfying
the by-reference injection in `src/app.py:83-200`. It is never replaced by a
second load; a re-read updates it in place if that is ever needed.

### 7.7 Callbacks

Register once after `register`:

- `on_application(previous_effective, new_effective, changed_paths)`:
  - if a changed path is under `/logging`, call
    `configure_logging(new_effective, root_dir)`;
  - if a changed path is under `/sandboxes/items/*/run_at_startup`, update the
    runtime config and `sandbox_manager.sandbox_configs`;
  - other live paths are ignored.
- `on_after_save(previous_stored, new_stored, event)`:
  - for `main` and a changed `run_at_startup`, keep the in-memory value in sync
    even if the application callback is skipped because the batch also contains
    a restart path;
  - never writes the file; the service already committed.

`on_application` runs only for `live`/`reload` changes, so a batch that mixes
logging with a restart field does not reconfigure logging; the restart will
reload it anyway.

### 7.8 In-memory config after a write

The running process keeps the configuration it started with until restart, with
two exceptions:

- `logging.*` is reconfigured live;
- `run_at_startup` is updated in memory because it only affects the next start.

Everything else is deliberately restart-gated: `DockerSandboxManager`, the SSH
pool and the auth user table are built from the startup dict; partially updating
them after an edit would create a half-applied runtime. The settings editor reads
the files through aiset, so it always shows the new state. The lifecycle button
is the explicit boundary between "saved" and "active".

---

## 8. Web UI integration

### 8.1 Mounting

Inside `WebUIServer._create_app()` (`src/webui_server.py:885`), **before** the
final `app.mount("/", StaticFiles(...))`:

```python
from aiset.asgi import create_asgi_app

app.mount(
    "/api/settings",
    create_asgi_app(
        self.settings.service,
        base="/api/settings",
        principal_from_scope=self._settings_principal,
    ),
)
```

Why `base="/api/settings"`: in the installed Starlette 1.0 a `Mount` keeps the
full request path and only sets `root_path`; the aiset ASGI adapter routes on the
full path and subtracts `base`. The public URLs are `/api/settings/tree`,
`/api/settings/assets/aiset.js`, etc. On a framework version that strips the
prefix before the sub-application, pass `base=""` instead. The integration test
`tests/test_settings_http.py` exercises the actual installed stack.

Mount order matters. The settings mount must be registered before the catch-all
`/` static mount, otherwise `/api/settings/...` is served the SPA and the editor
fails with HTML-as-JavaScript.

### 8.2 Principal mapping

`src/settings/principals.py`:

| Session | Permissions |
|---|---|
| no valid `sndbx_session` | empty `frozenset()` -> library returns 401/403 |
| authenticated, `permissions` contains `"all"` | all eight `settings_*` permissions, including `settings_read_secret` and `settings_edit_secret` |
| authenticated, no `"all"` | viewer: `settings_read_source`, `settings_read_effective` |

The resolver is async and reads the cookie from the ASGI scope
(`starlette.requests.Request(scope).cookies.get("sndbx_session")`), then calls
`SessionStore.get(token)`. It must never return `Principal.demo()`.

The cookie is already `HttpOnly`, `SameSite=strict`, and `Secure` on HTTPS
(`src/webui_server.py:902-910`), so the host keeps its existing CSRF posture.
aiset adds no auth, TLS, CORS or CSRF logic, by design.

### 8.3 Document selector and page

Replace the placeholder body of `#page-settings` with:

```html
<div class="card">
  <div class="card-title">Settings</div>
  <div class="settings-toolbar">
    <label for="settings-document" class="muted">Document</label>
    <select id="settings-document"></select>
    <span id="settings-document-note" class="muted"></span>
  </div>
  <div id="settings-tree"></div>
</div>
```

Add a small host endpoint:

```text
GET /api/settings/documents
-> {"ok": true, "documents": [
     {"id": "main", "label": "Root configuration (overrides)", "sandboxes": []},
     {"id": "sandbox:langvm", "label": "Sandbox: langvm", "sandboxes": ["langvm"]},
     {"id": "sandbox-group:default_sandbox_1",
      "label": "Image: default_sandbox_1 (sandbox-1, default-sandbox-1)",
      "sandboxes": ["sandbox-1", "default-sandbox-1"]},
     ...
   ]}
```

It requires a valid session and returns only UI metadata from
`SettingsRuntime.document_catalog()`. It is not a settings read; aiset's
`/documents` remains the authority for registered ids.

`app.js`:

- `let settingsTree = null;`
- `loadSettingsDocuments()` fetches the catalog and fills the selector;
- `mountSettings(documentId)` calls `aiset.mountTree("#settings-tree", {
  document: documentId, apiBase: "/api/settings", view: "source",
  lifecycle: [{id: "restart", label: "Restart sndbx",
  url: "/api/service/restart", confirm: "Restart the service now?",
  reloadAfter: false}] })`;
- `destroy()` the previous instance before mounting a new document;
- `showPage("settings")` mounts the currently selected document once;
- the root document shows a note: "Keys here override the same key in
  `images/<id>/sndbx.overlay.json5`; remove a key from the root to let the
  overlay supply it";
- an overlay document shows a note: "VM defaults. The root config wins for every
  key it defines; arrays in the root replace this list entirely".

`reloadAfter: false` avoids racing the editor's reload against the service
restart. After a restart the user reloads the page; the file is already saved.

### 8.4 Assets and styles

In `index.html` `<head>`, in this order:

```html
<link rel="stylesheet" href="/api/settings/assets/aiset.css">
<link rel="stylesheet" href="/api/settings/assets/aiset-host-theme.css">
<link rel="stylesheet" href="/settings.css">
```

and at the end of `<body>`, in dependency order:

```html
<script src="/api/settings/assets/aiset.js" defer></script>
<script src="/api/settings/assets/aiset-tree.js" defer></script>
<script src="/app.js"></script>
```

Rules:

- No copy of `aiset.css`, `aiset.js` or `aiset-tree.js` enters the host tree.
- `settings.css` is the host sheet and is loaded **last**.
- It must override `--aiset-*` variables rather than fight selectors.
- The existing global reset `* { margin: 0; padding: 0 }`
  (`index.html:19`) reaches the editor. `settings.css` must restate the padding
  of `input`, `button` and `select` inside `.aiset-tree-editor` and
  `.aiset-form`, or the reset must be scoped away from the editor.
- Set `color-scheme: dark` and map the sndbx palette (`#0f1117`, `#1a1d27`,
  `#2a2d3a`, `#5b8af7`, `#d0d4e8`, `#6b7090`) to `--aiset-*` variables.
- `aiset-host-theme.css` is a starting point, not a contract; adopt it and
  override from `settings.css`.
- Add a class-coverage test (section 9) against `aiset.classes()`.

### 8.5 Lifecycle and restart

`POST /api/service/restart` already exists (`src/webui_server.py:1031`) and
requires a session. It ignores the request body, so the editor's default
`{document}` body is harmless. The button is the only host chrome needed for
the `restart_required` contract.

### 8.6 Reverse-proxy and asset checks

sndbx is normally used directly on `WEBUI_HOST:WEBUI_PORT`, but the acceptance
protocol must still check:

```sh
curl -sI "http://HOST:PORT/api/settings/assets/aiset-tree.js"   # 200, JavaScript
curl -sI "http://HOST:PORT/api/settings/assets/missing.js"      # 404, not HTML
curl -s  "http://HOST:PORT/api/settings/health"                 # {"ok": true, ...}
```

An SPA fallback that answers a missing `.js` with `index.html` and 200 is the
most expensive failure to debug; check it explicitly.

---

## 9. Testing plan

### 9.1 Automated tests (new)

| File | Covers |
|---|---|
| `tests/test_config_descriptor.py` | strict descriptor; root, example and every overlay load; path inventory; `env.allowed` coverage; secret flags; overlay host rules; id coverage |
| `tests/test_settings_runtime.py` | path bootstrap (including namespace trap); resolver precedence; `SNDBX_ROOT` injection; missing config error; invalid root/overlay refusal; LKG refusal; `load_config` idempotence; runtime dict shape |
| `tests/test_settings_overlays.py` | discovery, path/id mapping, merge semantics (`prefer="base"`: root wins every conflict, overlay-only keys added, root arrays win), root-wins `envids`/`run_at_startup`, group document for `default_sandbox_1` |
| `tests/test_settings_parity.py` | for the root plus every overlay, compare the merged runtime config with the independent reference reader (phase 2/3 gate) |
| `tests/test_settings_http.py` | mount semantics: `base=""`; `/health`, `/documents`, `/tree`, `/value`; `428` without revisions; `409` on stale; one end-to-end write preserving comments, quoting and the final newline; secret subtree denial |
| `tests/test_settings_styles.py` | `aiset.classes()` coverage by `settings.css` + `aiset.css`; host sheet linked after `aiset.css`; no unscoped element selectors in the host sheet |

Use a temporary root for every test: copy `config.json5`/`.example`, the
descriptor and a synthetic overlay set; never touch the real project root.

The existing suite stays green:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

### 9.2 Descriptor and library commands

```sh
.venv/bin/python -c "import sys; sys.path.insert(0,'aiset/src'); from aiset.cli import main; raise SystemExit(main(['validate-descriptor','config.descriptor.json5','--strict']))"
.venv/bin/python -c "import sys; sys.path.insert(0,'aiset/src'); import aiset; print(aiset.__version__)"
node --check aiset/src/aiset/assets/aiset-tree.js
```

Do not add aiset's own test suite to the host suite; the submodule owns it.

### 9.3 Parity baseline

The baseline is the **current runtime configuration**: legacy
`load_config()` + `apply_sandbox_startup_overrides`. The new runtime is the root
effective config merged over every overlay effective config with
`prefer="base"` (root wins). Compare:

- every leaf of the global sections (`logging`, `webui`, `mcp`, `envids`,
  `users`, `ssh`);
- every leaf of every sandbox object;
- the set of sandbox ids;
- the resolved values of all placeholders.

Zero differences is the gate for enabling the new backend. After the root is
trimmed in a wave, the same test must remain at zero differences.

### 9.4 Manual acceptance (add to `TEST.md`)

1. Start sndbx on a free port with a temporary root + overlays.
2. Open Settings; the selector lists `main` and every overlay.
3. Mount each document; the tree renders with the dark sndbx theme.
4. Change `logging.level` in `main`; apply; expect `applied` (live); confirm new
   log lines obey the level without restart.
5. After the root trim, change `sandboxes.items.<id>.memory` in an overlay;
   apply; expect `restart_required`; press Restart; after restart the dashboard
   shows the new value.
6. Put the same `memory` key in the root override; apply; restart; confirm the
   root value wins over the overlay.
7. Stage an invalid value; expect `422` with `issues[].path`.
8. Change an overlay externally, then apply a stale edit; expect `409`.
9. Change Auto-start in the dashboard; confirm it writes `main`, not the overlay,
   and survives a restart.
10. Log in as a user without `all`; expect a read-only tree and denied secrets.
11. Request a missing asset; expect 404, not the SPA.
12. Restart the service and confirm every editor reflects the saved files.

---

## 10. Implementation phases

Every phase is separately revertable and ends with the existing suite green.

### Phase 0 — pin and guards (no behaviour change)

- [x] Add `aiset` submodule at `11d8838e`.
- [ ] Record the revision in `README.md` (new "Settings library" subsection).
- [ ] `.gitignore`: `config.json5.lock`, `config.json5.back*`,
      `config.json5.conflicted.*.back`; add a narrow exception for
      `images/*/sndbx.overlay.json5` (11.1).
- [ ] Update image `.dockerignore` files that list `config.sndbx.json5` so the
      host-only overlay never enters an image build.
- [ ] Add `tests/test_config_descriptor.py` skeleton with the frozen path
      inventory.
- Exit: no runtime change; descriptor test may be skipped until phase 1.

### Phase 1 — descriptor only (the central review deliverable)

- [ ] Write `config.descriptor.json5` and `config.types.json5`.
- [ ] Cover `config.json5`, `config.json5.example` and the current
      `config.sndbx.json5` templates as partial documents.
- [ ] Apply the `env.allowed` / `env.convert` rules of 6.5 and the secret flags
      of 6.6.
- [ ] Gate: strict validate; register + reread every document with zero errors;
      path inventory complete.
- [ ] Review the descriptor against Appendix A line by line; record the review
      result in the phase notes.
- [ ] sndbx still uses the old loader.
- Exit: descriptor gate green; `TEST.md` smoke check unchanged.

### Phase 2 — overlays and migration tool (still legacy runtime)

- [ ] Add `scripts/migrate_sandbox_overlays.py` with `--dry-run`,
      `--write-overlays`, `--trim-root`, `--import-startup-overrides`.
- [ ] Reconcile each existing `config.sndbx.json5` with the current root entry;
      root values are authoritative. Generate `aibusiness` from the root, not
      from the stale template.
- [ ] Create `sndbx.overlay.json5` for `default_sandbox_1` (two ids) and
      `mcp_toolbox_1` from the current root entries.
- [ ] Rename the remaining templates to `sndbx.overlay.json5` and remove stale
      `config.sndbx.json5` names.
- [ ] Update every document that references `config.sndbx.json5` (11.2).
- [ ] Validate every overlay with the descriptor; run the overlay host rules.
- [ ] Do **not** trim the root yet: overlays currently repeat the root values, so
      the legacy runtime is unchanged.
- Exit: all overlays exist and validate; legacy runtime still starts; docs are
  consistent.

### Phase 3 — runtime behind a flag

- [ ] Add `src/settings/` (`aiset_path`, `runtime`, `principals`, `facade`,
      `overlays`).
- [ ] Add `SNDBX_SETTINGS_BACKEND=legacy|aiset` (default `legacy`).
- [ ] `src/config.py`: `load_config` dispatches to the selected backend; the
      legacy functions remain untouched.
- [ ] Register `main` + every overlay; implement the merge of 7.4 and the
      startup policy of 7.3.
- [ ] Add parity tests comparing the two backends over the root and all overlays.
- [ ] Pilot wave in a temporary copy: `langvm` only, then the full set.
- Exit: parity zero differences; existing suite green; legacy default.

### Phase 4 — root trim in waves

- [ ] For each VM wave, reduce the root entry to `{run_at_startup: <current>}`
      plus any **deliberate root overrides**. aiset refuses a whole-object
      `set` that drops commented members, so the one-time migration tool edits
      the source span of each sandbox object, validates the result and keeps a
      `config.json5.pre-overlay.bak` backup. The runtime never does this edit.
- [ ] A key left in the root is an override and wins over the overlay. Removing
      it from the root is how the VM takes ownership of that key.
- [ ] Waves: langvm; then aibusiness/clawvm/denis-obsidian-assistant/
      open-webui/paperclip; then default_sandbox_1 (two ids) and mcp_toolbox_1.
- [ ] Run the parity test after every wave; a non-zero diff blocks the wave.
- [ ] Keep `config.json5` backups (`.back` chain and a manual
      `config.json5.pre-overlay.bak`) before trimming.
- [ ] Migrate `data/sandbox_startup_overrides.json` into `main` and rename it.
- Exit: root is the registry + `run_at_startup` + deliberate overrides; every
      VM default is in an overlay; runtime parity still zero; legacy backend
      still selectable.

### Phase 5 — Web UI settings tab

- [ ] Pass `settings=runtime` from `SNDBXApp` to `WebUIServer`.
- [ ] Mount `create_asgi_app` at `/api/settings` before the static mount.
- [ ] Add `_settings_principal` async resolver backed by `SessionStore`.
- [ ] Add `GET /api/settings/documents` and the selector.
- [ ] Replace the `#page-settings` placeholder with the mount point and notes.
- [ ] Add `src/webui/frontend/settings.css`, link the aiset assets and scripts
      in dependency order.
- [ ] `app.js`: mount/destroy the tree; lifecycle entry `restart`.
- [ ] Change `_set_sandbox_auto_start` to the aiset write path (7.5).
- [ ] Add HTTP, overlay, style and class-coverage tests.
- [ ] Manual acceptance of 9.4 on a temporary instance.
- Exit: editor works end to end; `restart_required` is honoured; auto-start is
      written to `main`.

### Phase 6 — cutover and cleanup

- [ ] Default `SNDBX_SETTINGS_BACKEND=aiset`.
- [ ] Remove the legacy branch, `expand_placeholders`,
      `load_config_json5` and `apply_sandbox_startup_overrides`; keep
      `load_env_file` and `ConfigError`.
- [ ] Decide whether `install.sh` should run the descriptor gate after applying
      a template.
- [ ] Update `README.md`, `TEST.md`, `memory/context.md`, `memory/history.md`.
- [ ] Re-run the full acceptance protocol and record the aiset revision.
- Exit: one loader; one write path per document; suite green.

---

## 11. Repository hygiene and documentation sweep

### 11.1 `.gitignore`

Add the aiset sidecars and decide overlay tracking. Aiset writes
`config.json5.lock`, `config.json5.back*` and
`config.json5.conflicted.<stamp>.back` next to the file.

All image directories except `default_sandbox_1` and `mcp_toolbox_1` are
ignored by the root repository. During implementation it turned out that those
directories are separate Git repositories (they contain their own `.git`), so
unignoring them in the root would expose nested repositories and unrelated
local files. The implemented rule is therefore:

- `images/default_sandbox_1/sndbx.overlay.json5` and
  `images/mcp_toolbox_1/sndbx.overlay.json5` are tracked by the root repository;
- overlays in the other image directories are local state owned by those image
  repositories, like `config.json5` itself.

The root `.gitignore` keeps `images/*/` ignored except the two tracked demo
images. Overlays for the nested repositories are present in the working tree
but do not appear in the root repository status.

### 11.2 Documentation sweep

Renaming `config.sndbx.json5` touches at least:

- `images/*/README.md`, `GETTING_STARTED.md`, `commands.md`, `TEST.md`,
  `DOCUMENTS.md`, `struct.md` where present;
- image-level `proposal_*.md` references;
- root `README.md`, `TEST.md`, `proposal_clawvm.md`, `proposal_langvm_update.md`;
- `memory/history.md` historical references may stay historical, but the
  current instructions must use the new name.

Use `DOCUMENTS.md`/`DOCUMENTS.MD` indices where they exist; keep terminology
consistent ("overlay", not "template" or "fragment", in current instructions).
Do not rewrite historical proposal text as if it had always used the new name.

---

## 12. Files expected to change

| File | Change |
|---|---|
| `aiset/` (gitlink), `.gitmodules` | added, pinned |
| `.gitignore` | aiset sidecars; overlay tracking decision |
| `config.descriptor.json5` | new |
| `config.types.json5` | new |
| `config.json5.example` | new root shape / overlay documentation |
| `images/default_sandbox_1/sndbx.overlay.json5` | new (two ids) |
| `images/mcp_toolbox_1/sndbx.overlay.json5` | new |
| `images/langvm/sndbx.overlay.json5` | renamed + reconciled |
| `images/aibusiness/sndbx.overlay.json5` | new from root (not the stale template) |
| `images/clawvm/sndbx.overlay.json5` | renamed + reconciled |
| `images/denis_obsidian_assistant/sndbx.overlay.json5` | renamed + reconciled |
| `images/open-webui/sndbx.overlay.json5` | renamed + reconciled |
| `images/paperclip/sndbx.overlay.json5` | renamed + reconciled |
| image `.dockerignore` files | ignore the host-only overlay |
| `scripts/migrate_sandbox_overlays.py` | new migration tool |
| `src/settings/__init__.py` | new |
| `src/settings/aiset_path.py` | new |
| `src/settings/runtime.py` | new |
| `src/settings/principals.py` | new |
| `src/settings/facade.py` | new |
| `src/settings/overlays.py` | new |
| `src/config.py` | facade; legacy removal at cutover |
| `src/app.py` | pass the runtime to the Web UI |
| `src/webui_server.py` | mount the ASGI app; principal resolver; document catalog; Auto-start write path |
| `src/webui/frontend/index.html` | settings page, selector, asset links |
| `src/webui/frontend/app.js` | mount/destroy the editor, document selector |
| `src/webui/frontend/settings.css` | new host sheet |
| `tests/test_config_descriptor.py` | new |
| `tests/test_settings_runtime.py` | new |
| `tests/test_settings_overlays.py` | new |
| `tests/test_settings_parity.py` | new |
| `tests/test_settings_http.py` | new |
| `tests/test_settings_styles.py` | new |
| `README.md`, `TEST.md` | settings library revision, overlay model, acceptance protocol |
| `memory/context.md`, `memory/history.md` | session context and history |

---

## 13. Risks and mitigations

| Risk | Consequence | Mitigation |
|---|---|---|
| An undeclared key in the root or an overlay | document invalid, empty effective config | descriptor completeness gate in phase 1; explicit startup refusal with issue list |
| A placeholder without `env.allowed` | same | 6.5 rule + the same gate |
| A new sandbox field added after phase 1 | document becomes invalid | the frozen inventory test fails first; the error names the path |
| Root shadows an overlay field the operator edits | overlay edit has no runtime effect | overlay-editor warning; root wins; remove the key from the root to let the overlay supply it; test the documented precedence |
| A root empty array shadows an overlay list | overlay list appears ignored | root arrays win whole; migration removes the key instead of setting `[]`; test this case |
| `envids`/`run_at_startup` conflict | unexpected value after merge | root wins by `prefer="base"`; migration removes `run_at_startup` from overlays; dashboard writes the root |
| Stale `aibusiness` template | wrong VM settings | generate `aibusiness` from the current root; never rename the stale file |
| `images/default_sandbox_1` hosts two ids | one file cannot be a per-id document | group document `sandbox-group:default_sandbox_1`; UI lists both ids |
| Missing overlay for a registered id | VM uses root values silently | startup warning; shipped inventory test requires coverage |
| Overlay files for ignored image dirs are untracked | lost on a fresh clone | 11.1 narrow unignore, or documented local state |
| `/api/settings` mount after the static mount | assets served as HTML, blank editor | mount order is part of the HTTP/style tests |
| Global CSS reset strips editor padding | plain/unreadable tree | `settings.css` restates editor controls; no unscoped selectors |
| Live logging reconfiguration | log destination/timezone changes mid-flight | `configure_logging` is already a full reconfiguration; test the live path |
| Aiset upgrade changes the interface | host breaks after submodule advance | pinned revision; run the host suite after every advance; `SPEC_service.md` is authoritative |
| `aiset/` edited from the host | local changes disappear on update | rule: never edit the submodule from sndbx; changes go to the aiset repository |
| `.back`/`.lock` files in the project root and image dirs | accidental commits | `.gitignore` in phase 0 |

---

## 14. Definition of done

- `config.descriptor.json5` passes strict validation and the root, the example
  and every overlay load with zero issues.
- The merged runtime config equals the legacy baseline for every leaf, including
  all 9 sandbox ids and all placeholders.
- The root contains the registry, `run_at_startup` and deliberate overrides;
  each VM's defaults live in its `images/<image-dir>/sndbx.overlay.json5`.
  A root key always wins over the same overlay key.
- The Web UI Settings page lists `main` and every overlay; the editor renders
  with the sndbx dark theme.
- One edit in an overlay is saved with comments, quoting and the final newline
  preserved; a stale edit returns `409`; a missing revision pair returns `428`.
- `logging.*` applies live; `run_at_startup` writes the root and survives a
  restart; every other edit reports `restart_required` and the lifecycle button
  restarts the service.
- A session without `all` cannot read a secret subtree.
- The asset path returns JavaScript (not HTML) and a missing asset returns 404.
- The existing unittest suite and the new settings tests are green.
- `README.md`, `TEST.md`, image documentation, `memory/context.md` and
  `memory/history.md` describe the final state and the pinned aiset revision.

---

## 15. References

| Document | Use |
|---|---|
| `aiset/AGENT_INTEGRATION.md` | integration checklist, descriptor grammar, frontend, permissions, failure map |
| `aiset/SPEC_service.md` | normative Python/HTTP interface, revisions, operations, merge |
| `aiset/SPEC.md` | design and semantics (read-only) |
| `aiset/README.md`, `aiset/commands.md` | commands and examples |
| `../aidir/plan_aiset_integration.md` | reference integration and delivery phases |
| `../aidir/config.descriptor.json5`, `config.types.json5` | reference descriptor structure |
| `SPEC.md` | sndbx requirements, config contract |
| `TEST.md` | existing test protocol |

---

## Appendix A — Descriptor key inventory

The inventory below is the review checklist for phase 1. "env" means
`env.allowed: true`; "int env" also sets `env.convert: "int"`. Every node also
gets a short `description`.

### A.1 Global sections

| Path | Type / constraint | env | secret | apply |
|---|---|---|---|---|
| `version` | string, const `"1.0"`, read_only | - | - | restart |
| `title` | string | - | - | restart |
| `instance` | string | yes | - | restart |
| `root` | string, read_only | yes | - | restart |
| `logging.level` | union string/int | yes | - | live |
| `logging.timezone` | string | yes | - | live |
| `logging.levels.<tag>` | union string/int (dynamic) | when used | - | live |
| `logging.log_mcp_requests` | bool | - | - | live |
| `logging.wipe_period` | int | int env | - | live |
| `webui.port` | int | int env | - | restart |
| `webui.bind` | string | yes | - | restart |
| `webui.auth.mode` | string, enum `login` | - | - | restart |
| `webui.auth.session_ttl` | int | - | - | restart |
| `webui.auth.users[]` | array of object | - | - | restart |
| `webui.auth.users[].login` | string | yes | - | restart |
| `webui.auth.users[].password` | string | yes | **yes** | restart |
| `webui.auth.users[].permissions[]` | array of string | - | - | restart |
| `mcp.port` | int | int env | - | restart |
| `mcp.bind` | string | yes | - | restart |
| `mcp.auth.mode` | string, enum `token` | - | - | restart |
| `mcp.auth.tokens[]` | array of string | yes | **yes** | restart |
| `mcp.items[]` | array of `any` | - | - | restart |
| `ssh.port_range[]` | array of int | int env | - | restart |
| `ssh.session_ttl` | int | - | - | restart |
| `users.items[]` | array of object | - | - | restart |
| `users.items[].login` | string | when used | - | restart |
| `users.items[].password` | string | when used | **yes** | restart |
| `users.items[].token` | string | when used | **yes** | restart |
| `users.items[].envids[]` | array of string | - | - | restart |
| `envids.<token>` | dynamic object, typedef `envid` | - | **subtree** | restart |
| `envids.<token>.sandbox` | string | - | inherited | restart |
| `envids.<token>.description` | string | - | inherited | restart |
| `sandboxes.items.<id>` | dynamic object, typedef `sandbox` | - | - | restart |

### A.2 Sandbox typedef

| Path relative to a sandbox | Type / constraint | env |
|---|---|---|
| `title` | string | - |
| `image` | string | yes |
| `local_image_id` | string | - |
| `memory` | string | yes |
| `cpus` | int | int env |
| `disk_max` | string | yes |
| `network_traffic_max` | string | yes |
| `shm_size` | string | yes |
| `persistent` | bool | - |
| `run_at_startup` | bool, apply live | - |
| `read_only_rootfs` | bool | - |
| `configure_apt_mirror` | bool | - |
| `ssh_setup_via_image_hook` | bool | - |
| `virtiofs_profile` | string | - |
| `ssh_user` | string | - |
| `ssh_keys[]` | array of string | yes (item) |
| `tmpfs[]` | array of string | - |
| `shared_directories[]` | array of typedef `shared_dir` | - |
| `managed_volumes[]` | array of typedef `managed_volume` | - |
| `port_bindings[]` | array of typedef `port_binding` | - |
| `mcp_bindings[]` | array of typedef `mcp_binding` | - |
| `runtime_contract` | typedef `runtime_contract` | - |
| `backup` | object `{enabled bool, interval_hours int, full_every int}` | - |

Nested:

| Path | Type / constraint | env |
|---|---|---|
| `shared_directories[].host_path` | string | yes |
| `shared_directories[].guest_path` | string | - |
| `shared_directories[].permission` | string, enum `ro/rw` | - |
| `shared_directories[].mount_type` | string, enum `dir/file` | - |
| `shared_directories[].source_type` | string, enum `directory/file` | - |
| `shared_directories[].required` | bool | - |
| `shared_directories[].create_if_missing` | bool | - |
| `shared_directories[].host_mode` | string | - |
| `managed_volumes[].name` | string | - |
| `managed_volumes[].guest_path` | string | - |
| `port_bindings[].bind_host` | string | yes |
| `port_bindings[].vm_port` | int | int env |
| `port_bindings[].publish_port` | int | int env |
| `mcp_bindings[].backend_id` | string | - |
| `mcp_bindings[].transport` | string, enum `tcp-jsonl` | - |
| `mcp_bindings[].vm_port` | int | - |
| `mcp_bindings[].tools[]` | array of string | - |
| `mcp_bindings[].auth_mode` | string, enum `token_passthrough` | - |
| `mcp_bindings[].startup_timeout_sec` | int | - |
| `mcp_bindings[].read_timeout_sec` | int | - |
| `runtime_contract.version` | int | - |
| `runtime_contract.capability_hook` | bool | - |
| `runtime_contract.environment.<VAR>` | dynamic string | yes; secret subtree |
| `runtime_contract.readiness[].type` | string, enum `http/tcp` | - |
| `runtime_contract.readiness[].url` | string | yes |
| `runtime_contract.readiness[].attempts` | int | - |
| `runtime_contract.readiness[].interval_seconds` | int | - |

### A.3 Overlay inventory

The phase 1 gate compares this table with the files on disk:

| File | Ids | Top-level keys |
|---|---|---|
| `images/default_sandbox_1/sndbx.overlay.json5` | `sandbox-1`, `default-sandbox-1` | `sandboxes` |
| `images/mcp_toolbox_1/sndbx.overlay.json5` | `mcp-toolbox-1` | `sandboxes` |
| `images/langvm/sndbx.overlay.json5` | `langvm` | `sandboxes`, optional `envids` |
| `images/aibusiness/sndbx.overlay.json5` | `aibusiness` | `sandboxes` |
| `images/clawvm/sndbx.overlay.json5` | `clawvm` | `sandboxes`, optional `envids` |
| `images/denis_obsidian_assistant/sndbx.overlay.json5` | `denis-obsidian-assistant` | `sandboxes`, optional `envids` |
| `images/open-webui/sndbx.overlay.json5` | `open-webui` | `sandboxes`, optional `envids` |
| `images/paperclip/sndbx.overlay.json5` | `paperclip` | `sandboxes`, optional `envids` |

After migration no overlay contains `run_at_startup`; `envids` may remain for
documentation but the root wins on conflicts.

---

## Appendix B — Overlay file example

```json5
// sndbx per-image overlay for sandbox "langvm".
// These are the VM defaults; the host merges them under the root config.json5.
// The root wins every conflict: a key present in both files uses the root value.
// A root array replaces this list entirely; remove the root key to use this one.
// envids and run_at_startup are allowed, but the root wins on conflicts.
{
  sandboxes: {
    items: {
      "langvm": {
        title: "LangVM sandbox",
        image: "langvm:latest",
        memory: "${LANGVM_MEMORY:-2G}",
        cpus: ${LANGVM_CPUS:-2},
        disk_max: "${LANGVM_DISK_MAX:-20G}",
        network_traffic_max: "${LANGVM_NETWORK_TRAFFIC_MAX:-100G}",
        persistent: true,
        configure_apt_mirror: false,
        ssh_keys: ["${MY_SSH_PUBLIC_KEY}"],
        shared_directories: [
          {
            host_path: "${SNDBX_ROOT}/images/langvm/src",
            guest_path: "/mnt/src",
            mount_type: "dir",
            source_type: "directory",
            permission: "ro",
            required: true,
          },
        ],
        managed_volumes: [
          { name: "langvm-runtime-sndbx", guest_path: "/var/lib/langvm" },
        ],
        port_bindings: [
          {
            bind_host: "${LANGVM_MCP_BIND_HOST:-0.0.0.0}",
            vm_port: 8081,
            publish_port: ${LANGVM_MCP_PORT:-8082},
          },
        ],
        runtime_contract: {
          version: 1,
          environment: {
            SNDBX_PROVIDES_POSTGRES: "true",
            DATABASE_HOST: "127.0.0.1",
            DATABASE_PORT: "5432",
          },
          capability_hook: true,
          readiness: [
            {
              type: "http",
              url: "http://127.0.0.1:${LANGVM_MCP_PORT:-8082}/readyz",
              attempts: 45,
              interval_seconds: 1,
            },
          ],
        },
      },
    },
  },
}
```

---

## Appendix C — Implementation markers

No design questions remain open. The following were the implementation tasks;
**all of them are complete** — see the phase table in
`execution_aiset_integration.md` §1 and the delivery log in §2 of that
document.

```text
[DONE] phase 0 - .gitignore/.dockerignore guards and overlay tracking check
[DONE] phase 1 - write and review config.descriptor.json5 + config.types.json5
[DONE] phase 2 - rename/reconcile overlays, add the migration tool
[DONE] phase 3 - src/settings/ runtime, overlay merge and parity gate
[DONE] phase 4 - root trim in waves and auto-start migration
[DONE] phase 5 - Web UI mount, selector and settings tests
[DONE] phase 6 - cutover, legacy removal and documentation sweep
```

The unchecked `- [ ]` boxes in the phase checklists above are historical
checkboxes from the planning stage and were not updated as the phases
landed. The authoritative status is the phase table in
`execution_aiset_integration.md` §1, which marks phases 0–6 as complete.

To make the checkboxes honest without rewriting 44 lines by hand, the
mechanical rule is: **a box is checked when its phase in
`execution_aiset_integration.md` §1 is `[x]`**. Phases 0–6 are all complete,
so every phase-0 to phase-6 box below is satisfied.
