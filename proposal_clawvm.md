# ClawVM: Minimal OpenClaw Virtual Machine Proposal

## 1. Objective

Create `images/clawvm/` as an independent Git repository template for a blank
virtual machine intended to host one user-installed OpenClaw instance. It must
follow the operational model of `images/langvm`: clone the template, start it
locally with Docker or inside sndbx, install and configure OpenClaw from its
official instructions through the console, then retain and move the complete
mutable instance by copying `data/`.

ClawVM is a generic host for an OpenClaw installation, not a reimplementation
of OpenClaw and not a new agent framework. Reuse LangVM deployment patterns
only where they solve a deployment problem; do not copy its LangGraph,
PostgreSQL, REST, MCP, worker, scheduler, UI, or policy code.

## 2. Scope

The first version must provide:

- Docker Compose mode for local development and operation.
- sndbx mode that runs the same image in a microVM.
- Configurable host-to-instance port publication in both modes.
- One writable host directory, `data/`, mounted as the complete OpenClaw state
  and workspace.
- A strict portability invariant: copying `data/` over the `data/` directory of
  another ClawVM checkout creates an equivalent working instance.
- Outbound network access from OpenClaw to the Internet and machines reachable
  on the host network.
- A documented way to publish the OpenClaw workspace at a stable in-instance
  path.
- Host-side Samba sharing of the workspace only, with bidirectional OpenClaw and
  user read/write access through ACLs rather than world-writable permissions.
- First-launch console access so the user can install and configure the latest
  OpenClaw release through the official installation flow.
- A broad preinstalled command-line toolkit for agent work, automation,
  development, archive handling, database clients, and diagnostics.
- Minimal health and diagnostic commands.

The first version must not add a database, separate worker, custom REST API,
management UI, scheduler, custom tool policy, or application-specific code.
OpenClaw retains its own behavior, configuration, tools, and permissions.

## 3. Design Decisions

### 3.1 Base runtime

The initial ClawVM image must not contain OpenClaw. It provides only a maintained
base operating system and the small set of console/install prerequisites required
by the official OpenClaw installation flow. The user downloads, installs, and
configures the current OpenClaw release after starting the VM.

The base image must create a fixed non-root `clawvm` user and group with UID/GID
`1000`. The runtime process and interactive installation run as this user. Its
home directory is `/home/clawvm`; the host `./data` directory is mounted there,
so `data/` is the complete native home directory of OpenClaw and its related
user-local tools.

The only project-owned runtime code may be a minimal entrypoint: when
`/home/clawvm/config/start.sh` is executable, it must `exec` that user-owned startup
script; otherwise it must run `sleep infinity` so the VM remains available in a
console. It must not interpret requests, filter tools, proxy traffic, or persist
data outside the mounted home directory.

### 3.1.1 Preinstalled software

ClawVM is intentionally blank with respect to OpenClaw, but it is not a sparse
shell image. The base image must use a maintained Ubuntu or Debian release and
install this broad, non-interactive baseline with `apt-get install -y
--no-install-recommends`:

```text
ca-certificates curl wget gnupg lsb-release
git openssh-client bash-completion
jq
unzip zip p7zip-full tar gzip bzip2 xz-utils zstd
make gcc g++ build-essential pkg-config
python3 python3-pip python3-venv python3-dev pipx
sqlite3 libsqlite3-dev postgresql-client default-mysql-client redis-tools
rsync rclone
tzdata locales
file tree less nano vim-tiny
procps psmisc lsof strace htop btop iotop iftop
tmux screen
acl attr
dnsutils iproute2 iputils-ping traceroute mtr-tiny
netcat-openbsd socat nmap whois
openssl age
ripgrep fd-find fzf parallel pv entr shellcheck yamllint
```

Install the current active Node.js LTS through its official vendor distribution
rather than the potentially obsolete distribution `nodejs` package. The image
must expose `node`, `npm`, `npx`, `corepack`, Python 3, `pipx`, Git, and the
database client commands on `PATH`. The OpenClaw local-prefix installer remains
the source of OpenClaw itself; Node.js is a general-purpose tool and an
installation fallback, not a bundled OpenClaw release.

Install the maintained Go implementation of `yq` as a separately versioned,
checksum-verified binary. Do not use Debian/Ubuntu's `yq` package: it is a
different Python/jq wrapper and is not command-compatible with the widely used
Go `yq`. Add an image-owned `fd` symlink to `fdfind` so the documented command
name works on Debian-family systems.

