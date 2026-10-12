#!/usr/bin/env bash
# install-cbcl.sh — one-liner installer for the Cubicle Communicator (cbcl).
#
#   curl -fsSL https://raw.githubusercontent.com/cbcl-ai/cbcl-cli/main/install.sh | bash
#
# The public cbcl-cli repository (REPO_URL below) serves this script as
# install.sh; this monorepo copy is its development source. The published
# copy is released separately (communicator/UPSTREAM.md): diff the two
# before publishing so neither side's fixes are lost.
#
# What it does, its flags, and the rules for re-running it are in usage()
# below, which is also what --help prints. Keep that text in one place:
# the script is usually piped into bash, where it cannot read itself.
# Behaviour is covered by communicator/tests/test_install_script.py.

set -euo pipefail

usage() {
  cat <<'USAGE'
Installs the Cubicle Communicator (cbcl) on a Linux or macOS machine with
Python 3.12+ and Docker.

  curl -fsSL https://raw.githubusercontent.com/cbcl-ai/cbcl-cli/main/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/cbcl-ai/cbcl-cli/main/install.sh | bash -s -- [flags]

What it does:
  1. Installs with umask 022 (see "umask" below).
  2. Refuses to run while a cbcl daemon is running (see "Re-running").
  3. Verifies Python 3.12+ is on PATH (and offers the next step if not).
  4. Verifies the Docker daemon is reachable.
  5. Installs the cubicle-communicator package from the GitHub release
     tarball, falling back to git: with pipx, or into ~/.local, or into a
     venv if requested.
  6. Prints the next steps: pairing on a new machine, or starting and
     reopening after an upgrade.

Flags:
  --venv <path>     Install into a venv at <path> (recommended).
                    With --uninstall: remove cbcl from that venv.
  --ref <git-ref>   Install a specific tag or branch (default: main).
  --uninstall       Remove the cbcl install (keeps ~/.cubicle/ data).
                    Finds a pipx install or the cbcl on your PATH; a venv
                    install needs --venv <path> as well.
  --force           Skip the running-daemon check. Use it only for a
                    daemon you have already paused, drained and checked.
                    It keeps running the code it started with, so the
                    steps printed after an install begin by stopping it.
  -h, --help        Show this text.

umask: cbcl builds each office's container image from its installed
package files, and the agents inside the container run as a different
user. Files installed under a restrictive umask (077 is common in root
shells and on hardened servers) are readable only by the installing
user. cbcl 0.5.36 and earlier copy them into the image as they are, so
the agents cannot read their own tools and the office breaks. This
script therefore installs with umask 022 whatever the calling shell
uses, also for the versions that no longer depend on it.

Re-running upgrades cbcl in place (the same version is reinstalled), but
never while cbcl is serving work: the running daemon and its workers
keep using the files being replaced. Pause and drain first, then install:
  cbcl maintenance enable --wait 300   # pause new work, wait for running work
  cbcl maintenance status              # every office must show "drained"
  cbcl stop
  <re-run this installer with the same flags>
  cbcl start -d
  cbcl maintenance disable             # reopen once cbcl status looks right
Offices start no new work until that last command. If cbcl runs as a
service, stop and start it through the service manager instead:
sudo systemctl stop / start cbcl (systemd), or launchctl unload / load
~/Library/LaunchAgents/ai.cbcl.communicator.plist (launchd). When this
script finds such a service, the commands it prints are those.
Configuration in ~/.cubicle/ is NEVER touched by this script.
USAGE
}

# Installed files must stay readable by the office's agent user (see
# "umask" in usage). The caller's umask is kept only to explain the change.
CALLER_UMASK="$(umask)"
umask 022

REPO_URL="https://github.com/cbcl-ai/cbcl-cli.git"
PACKAGE="cubicle-communicator"
DEFAULT_REF="main"
INSTALL_VENV=""
INSTALL_REF="$DEFAULT_REF"
UNINSTALL=0
FORCE=0

