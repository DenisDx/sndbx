# sndbx document index

This file lists the project documents and what each one owns. It is an index,
not a specification; the documents themselves are authoritative for their
subject.

## Core project documents

| Document | Purpose |
|---|---|
| `README.md` | Installation, configuration, Web UI, MCP and operations overview. |
| `GETTING_STARTED.md` | First-run walkthrough for a new installation. |
| `SPEC.md` | Project requirements and architecture. **Read-only for agents.** |
| `AGENTS.md` | Agent workflow and repository rules. **Read-only for agents.** |
| `SPEC_.md` | Required corrections to the read-only `SPEC.md` (configuration locations and the settings tab). |
| `TEST.md` | Unit, integration, smoke and browser test protocols. |
| `MCP_USAGE.md` | MCP transport and tool usage. |
| `MVP_SUMMARY.md` | Scope summary of the first delivered version. |
| `SSH_SYNC_AND_SHARED_SETUP.md` | SSH and shared-directory setup notes. |

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