The default image must not include `openssh-server`, `cron`, `at`, `logrotate`,
`ufw`, `iptables`, `nftables`, or `fail2ban`. They are services or host-network
security controls that need an init system, persistent service state, or Linux
capabilities deliberately absent from this non-root, read-only-root runtime.
Database servers are likewise out of scope; only clients are preinstalled.
Do not grant `clawvm` passwordless `sudo`: system-package changes belong in a
new image build, while user-scoped packages installed through `pipx`, npm, or
the OpenClaw installer remain portable below `data/`.

`tcpdump` and `tshark` are excluded from the default image because packet capture
needs additional Linux capabilities and can expose unrelated network traffic.
Provide a documented, explicitly opt-in diagnostic image variant for them only
when Docker or sndbx can grant the minimum required capabilities. This variant
must retain the same non-root user, read-only-root, and data-mount contract.

### 3.2 Portable state and workspace

`data/` is the only persistent writable mount and the portable instance
boundary. It contains all user-created and mutable content:

```text
data/
  .openclaw/              # native config, state, agents, workspace, plugins
  .config/openclaw/       # native auth-profile secret material
  .local/                 # user-local OpenClaw and related CLI installations
  config/start.sh         # user-owned launch command for the installed gateway
```

The runtime must set `HOME=/home/clawvm` and
`OPENCLAW_HOME=/home/clawvm/.openclaw`. This uses OpenClaw's native default
paths: `~/.openclaw/openclaw.json` for configuration and
`~/.openclaw/workspace` for the default workspace. No separate configuration
translation layer or second writable mount is permitted.

The container root filesystem must be read-only in Docker and sndbx modes. The
only permitted writable paths outside `/home/clawvm` are explicitly declared `tmpfs`
directories such as `/tmp` and `/run`; their contents are intentionally
ephemeral and must not contain OpenClaw state, credentials, configuration, or
workspace files. A persistent write outside the mounted home is a deployment defect.

After the interactive installation, the user creates `data/config/start.sh` from
the documented OpenClaw startup command. This makes the installed instance start
after Docker or sndbx restarts while keeping the command and all mutable state in
the portable `data/` tree.

The only host-visible collaboration path is
`data/.openclaw/workspace` (`/home/clawvm/.openclaw/workspace` in the VM).
Do not publish the surrounding home directory: it contains configuration,
credentials, and user-local tool state. The workspace must be a setgid directory
owned by UID `1000` and a dedicated host group such as `clawvm-share`. Its POSIX
default ACL must grant read/write/execute access to UID `1000` and that group,
with no access for other users. The OpenClaw startup command must use
`umask 0007`. This lets OpenClaw and Samba-authorized host users create and edit
one another's workspace files without granting `777` permissions.

Copying or backing up `data/` must be sufficient to move the OpenClaw instance,
including its configuration, credentials, workspace, and runtime state. The
repository itself contains only image/deployment templates and examples.

`data/` is ignored by the ClawVM repository except tracked empty-directory
markers and non-secret examples. Real secrets must remain only under `data/` and
must not be committed.

### 3.3 Docker mode

Provide a small `docker-compose.yml` with one `clawvm` service:

- build or reference the minimal ClawVM base image without OpenClaw;
- run as the image-defined `clawvm` user with UID/GID `1000`;
- mount `./data` read-write at `/home/clawvm`;
- set `read_only: true` for the container root filesystem;
- provide only the documented ephemeral `tmpfs` paths required by the base
  image and OpenClaw installer;
- set `HOME=/home/clawvm` and `OPENCLAW_HOME=/home/clawvm/.openclaw`;
- declare an optional configurable port mapping for the listener selected by the
  user during OpenClaw setup;
- provide an interactive console before OpenClaw is installed;
- not define an OpenClaw healthcheck in the base template.

The Compose configuration must expose its port mapping through environment
variables with safe loopback defaults. A documented explicit non-loopback bind
enables LAN access. Do not use Docker host-network mode: it removes predictable
port publication and weakens the deployment boundary unnecessarily.

The installation instructions must use OpenClaw's local-prefix installer with
`HOME=/home/clawvm`; its default `~/.openclaw` prefix then remains inside
`data/`. They must not use a global package install or any default path under
the container root filesystem.

### 3.4 sndbx mode

Provide `config.sndbx.json5` as a mergeable sandbox template, modelled on the
small deployment contract in LangVM. It must define:

- a `clawvm` sandbox using the same base image as Docker mode;
- resource defaults and persistent operation;
- exactly one required shared directory: `<clawvm checkout>/data` to
  `/home/clawvm`
  with read-write permission and `create_if_missing: true`;
- a required read-only root filesystem setting, implemented by sndbx as Docker
  `--read-only`, plus the same documented ephemeral `tmpfs` paths as Compose;
