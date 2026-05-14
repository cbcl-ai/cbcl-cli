#!/usr/bin/env bash
# install-cbcl.sh — one-liner installer for the Cubicle Communicator (cbcl).
#
# Usage on any Linux / macOS box with Python 3.12+ and Docker:
#
#   curl -sSL https://gitlab.com/cubicle1/v2/cubicle/-/raw/main/install-cbcl.sh | bash
#
# What it does:
#   1. Verifies Python 3.12+ is on PATH (and offers the next step if not).
#   2. Verifies Docker daemon is reachable.
#   3. ``pip install``s the ``cubicle-communicator`` package directly
#      from the Git repo into ~/.local (or a venv if requested).
#   4. Prints next steps (``cbcl setup`` to pair with the platform).
#
# Flags:
#   --venv <path>     Install into a fresh venv at <path> (recommended).
#   --ref <git-ref>   Install from a specific tag/branch (default: main).
#   --uninstall       Remove the cbcl install (keeps ~/.cubicle/ data).
#
# Safe to re-run: pip upgrades in place. Configuration in ~/.cubicle/
# is NEVER touched by this script.

set -euo pipefail

REPO_URL="https://github.com/cbcl-ai/cbcl-cli.git"
DEFAULT_REF="main"
INSTALL_VENV=""
INSTALL_REF="$DEFAULT_REF"
UNINSTALL=0

# ── arg parse ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv)
      INSTALL_VENV="$2"
      shift 2
      ;;
    --ref)
      INSTALL_REF="$2"
      shift 2
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
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

# ── uninstall ──────────────────────────────────────────────────────
if [[ $UNINSTALL -eq 1 ]]; then
  say "Uninstalling cbcl…"
  if command -v cbcl >/dev/null 2>&1; then
    pip uninstall -y cubicle-communicator 2>/dev/null || \
      pipx uninstall cubicle-communicator 2>/dev/null || true
  fi
  say "Done. Your data under ~/.cubicle/ was left untouched."
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

# ── precheck: git (needed for VCS-pip install) ────────────────────
if ! command -v git >/dev/null 2>&1; then
  die "git not found on PATH (needed to clone the package source). Install git and re-run."
fi

# ── install ───────────────────────────────────────────────────────
PIP_SRC="git+${REPO_URL}@${INSTALL_REF}"

if [[ -n "$INSTALL_VENV" ]]; then
  say "Creating venv at $INSTALL_VENV …"
  "$PYTHON_BIN" -m venv "$INSTALL_VENV"
  PIP_BIN="$INSTALL_VENV/bin/pip"
  CBCL_BIN="$INSTALL_VENV/bin/cbcl"
  say "Installing cubicle-communicator into venv from ref '$INSTALL_REF' …"
  "$PIP_BIN" install --upgrade --quiet "$PIP_SRC"
  say "Installed. Run:"
  echo
  echo "    $CBCL_BIN setup"
  echo "    $CBCL_BIN start"
  echo
  echo "Or add the venv to PATH:  export PATH=\"$INSTALL_VENV/bin:\$PATH\""
else
  # User-site install (no sudo, no venv). Best-effort fall back to
  # pipx if available, since some distros refuse direct ``pip install``
  # with PEP 668 (externally-managed environment).
  if command -v pipx >/dev/null 2>&1; then
    say "Installing via pipx from ref '$INSTALL_REF' …"
    pipx install --force "$PIP_SRC"
    say "Installed. pipx put 'cbcl' on your PATH. Run:"
    echo
    echo "    cbcl setup"
    echo "    cbcl start"
    echo
  else
    say "Installing user-site from ref '$INSTALL_REF' …"
    if ! "$PYTHON_BIN" -m pip install --user --upgrade --quiet "$PIP_SRC" 2>/tmp/cbcl-pip.err; then
      if grep -q "externally-managed-environment" /tmp/cbcl-pip.err 2>/dev/null; then
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
      cat /tmp/cbcl-pip.err >&2
      die "pip install failed."
    fi
    say "Installed. Make sure ~/.local/bin is on your PATH, then run:"
    echo
    echo "    cbcl setup"
    echo "    cbcl start"
    echo
  fi
fi

say "Done. Pair this machine with your Cubicle company:"
echo
echo "  1. Go to Company Settings → Tokens in the Cubicle UI."
echo "  2. Mint a token (or copy your existing one)."
echo "  3. Run 'cbcl setup' and paste the token when prompted."
echo "  4. In Office Settings → Connection, assign each office to this token."
echo "  5. Run 'cbcl start' to bring the offices online."
echo
