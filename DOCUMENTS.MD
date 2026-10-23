# sndbx document index

This file lists the project documents and what each one owns. It is an index,
not a specification; the documents themselves are authoritative for their
subject.

## Core project documents

| Document | Purpose |
|---|---|
| `README.md` | Installation, configuration, Web UI, MCP and operations overview. |
| `GETTING_STARTED.md` | First-run walkthrough for a new installation. |
| `SPEC_NEW.md` | **Developer and agent specification.** Purpose, architecture, domain model, component catalogue, configuration model, filesystem layout and engineering decisions. Supersedes `SPEC.md`. |
| `SPEC.md` | Original project requirements and architecture. **Read-only for agents. Superseded by `SPEC_NEW.md`; kept for history.** |
| `AGENTS.md` | Agent workflow and repository rules. **Read-only for agents.** |
| `SPEC_.md` | Required corrections to the read-only `SPEC.md` (configuration locations and the settings tab). |
| `TEST.md` | Unit, integration, smoke and browser test protocols. |
| `MCP_USAGE.md` | MCP transport and tool usage. |
| `MVP_SUMMARY.md` | Scope summary of the first delivered version. |
| `SSH_SYNC_AND_SHARED_SETUP.md` | SSH and shared-directory setup notes. |

## Specification topic documents

`SPEC_NEW.md` is the entry point; these documents carry the depth for the
largest subsystems. Each one is referenced from the relevant section of
`SPEC_NEW.md`.

| Document | Owns |
|---|---|
| `docs/IMAGES.md` | The image contract: `images/<id>` layout, `app.py` hook protocol, runtime contract, persistent data layout, the image catalogue, and how to add an image. |
| `docs/INTERFACES.md` | Protocol reference: MCP HTTP/SSE, Web UI REST, console WebSocket, settings ASGI API, image hook, runtime contract, host configuration contract. |
| `docs/HOST_OPERATIONS.md` | Host-side provisioning: prerequisite installer, application installer, service units, the `aiset` submodule dependency, Kata runtimes and the ACL variant, the privileged backup helper, host state inventory, known tooling defects. |
| `docs/STATUS_AND_ROADMAP.md` | What is implemented, what is declared but not implemented, known defects, open tasks by priority, planned directions, and how to verify the current state. |

## Settings subsystem (aiset)

| Document | Purpose |
|---|---|
| `proposal_aiset_integration.md` | Design of record: descriptor, root/overlay model, runtime, Web UI, phases and risks. |
| `execution_aiset_integration.md` | Actual delivery: phase status, commands, deviations, test results and remaining operational steps. |
| `config.descriptor.json5` | Schema for the root configuration and every overlay. |
| `config.types.json5` | Descriptor type templates used by the schema. |
| `scripts/migrate_sandbox_overlays.py` | One-time overlay generation, auto-start import and root-trim migration. |

## Image documents

Each `images/<image-id>/` directory is a separate repository with its own
`README.md`, `commands.md`, `TEST.md`, and (where present) `DOCUMENTS.md`. Those
documents describe the image, its in-VM application configuration
(`config.json5`) and its host overlay (`sndbx.overlay.json5`). They do not
describe the root sndbx repository.

## Proposals and historical documents

| Document | Purpose |
|---|---|
| `proposal_backup.md` | Per-VM backup design. |
| `proposal_monitoring.md` | Implemented per-VM runtime monitoring: Docker stats, guest memory/OOM probe, dashboard switches, thresholds and limitations. |
| `proposal_clawvm.md` | ClawVM sandbox design and migration. |
| `proposal_langvm_update.md` | LangVM image update procedure. |
| `proposal_update_2.md` | Earlier update plan. |
| `_kata_acl_install.md` | Kata ACL installation notes. |

Image-local `proposal_*.md` files are owned by their image repositories.

## Memory

| Document | Purpose |
|---|---|
| `memory/context.md` | Durable context between agent sessions; keep under 20,000 characters. |
| `memory/history.md` | Chronological record of user requests and results. |