# ── arg parse ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv|--ref)
      if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
        echo "Missing value for $1." >&2
        echo >&2
        usage >&2
        exit 2
      fi
      if [[ "$1" == "--venv" ]]; then INSTALL_VENV="$2"; else INSTALL_REF="$2"; fi
      shift 2
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
      echo >&2
      usage >&2
      exit 2
      ;;
  esac
done

# ── colours ────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_RED="\033[31m"; C_RESET="\033[0m"
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi
say()  { printf "${C_GREEN}==>${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}!!!${C_RESET} %s\n" "$*" >&2; }
die()  { printf "${C_RED}xxx${C_RESET} %s\n" "$*" >&2; exit 1; }

# Say so when the calling shell masked read access for other users.
if (( 8#${CALLER_UMASK} & 8#055 )); then
  warn "Your shell's umask is ${CALLER_UMASK}; this installer uses 022 so the office's agent user can read the installed files."
fi

# The tilde falls back to the account's home directory when HOME is not
# set (a bare "$HOME" would abort the script under `set -u`), which is
# also where cbcl itself looks.
USER_HOME=~
CUBICLE_HOME="$USER_HOME/.cubicle"
USER_BIN="$USER_HOME/.local/bin"

# ── how cbcl is stopped and started on this machine ───────────────
# A cbcl set up as a service (the docs' "Running cbcl as a service": the
# systemd unit cbcl.service, or the launchd job ai.cbcl.communicator in
# ~/Library/LaunchAgents) is stopped and started through its service
# manager. A `cbcl start -d` by hand runs outside the service, and a
# hand-run `cbcl stop` that has to force the daemon counts as a failure,
# which the service manager answers by starting cbcl again. The commands
# this script prints follow what it finds.
#
# CBCL_SERVICE names the service manager cbcl is set up with (empty for
# none). CBCL_SERVICE_RUNS is 1 while that service is started (the
# systemd unit is active, the launchd job is loaded), so that the service
# manager is what stops cbcl: a cbcl started by hand next to a service
# that is not started (the old upgrade steps did that) is stopped by hand,
# and started again through the service. detect_service sets both from
# what the service managers report at the time; the script asks again
# before it prints the steps that follow an install, because a service
# manager can start cbcl again in the meantime.
LAUNCHD_LABEL="ai.cbcl.communicator"
LAUNCHD_PLIST="$USER_HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
# shellcheck disable=SC2088  # printed for the reader's shell to expand
LAUNCHD_PLIST_SHOWN="~/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
detect_service() {
  CBCL_SERVICE=""
  CBCL_SERVICE_RUNS=0
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet cbcl.service 2>/dev/null; then
      CBCL_SERVICE="systemd"
      CBCL_SERVICE_RUNS=1
    elif systemctl is-enabled --quiet cbcl.service 2>/dev/null; then
      CBCL_SERVICE="systemd"
    fi
  fi
  if [[ -z "$CBCL_SERVICE" && -f "$LAUNCHD_PLIST" ]] && command -v launchctl >/dev/null 2>&1; then
    CBCL_SERVICE="launchd"
    if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
      CBCL_SERVICE_RUNS=1
    fi
  fi
}
detect_service
# Stopping or starting a system service needs root.
SUDO="sudo "
if [[ $EUID -eq 0 ]]; then
  SUDO=""
fi

# service_name: the service cbcl is set up as, for messages.
service_name() {
  case "$CBCL_SERVICE" in
    systemd) echo "the systemd service cbcl.service" ;;
    launchd) echo "the launchd job $LAUNCHD_LABEL" ;;
  esac
}

# stop_command / start_command <cbcl>: how to stop or start cbcl on this
# machine, <cbcl> being how to call cbcl by hand.
stop_command() {
  if [[ $CBCL_SERVICE_RUNS -eq 0 ]]; then
    echo "$1 stop"
    return
  fi
  case "$CBCL_SERVICE" in
    systemd) echo "${SUDO}systemctl stop cbcl" ;;
    launchd) echo "launchctl unload $LAUNCHD_PLIST_SHOWN" ;;
  esac
}
start_command() {
  case "$CBCL_SERVICE" in
    systemd) echo "${SUDO}systemctl start cbcl" ;;
    launchd) echo "launchctl load $LAUNCHD_PLIST_SHOWN" ;;
    *) echo "$1 start -d" ;;
  esac
}

