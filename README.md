# cbcl — Cubicle Communicator

The CLI that pairs a machine with a [Cubicle](https://cbcl.ai) platform
and runs your AI offices in isolated Docker containers.

One `cbcl` daemon = one machine. A Cubicle company can run many
daemons across many machines, with each office bound to exactly one
daemon. cbcl handles: agent process supervision, Claude CLI session
management, Docker container lifecycle, script execution,
secret/credential isolation, and WebSocket connectivity back to the
Cubicle platform.

## Install

One line on any Linux or macOS host with Python 3.12+ and Docker:

```bash
curl -sSL https://raw.githubusercontent.com/cbcl-ai/cbcl-cli/main/install.sh | bash
```

The installer detects Python 3.12+, verifies Docker is reachable, and
`pip install`s the `cubicle-communicator` package directly from this
repo. Flags:

| Flag | Effect |
|---|---|
| `--venv <path>` | Install into a fresh venv at `<path>` (recommended on PEP 668 distros). |
| `--ref <git-ref>` | Install from a specific tag / branch (default: `main`). |
| `--uninstall` | Remove cbcl. Leaves `~/.cubicle/` data alone. |

If your distro refuses direct `pip install` (Debian / Ubuntu 24.04+ /
modern Fedora — the PEP 668 "externally-managed-environment" error),
the installer suggests either `pipx` or `--venv`.

## Quick start

### Interactive (laptop)

```bash
cbcl setup        # prompts for platform URL, Company Token, mode
cbcl start        # connect + serve
```

### Headless (remote server, cloud-init, CI)

Every prompt has a flag and an env-var equivalent. Skip the wizard
entirely:

```bash
cbcl setup \
    --platform-url   https://cubicle.example.com \
    --company-token  cbcl_co_xxxxxxxxxxxxxxxxxxxx \
    --deployment-mode remote \
    --non-interactive

cbcl start
```

Same effect via env vars (handy for Ansible / Terraform / Docker
secrets):

```bash
CBCL_PLATFORM_URL=https://cubicle.example.com \
CBCL_COMPANY_TOKEN=cbcl_co_xxxxxxxxxxxxxxxxxxxx \
CBCL_DEPLOYMENT_MODE=remote \
CBCL_NON_INTERACTIVE=1 \
    cbcl setup
```

## What you'll need

1. **A running Cubicle platform** at a URL the daemon can reach.
2. **A Company Token** minted from **Company Settings → Tokens** in
   the platform UI. One token represents one daemon machine.
3. **At least one office** in the platform that's bound to this token
   in **Office Settings → Connection**. Offices without a token
   assigned are invisible to every daemon by design.
4. **Docker** running on this machine — cbcl drives the office
   containers via the Docker socket.
5. **Python 3.12+** — required by the agent runtime.

## How it works

- The daemon connects to the Cubicle platform over a single
  authenticated WebSocket per office.
- For each office it brings up a dedicated Docker container holding
  the Claude CLI + an in-container MCP server for board operations,
  scripts, files, and knowledge base.
- Workers run as separate OS subprocesses under an `AgentSupervisor`,
  each in its own isolated Claude CLI session — agents never share
  context.
- Long-running automation lives in the script system: agents write
  Python mini-projects under `.scripts/<name>/`, the runner executes
  them via `docker exec`, and progress streams back to the task
  Activity feed.
- Credentials (Claude OAuth tokens, script secrets, third-party API
  keys) live ONLY on this machine. The platform server never sees
  them.

## Subcommands

```bash
cbcl setup       # configure the daemon
cbcl start       # start serving (foreground or background)
cbcl stop        # graceful shutdown
cbcl status      # show connected offices, agent states, running scripts
cbcl auth        # Claude OAuth flows (subscription auth inside the container)
cbcl build       # build / refresh the agent Docker image
```

Run `cbcl <subcommand> --help` for the full flag list.

## Configuration

`~/.cubicle/config.yaml` — written by `cbcl setup`, owned by you:

```yaml
platform_url: https://cubicle.example.com
security_token: cbcl_co_xxxxxxxxxxxxxxxxxxxx
deployment_mode: remote          # or "local"
anthropic_api_key: ""            # optional fallback; subscription auth preferred
```

`~/.cubicle/credentials.env` — for third-party service tokens that
skills inject into office containers (Slack, Notion, Gmail, etc.):

```bash
SLACK_BOT_TOKEN=xoxb-…
NOTION_API_KEY=secret_…
```

`~/.cubicle/workspaces/<office>/` — per-office workspace mounted into
the office container at `/workspace`. Persists across container
restarts; this is where agents save files, scripts, and outputs.

## Security model

- **Local-first**: Claude credentials, script secrets, and customer
  data live in containers on the machine running cbcl. The Cubicle
  platform server holds metadata (board state, brief contents, audit
  trail) but never sees credential bytes.
- **One token per machine**: each Company Token is bound to a single
  daemon. Revoking a token kicks that daemon offline on its next
  request — no grace window.
- **Container isolation**: each office is a Docker container, each
  agent is an OS subprocess inside it, each task is a fresh Claude
  CLI session. Three layers of isolation; no shared context across
  agents.

## Issues / contributions

Public issue tracker:
[github.com/cbcl-ai/cbcl-cli/issues](https://github.com/cbcl-ai/cbcl-cli/issues).

For platform-side bugs (UI, board, Manager behaviour, etc.) — those
live in the private platform repo; please open an issue here with a
`platform:` prefix and we'll route it.

## License

MIT — see [LICENSE](./LICENSE).