- one optional configurable `port_bindings` entry from a host port to the
  listener port selected during OpenClaw setup;
- no `runtime_contract` or readiness probe in the base template.

The surrounding sndbx root configuration is deployment metadata, not ClawVM
runtime state. It necessarily registers the sandbox and port binding on the host.
It must contain no OpenClaw secrets. Cloning or copying the ClawVM `data/`
directory remains sufficient to move the actual instance state.

No lifecycle hook is required. The ordinary sndbx sandbox startup path retains
the blank VM through `sleep infinity`, which permits console-based installation.

The current sndbx sandbox configuration does not expose a read-only-root option.
Before ClawVM is implemented, add the smallest platform option required for this
contract, for example `read_only_rootfs: true`, which emits Docker `--read-only`
for the sandbox. This is required to enforce portability rather than merely
document it.

### 3.5 Networking

Do not add application-level egress restrictions. Docker mode uses its normal
bridge network; sndbx mode uses the guest's normal network. Both must permit
OpenClaw to reach the Internet and hosts routable from the physical host's LAN.

Document that services on the physical Docker host are not automatically reached
as `localhost` from the container. When required, expose them through an
explicit published host port or the platform-supported host gateway name. This
preserves the requested network access while retaining deterministic ports in
both deployment modes.

## 4. Repository Contents

The initial `images/clawvm/` repository should contain only:

```text
clawvm/
  Dockerfile                 # minimal console/install base; no OpenClaw
  docker-compose.yml         # one ClawVM console service
  config.sndbx.json5         # sndbx sandbox template
  .env.example               # non-secret port/image defaults, if Compose needs it
  README.md                  # clone, configure, run, backup, and diagnostics
  commands.md                # short operator command list
  SAMBA_SETUP.md             # host Samba and ACL setup for the workspace only
  data/
    config/start.sh.example  # user-owned command that starts installed OpenClaw
    .openclaw/.gitkeep       # native OpenClaw state and workspace root
    .config/openclaw/.gitkeep # native auth-profile secret root
    .local/.gitkeep          # user-local CLI installation root
```

Do not create `src/`, Python requirements, a custom server, or a test UI unless
an unavoidable upstream OpenClaw integration requirement proves they are needed.

## 5. Implementation Steps

1. Select the maintained base image and verify that it supports console access
  and the official current OpenClaw installation flow without placing OpenClaw
  in the ClawVM image.
2. Implement and build-test the documented package baseline, the official
   current Node.js LTS distribution, the checksum-verified Go `yq` binary, and
   the `fd` compatibility symlink. Keep the privileged packet-capture tools in
   a separate opt-in image variant.
3. Create the independent `images/clawvm` Git repository and add its own
   `.gitignore` for `data/` contents, secrets, logs, and local editor files.
4. Implement the one-service Docker Compose deployment with the
  `/home/clawvm` mount, fixed UID/GID `1000`, native home variables, and
  configurable port mapping.
5. Implement the matching sndbx template with the same image,
  `/home/clawvm` mount, fixed UID/GID `1000`, port mapping, and no hook unless
  it is demonstrably required.
6. Write the short README and command reference for Docker and sndbx console
  access, first-launch OpenClaw installation, creating `data/config/start.sh`,
  configuring a listener/port, and backup/restore by copying `data/`.
7. Add the minimal sndbx `read_only_rootfs` configuration support and configure
  ClawVM to use it with only documented `tmpfs` paths outside `/home/clawvm`.
8. Write `SAMBA_SETUP.md` with commands to create the host collaboration group,
  initialize setgid and default ACLs on `data/.openclaw/workspace`, add the
  Samba share, restrict it to that group, and test both directions of access.
  It must explicitly prohibit sharing `data/` or `/home/clawvm` as a whole.
9. Run the acceptance checks below in Docker mode and sndbx mode.

## 6. Acceptance Criteria

### Docker mode

- A fresh clone starts as a console-accessible VM without OpenClaw installed.
- The documented baseline commands are available, including `node`, `npm`,
  `npx`, `corepack`, `python3`, `pipx`, `git`, `jq`, `yq`, `fd`, `rg`,
  `sqlite3`, `psql`, `mysql`, and `redis-cli`; `yq --version` identifies the
  Go implementation.
- `clawvm` has no passwordless `sudo`; the default image has no running
  SSH, scheduler, firewall, or database-server daemon.
- A write to a persistent path outside `/home/clawvm` fails; writes to the declared
  `tmpfs` paths disappear after recreation.
- The user can install the current OpenClaw release through its official
  local-prefix installer and persist it below `data/`.