# ── precheck: no running daemon ───────────────────────────────────
# A running cbcl records its PID in ~/.cubicle/communicator.pid.
# Replacing the package under it (or under its workers) mixes old and
# new code while tasks are running.
#
# The file outlives a daemon that was killed, and PIDs are reused, so
# the PID alone proves nothing: the process must also look like a cbcl
# daemon, i.e. the detached entry (`python -m src._daemon_entry`) or the
# console script (`python …/cbcl start`). communicator/src/daemon.py
# (_DAEMON_COMMAND) carries the same pattern.
DAEMON_COMMAND_RE='(^|[[:space:]])-m[[:space:]]+src\._daemon_entry([[:space:]]|$)|(^|[[:space:]/])cbcl([[:space:]]+[^[:space:]]+)*[[:space:]]+start([[:space:]]|$)'

# pid_state <pid> prints: running (a cbcl daemon), other (another
# program has the PID) or gone.
pid_state() {
  local pid="$1" cmdline
  if ! kill -0 "$pid" 2>/dev/null; then
    echo gone
    return
  fi
  # -ww keeps ps from cutting the command line short. When ps cannot
  # answer, keep the safe assumption that it is the daemon.
  cmdline="$(ps -ww -p "$pid" -o command= 2>/dev/null || true)"
  if [[ -z "$cmdline" || "$cmdline" =~ $DAEMON_COMMAND_RE ]]; then
    echo running
  else
    echo other
  fi
}

PID_FILE="$CUBICLE_HOME/communicator.pid"

# pid_file_pid prints the PID that $PID_FILE names (empty without one).
pid_file_pid() {
  local pid=""
  if [[ -r "$PID_FILE" ]]; then
    pid="$({ head -c 32 "$PID_FILE" 2>/dev/null || true; } | tr -cd '0-9')"
  fi
  echo "${pid:0:10}"
}

# running_cbcl_pid prints that PID while it is a running cbcl daemon.
running_cbcl_pid() {
  local pid
  pid="$(pid_file_pid)"
  if [[ -n "$pid" && "$(pid_state "$pid")" == running ]]; then
    echo "$pid"
  fi
}

# stop_before_start succeeds when something must be stopped before the
# installed cbcl can start: a cbcl that is still running (installed over
# with --force, or started again by its service manager during the
# install) keeps the code it started with while the workers it starts
# load the new files, and a launchd job that is still loaded (`cbcl stop`
# leaves it loaded) makes `launchctl load` fail. Call detect_service
# first.
stop_before_start() {
  [[ -n "$(running_cbcl_pid)" ]] && return 0
  [[ "$CBCL_SERVICE" == launchd && $CBCL_SERVICE_RUNS -eq 1 ]]
}

if [[ $FORCE -eq 0 ]]; then
  RUNNING_PID="$(pid_file_pid)"
  if [[ -n "$RUNNING_PID" ]]; then
    case "$(pid_state "$RUNNING_PID")" in
      running)
        if [[ $UNINSTALL -eq 1 ]]; then
          warn "cbcl is running (PID $RUNNING_PID). Removing it takes away files its workers are using."
        else
          warn "cbcl is running (PID $RUNNING_PID). Installing over it replaces files its workers are using."
        fi
        if [[ -n "$CBCL_SERVICE" && $CBCL_SERVICE_RUNS -eq 0 ]]; then
          warn "It runs outside $(service_name) (it was started by hand), so stop it by hand."
        fi
        warn "Pause and drain it first:"
        warn "  cbcl maintenance enable --wait 300"
        warn "  cbcl maintenance status        # every office must show \"drained\""
        warn "  $(stop_command cbcl)"
        if [[ $UNINSTALL -eq 1 ]]; then
          warn "Then re-run this installer."
        else
          warn "Then re-run this installer, start with '$(start_command cbcl)' and reopen with 'cbcl maintenance disable'."
        fi
        die "Refusing to change the install while cbcl is running (--force skips this check; use it only for a drained daemon)."
        ;;
      other)
        warn "Stale PID file: $PID_FILE names PID $RUNNING_PID, which is another program, not cbcl. Continuing."
        ;;
    esac
  fi
fi

# ── which install is this? ────────────────────────────────────────
# The installed version of the package in the place this run works on:
# the venv given with --venv, else pipx, else the given interpreter's
# own site. Prints nothing when it is not installed there.
pip_version() {
  { "$1" -m pip show "$PACKAGE" 2>/dev/null || true; } | awk '/^Version:/ { print $2; exit }'
}
pipx_version() {
  { pipx list --short 2>/dev/null || true; } | awk -v package="$PACKAGE" '$1 == package { print $2; exit }'
}

# ── uninstall ──────────────────────────────────────────────────────
# Remove the package the way it was installed and say what happened.
# `pip uninstall` exits 0 for a package that is not installed, so every
# path checks first and never reports a removal that did not take place.

# The interpreter a console script runs under, from its shebang.
entry_point_python() {
  local line launch="" interpreter argument
  {
    IFS= read -r line || return 1
    IFS= read -r launch || true
  } 2>/dev/null < "$1" || return 1
  [[ "$line" == '#!'* ]] || return 1
  read -r interpreter argument _ <<< "${line#\#!}"
  if [[ "$interpreter" == /bin/sh ]]; then
    # pip writes a "#!/bin/sh" launcher when the interpreter path has a
    # space or is too long for a shebang. The interpreter is then on the
    # second line, quoted when it has a space:
    #   '''exec' "<python>" "$0" "$@"
    [[ "$launch" == "'''exec' "* ]] || return 1
    launch="${launch#"'''exec' "}"
    if [[ "$launch" == '"'* ]]; then
      launch="${launch#\"}"
      interpreter="${launch%%\"*}"
    else
      interpreter="${launch%% *}"
    fi
  elif [[ "$interpreter" == */env && -n "$argument" ]]; then
    interpreter="$(command -v "$argument" 2>/dev/null || true)"
  fi
  [[ -n "$interpreter" && -x "$interpreter" && "$(basename "$interpreter")" == python* ]] || return 1
  echo "$interpreter"
}

pip_uninstall() {
  local python="$1" where="$2"
  say "Removing $PACKAGE from $where …"
  "$python" -m pip uninstall -y "$PACKAGE" || die "pip uninstall failed. cbcl is still installed in $where."
  if [[ -n "$(pip_version "$python")" ]]; then
    die "cbcl is still installed in $where after pip uninstall."
  fi
}

uninstall_cbcl() {
  local python="" where="" entry candidate left unread=""
  if [[ -n "$INSTALL_VENV" ]]; then
    python="$INSTALL_VENV/bin/python"
    [[ -x "$python" ]] || die "No venv at $INSTALL_VENV (there is no $python). Nothing was removed."
    [[ -n "$(pip_version "$python")" ]] || die "cbcl is not installed in the venv at $INSTALL_VENV. Nothing was removed."
    pip_uninstall "$python" "the venv at $INSTALL_VENV"
    say "The venv itself was left in place. If it was only for cbcl, remove it: rm -rf $INSTALL_VENV"
  elif command -v pipx >/dev/null 2>&1 && [[ -n "$(pipx_version)" ]]; then
    say "Removing the pipx install of $PACKAGE …"
    pipx uninstall "$PACKAGE" || die "pipx uninstall failed. cbcl is still installed."
    [[ -z "$(pipx_version)" ]] || die "pipx still lists $PACKAGE after pipx uninstall."
  else
    # Only the install that the cbcl command leads to is removed. Another
    # Python that happens to have the package (a development checkout,
    # another environment) is not this script's to touch.
    for entry in "$(command -v cbcl 2>/dev/null || true)" "$USER_BIN/cbcl"; do
      [[ -n "$entry" && -f "$entry" ]] || continue
      candidate="$(entry_point_python "$entry" || true)"
      if [[ -z "$candidate" ]]; then
        unread="$entry"
        continue
      fi
      if [[ -n "$(pip_version "$candidate")" ]]; then
        python="$candidate"
        where="the Python behind $entry ($candidate)"
        break
      fi
    done
    if [[ -z "$python" && -n "$unread" ]]; then
      warn "Found $unread, but could not tell which Python it runs on."
      warn "If you installed into a venv, pass it: --uninstall --venv <path>"
      die "Nothing was removed."
    elif [[ -z "$python" ]]; then
      warn "No cbcl install found: pipx does not list $PACKAGE, and there is no cbcl on your PATH or in $USER_BIN whose Python has it."
      warn "If you installed into a venv, pass it: --uninstall --venv <path>"
      die "Nothing was removed."
    fi
    pip_uninstall "$python" "$where"
  fi

  hash -r 2>/dev/null || true
  left="$(command -v cbcl 2>/dev/null || true)"
  if [[ -z "$left" && -e "$USER_BIN/cbcl" ]]; then
    left="$USER_BIN/cbcl"
  fi
  if [[ -n "$left" ]]; then
    if [[ -n "$INSTALL_VENV" ]]; then
      say "Note: another cbcl is still installed at $left."
    else
      warn "Removed one install, but cbcl is still installed at $left (a second install)."
      warn "Run this again to remove it, or with --venv <path> if it lives in a venv."
      exit 1
    fi
  fi
  say "Done. Your data under ~/.cubicle/ was left untouched."

  # A service left in place keeps trying to start the removed cbcl. While
  # another cbcl is installed the service may be that one's, so nothing
  # is said then.
  if [[ -z "$left" ]]; then
    case "$CBCL_SERVICE" in
      systemd)
        warn "cbcl is still set up as $(service_name), which would keep trying to start the removed cbcl. Remove the service:"
        warn "  ${SUDO}systemctl disable --now cbcl"
        warn "  ${SUDO}rm /etc/systemd/system/cbcl.service    # the unit file you created"
        warn "  ${SUDO}systemctl daemon-reload"
        ;;
      launchd)
        warn "cbcl is still set up as $(service_name), which would try to start the removed cbcl at your next login. Remove the job:"
        warn "  launchctl unload $LAUNCHD_PLIST_SHOWN"
        warn "  rm $LAUNCHD_PLIST_SHOWN"
        ;;
    esac
  fi
}

if [[ $UNINSTALL -eq 1 ]]; then
  say "Uninstalling cbcl…"
  uninstall_cbcl
  exit 0
fi

# ── precheck: Python 3.12+ ─────────────────────────────────────────
say "Checking Python 3.12+ …"
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  die "Python 3.12+ required. Install via your package manager (e.g. 'sudo apt install python3.12 python3.12-venv' on Ubuntu, 'brew install python@3.12' on macOS), then re-run."
fi
say "Using $($PYTHON_BIN --version) at $(command -v "$PYTHON_BIN")"

# ── precheck: Docker ──────────────────────────────────────────────
say "Checking Docker daemon reachable …"
if ! command -v docker >/dev/null 2>&1; then
  die "Docker not installed. cbcl needs Docker to run office containers. Install Docker Engine, then re-run."
fi
if ! docker info >/dev/null 2>&1; then
  warn "docker info failed — make sure the daemon is running and your user is in the 'docker' group. Continuing install anyway (you can run 'cbcl setup' once the daemon is up)."
fi

# ── precheck: git (needed only for the git fallback) ──────────────
if ! command -v git >/dev/null 2>&1; then
  say "note: git not found — fine for the tarball install path; the git fallback would need it."
fi

# ── install ───────────────────────────────────────────────────────
# PRIMARY source: the GitHub release tarball over plain HTTPS
# (codeload) — deterministic, no git needed, and immune to GitHub's
# INTERMITTENT anonymous git-over-HTTPS rate limiting (observed on
# Hetzner IPs 2026-09-02: bursty 401 windows — "could not read
# Username … terminal prompts disabled" — while tarballs keep
# working; each retried clone keeps the throttle window hot).
# Works for branches and tags alike. The git+ spec stays as the
# fallback for exotic refs.
PIP_SRC_TARBALL="${REPO_URL%.git}/archive/${INSTALL_REF}.tar.gz"
PIP_SRC_GIT="git+${REPO_URL}@${INSTALL_REF}"

# Where this run installs: venv, pipx or the user site.
if [[ -n "$INSTALL_VENV" ]]; then
  INSTALL_KIND="venv"
elif command -v pipx >/dev/null 2>&1; then
  INSTALL_KIND="pipx"
else
  INSTALL_KIND="user-site"
fi

installed_version() {
  case "$INSTALL_KIND" in
    venv)
      if [[ -x "$INSTALL_VENV/bin/python" ]]; then pip_version "$INSTALL_VENV/bin/python"; fi
      ;;
    pipx) pipx_version ;;
    *) pip_version "$PYTHON_BIN" ;;
  esac
}

# What was installed before this run, to say honestly what changed and,
# when the install fails, what is left.
PREVIOUS_VERSION="$(installed_version)"

# The user-site install keeps pip's error text here (see that branch).
PIP_ERR_FILE=""

# A different source cannot fix an externally managed interpreter
# (PEP 668), so that failure is not retried through git.
externally_managed() {
  [[ -n "$PIP_ERR_FILE" ]] && grep -q "externally-managed-environment" "$PIP_ERR_FILE" 2>/dev/null
}

# try_sources <command…>: run the command with the source as its last
# argument, the tarball first and git when that fails. The source that
# worked is remembered, so a second step does not start over.
INSTALL_SOURCE=""
try_sources() {
  if [[ -n "$INSTALL_SOURCE" ]]; then
    "$@" "$INSTALL_SOURCE"
    return
  fi
  if "$@" "$PIP_SRC_TARBALL"; then
    INSTALL_SOURCE="$PIP_SRC_TARBALL"
    return 0
  fi
  if externally_managed; then
    return 1
  fi
  say "tarball source failed — retrying via git …"
  "$@" "$PIP_SRC_GIT" || return 1
  INSTALL_SOURCE="$PIP_SRC_GIT"
}

# Both sources failed: say what is left on the machine and how to get
# back to a working cbcl. The tool's own error text is already above.
# pip leaves an existing install in place when an install over it fails;
# pipx can remove it when the install fails part-way (`pipx install
# --force` deletes the old venv), so this reports what is really there.
install_failed() {
  local now stop=""
  now="$(installed_version)"
  detect_service
  if stop_before_start; then
    stop="$(stop_command "$CBCL")"
  fi
  warn "cbcl could not be installed from ref '$INSTALL_REF' (the errors are above)."
  if [[ -n "$now" ]]; then
    warn "cbcl $now is still installed: the previous install was not replaced."
    if [[ -f "$CUBICLE_HOME/config.yaml" ]]; then
      warn "To keep serving on cbcl $now now:"
      if [[ -n "$stop" ]]; then
        warn "  $stop"
      fi
      warn "  $(start_command "$CBCL")"
      warn "  $CBCL maintenance disable     # only if you paused before upgrading"
    fi
    warn "To upgrade: fix the cause (network access to github.com, the --ref name), then re-run this installer."
  elif [[ -n "$PREVIOUS_VERSION" ]]; then
    warn "cbcl $PREVIOUS_VERSION was installed before this run and is NOT installed now."
    warn "To recover: fix the cause (network access to github.com, the --ref name), then re-run this installer."
    warn "  With '--ref v$PREVIOUS_VERSION' it installs the previous version again."
    if [[ -f "$CUBICLE_HOME/config.yaml" ]]; then
      warn "Then: ${stop:+$stop, }$(start_command "$CBCL"), and $CBCL maintenance disable if you paused before upgrading."
    fi
  else
    warn "cbcl was not installed before this run and is not installed now."
    warn "To recover: fix the cause (network access to github.com, the --ref name), then re-run this installer."
  fi
  die "Install failed."
}

# pip skips a URL requirement whose version is already installed, so a
# re-run of the same version would change nothing while reporting
# success. pip_install <command…> therefore runs an upgrade install and,
# when that left the same version in place, reinstalls the package
# itself. Its dependencies are not reinstalled: the same version needs
# the same ones, and the upgrade install has just checked them.
pip_install() {
  try_sources "$@" --upgrade --quiet || return 1
  if [[ -n "$PREVIOUS_VERSION" && "$(installed_version)" == "$PREVIOUS_VERSION" ]]; then
    say "cbcl $PREVIOUS_VERSION is already installed — reinstalling its files …"
    try_sources "$@" --force-reinstall --no-deps --quiet || return 1
  fi
}

case "$INSTALL_KIND" in
  venv)
    CBCL="$INSTALL_VENV/bin/cbcl"
    say "Creating venv at $INSTALL_VENV …"
    "$PYTHON_BIN" -m venv "$INSTALL_VENV"
    say "Installing $PACKAGE into venv from ref '$INSTALL_REF' (tarball) …"
    pip_install "$INSTALL_VENV/bin/pip" install || install_failed
    ;;
  pipx)
    CBCL="cbcl"
    say "Installing via pipx from ref '$INSTALL_REF' (tarball) …"
    # --force always reinstalls, so a same-version re-run is real here.
    try_sources pipx install --force || install_failed
    # pipx installs binaries to ~/.local/bin but DOES NOT export the
    # dir to PATH for the current shell. On a fresh Ubuntu box that
    # dir often doesn't exist before pipx creates it, so Bash's
    # ``.profile`` PATH addition (which is guarded by an
    # ``if [ -d "$HOME/.local/bin" ]``) doesn't fire either.
    # ``pipx ensurepath`` writes the export to ~/.bashrc / ~/.profile
    # for FUTURE shells; for the CURRENT shell the caller has to
    # source the file or export PATH inline.
    say "Running 'pipx ensurepath' so future shells find cbcl …"
    pipx ensurepath --force >/dev/null 2>&1 || true
    ;;
  *)
    CBCL="cbcl"
    say "Installing user-site from ref '$INSTALL_REF' (tarball) …"
    PIP_ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/cbcl-pip.XXXXXX")"
    trap 'rm -f "$PIP_ERR_FILE"' EXIT
    user_site_pip() { "$PYTHON_BIN" -m pip install --user "$@" 2>>"$PIP_ERR_FILE"; }
    if ! pip_install user_site_pip; then
      if externally_managed; then
        warn "Your distro forbids global pip installs (PEP 668)."
        warn "Easiest fix: install pipx, then re-run this script."
        echo
        echo "  Ubuntu / Debian:   sudo apt install pipx"
        echo "  macOS (Homebrew):  brew install pipx"
        echo "  Other:             $PYTHON_BIN -m pip install --user pipx"
        echo
        warn "Alternatively, re-run with --venv:  install-cbcl.sh --venv ~/cbcl-venv"
        exit 1
      fi
      cat "$PIP_ERR_FILE" >&2
      install_failed
    fi
    ;;
esac

NEW_VERSION="$(installed_version)"
if [[ -z "$PREVIOUS_VERSION" ]]; then
  say "Installed cbcl ${NEW_VERSION:-(version unknown)}."
elif [[ "$NEW_VERSION" == "$PREVIOUS_VERSION" ]]; then
  say "Reinstalled cbcl $NEW_VERSION (the same version as before)."
else
  say "Installed cbcl ${NEW_VERSION:-(version unknown)}; it replaces cbcl $PREVIOUS_VERSION."
fi

# pipx and `pip --user` put the entry point in ~/.local/bin but do not
# put that directory on PATH for the current shell. We can't fix the
# parent shell's PATH from here (curl ... | bash runs in a sub-shell
# that exits), so the best we can do is print the exact unblock command.
if [[ "$CBCL" == "cbcl" && -x "$USER_BIN/cbcl" ]] && ! command -v cbcl >/dev/null 2>&1; then
  warn "cbcl is installed at ~/.local/bin/cbcl but THAT DIR IS NOT ON YOUR PATH for the current shell."
  echo
  echo "  Quickest fix for THIS shell:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo
  if [[ "$INSTALL_KIND" == "pipx" ]]; then
    echo "  For all future shells (already done by pipx ensurepath):"
    echo "    open a new SSH session OR run: exec \$SHELL -l"
  else
    echo "  Permanent fix (in ~/.bashrc or ~/.profile):"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
  echo
elif [[ "$INSTALL_KIND" == "venv" ]]; then
  echo
  echo "  Run cbcl as $CBCL, or put the venv on PATH:"
  echo "    export PATH=\"$INSTALL_VENV/bin:\$PATH\""
  echo
fi

# ── next steps ────────────────────────────────────────────────────
# next_step <command> <what it is for>: the next numbered line of the steps.
STEP=0
next_step() {
  STEP=$((STEP + 1))
  printf '  %s. %-28s # %s\n' "$STEP" "$1" "$2"
}

if [[ -f "$CUBICLE_HOME/config.yaml" ]]; then
  # Already paired: this was an upgrade or a reinstall. The pairing
  # steps would be noise, and the step people forget is the last one.
  say "Done. This machine is already paired, so nothing needs setting up again. Next:"
  echo
  # Something to stop first (see stop_before_start) gets its own step.
  detect_service
  STOP_FOR=""
  START_STEP=1
  if stop_before_start; then
    START_STEP=2
    STILL_RUNNING_PID="$(running_cbcl_pid)"
    if [[ -z "$STILL_RUNNING_PID" ]]; then
      echo "  The launchd job $LAUNCHD_LABEL is still loaded, so step 1 unloads it: launchctl load fails on a loaded job."
      STOP_FOR="unload the job"
    else
      echo "  cbcl is still running (PID $STILL_RUNNING_PID) and keeps the code it started with, so step 1 stops it."
      STOP_FOR="stop the cbcl still running"
      if [[ -n "$CBCL_SERVICE" && $CBCL_SERVICE_RUNS -eq 0 ]]; then
        STOP_FOR="it was started by hand, outside the service"
      fi
    fi
  fi
  if [[ -n "$CBCL_SERVICE" ]]; then
    echo "  cbcl is set up as $(service_name), so step $START_STEP starts it through $CBCL_SERVICE."
  fi
  if [[ -n "$STOP_FOR$CBCL_SERVICE" ]]; then
    echo
  fi
  if [[ -n "$STOP_FOR" ]]; then
    next_step "$(stop_command "$CBCL")" "$STOP_FOR"
  fi
  next_step "$(start_command "$CBCL")" "start cbcl"
  next_step "$CBCL status" "each office should come up; a maintenance pause is shown here"
  next_step "$CBCL maintenance status" "\"enabled\": true means that office is still paused"
  next_step "$CBCL maintenance disable" "reopen, once the offices look right"
  echo
  echo "  If you paused before upgrading, offices start NO new work until step $STEP."
  echo
else
  say "Done. Pair this machine with your Cubicle company:"
  echo
  echo "  1. Go to Company Settings → Tokens in the Cubicle UI."
  echo "  2. Mint a token (or copy your existing one)."
  echo "  3. Run '$CBCL setup' and paste the token when prompted."
  echo "  4. In Office Settings → Connection, assign each office to this token."
  echo "  5. Run '$CBCL start' to bring the offices online."
  echo
fi