- OpenClaw can read and write its native `/home/clawvm/.openclaw/workspace`
  after installation.
- A Samba-authorized host user can read and edit files created by OpenClaw in
  the workspace, and OpenClaw can read and edit files created through that
  Samba share. Neither side needs `777` permissions.
- Samba exposes only `data/.openclaw/workspace`; attempts to browse OpenClaw
  configuration or credentials through the share fail.
- Its user-configured listener is reachable through the configured host port.
- It can reach an Internet endpoint and a reachable LAN endpoint.
- Stopping the service, copying `data/` to a fresh clone, and starting there
  restores the same OpenClaw state, credentials, workspace, and startup command
  without copying a container filesystem layer.

### sndbx mode

- The same base image starts through the supplied sndbx configuration.
- `/home/clawvm` is the only writable shared mount and is writable by UID/GID
  `1000` in the guest.
- The sandbox root filesystem is read-only; only the declared `tmpfs` paths are
  writable outside `/home/clawvm` and none retain state after recreation.
- The user can open a sandbox console, install OpenClaw, and configure its
  listener.
- The configured host port reaches the user-configured OpenClaw listener in the
  guest.
- The same host workspace path supports the documented Samba ACL workflow,
  including bidirectional file creation and editing without `777` permissions.
- OpenClaw can reach an Internet endpoint and a reachable LAN endpoint.
- Recreating the sandbox with the same `data/` directory retains the instance.
- Copying `data/` over a second ClawVM checkout and starting its sandbox creates
  an equivalent working instance without copying a container filesystem layer.

### Minimality

- No copied LangVM application modules or bundled OpenClaw release exists in
  ClawVM.
- No custom API, database, worker, scheduler, or web UI is introduced.
- The only permitted startup wrapper executes the user-owned
  `data/config/start.sh` or retains the blank VM with `sleep infinity`.

## 7. Root Repository Integration

`images/clawvm/` is intentionally ignored by the root sndbx repository because
it is an independent Git project. The root `.gitignore` must contain
`images/clawvm/`. The ClawVM repository owner will initialize and place that
repository under version control separately.

## 8. TBD

- Какой конкретный релиз OpenClaw и официальный образ с версией или digest
  будут поддерживаться?
>> лучше сделать по возможности универсальную систему, чтобы OpenClaw мог обновляться естесвенным путем. Соответственно, используем последний релиз.
- Какими документированными переменными окружения или файлами конфигурации этот
  релиз задаёт домашний каталог, workspace, адрес и порт слушателя, а также
  аутентификацию?

>>**Ответ:** не создавать отдельный JSON5-слой ClawVM. Монтировать `data/` как
  домашний каталог непривилегированного пользователя `clawvm`:
  `/home/clawvm`. Установить `HOME=/home/clawvm` и
  `OPENCLAW_HOME=/home/clawvm/.openclaw`; тогда OpenClaw нативно использует
  `~/.openclaw/openclaw.json` и `~/.openclaw/workspace`, а credentials и
  user-local инструменты также остаются внутри `data/`. `docker-compose.yml` и
  `config.sndbx.json5` описывают только пользователя, mount home-каталога и port
  binding. `Dockerfile` не хранит пользовательскую конфигурацию и секреты.

- Есть ли у выбранного релиза стабильный readiness endpoint для Compose и sndbx?
  Если нет, какую документированную CLI- или TCP-проверку следует использовать
  при запуске?

>>**Ответ:** базовая ClawVM не содержит OpenClaw и не задаёт readiness endpoint.
  При первом запуске пользователь открывает консоль, устанавливает и настраивает
  OpenClaw штатными командами, включая Telegram, чат или другой выбранный канал.
  До создания `data/config/start.sh` VM остаётся доступной через `sleep infinity`.
  После установки доступность OpenClaw проверяется его собственным способом,
  выбранным пользователем; базовый `config.sndbx.json5` не содержит
  `runtime_contract` и readiness probe.

- Остаются ли все данные аутентификации внутри настроенного домашнего каталога?
  Если нет, как перенаправить каждый изменяемый путь под `/data` до принятия
  шаблона?

>>**Ответ:** это обязательный инвариант ClawVM. Все постоянные данные OpenClaw,
  включая секреты, credentials, сессии, кэш, конфигурацию, workspace и команду
  запуска, должны находиться под `data/`. Root filesystem работает только для
  чтения; вне `/data` допустимы лишь временные `tmpfs`-каталоги, содержимое
  которых исчезает при пересоздании. После копирования `data/` поверх каталога
  `data/` другой копии ClawVM она должна запускать эквивалентный рабочий
  инстанс без копирования контейнера или его файлового слоя.
