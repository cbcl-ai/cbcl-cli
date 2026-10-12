"""The installer script, run for real against stub tools.

``install-cbcl.sh`` is what an operator pipes into bash on a machine that
may be serving offices, so its refusals, fallbacks and messages are
behaviour, not decoration. Each test runs the real script with a private
HOME and a PATH that holds only stub ``python3`` / ``pip`` / ``pipx`` /
``docker`` / ``git`` programs (which log how they were called and under
which umask) plus the handful of system tools the script uses.

Set ``CBCL_INSTALL_SCRIPT`` to run the same tests against another copy, for
example the public ``install.sh`` before it is republished
(``communicator/UPSTREAM.md``). Without it the tests cover the monorepo's
``install-cbcl.sh`` and are skipped where that file is not shipped (the
public mirror).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(
    os.environ.get("CBCL_INSTALL_SCRIPT")
    or Path(__file__).resolve().parents[1] / "install-cbcl.sh"
)
BASH = shutil.which("bash")
# Everything the script (and the stubs) may call besides the stubs.
SYSTEM_TOOLS = (
    "awk", "basename", "cat", "chmod", "cp", "grep", "head", "mkdir",
    "mktemp", "ps", "rm", "sleep", "tr",
)
# The public install reference; absent on the public mirror.
INSTALL_PAGE = (
    Path(__file__).resolve().parents[2]
    / "landing_v2" / "content" / "docs" / "cbcl" / "install.mdx"
)
TARBALL = "https://github.com/cbcl-ai/cbcl-cli/archive/{ref}.tar.gz"
GIT = "git+https://github.com/cbcl-ai/cbcl-cli.git@{ref}"

pytestmark = pytest.mark.skipif(
    BASH is None or not SCRIPT.is_file(),
    reason="needs bash and the installer script (set CBCL_INSTALL_SCRIPT for another copy)",
)

PIP_IMPL = r"""#!@BASH@
# pip-impl <scope> <pip arguments…>: one fake site per scope.
scope="$1"; shift
printf 'pip[%s] umask=%s %s\n' "$scope" "$(umask)" "$*" >> "$STUB_LOG"
state="$STUB_STATE/$scope.version"
case "$1" in
  show)
    [[ -f "$state" ]] || exit 1
    printf 'Name: cubicle-communicator\nVersion: %s\n' "$(cat "$state")"
    ;;
  uninstall)
    # Like the real pip: exit 0 even when the package is not installed.
    rm -f "$state"
    if [[ "$scope" == site && -z "${STUB_KEEP_ENTRY:-}" ]]; then
      rm -f "$STUB_HOME/.local/bin/cbcl"
    fi
    ;;
  install)
    source="${!#}"
    case "${STUB_PIP_FAIL:-}" in
      pep668) echo "error: externally-managed-environment" >&2; exit 1 ;;
      all) echo "ERROR: stub pip cannot install $source" >&2; exit 1 ;;
      tarball)
        if [[ "$source" == *archive* ]]; then
          echo "ERROR: stub pip cannot install $source" >&2; exit 1
        fi ;;
    esac
    echo "${STUB_NEW_VERSION:-9.9.9}" > "$state"
    ;;
esac
exit 0
"""

PYTHON = r"""#!@BASH@
# The interpreter the installer finds on PATH.
case "$1" in
  -c) exit 0 ;;
  --version) echo "Python 3.12.9"; exit 0 ;;
esac
if [[ "$1 $2" == "-m venv" ]]; then
  printf 'python umask=%s %s\n' "$(umask)" "$*" >> "$STUB_LOG"
  mkdir -p "$3/bin"
  cp "$STUB_BIN/venv-python" "$3/bin/python"
  cp "$STUB_BIN/venv-pip" "$3/bin/pip"
  exit 0
fi
if [[ "$1 $2" == "-m pip" ]]; then shift 2; exec "$STUB_BIN/pip-impl" site "$@"; fi
exit 0
"""

VENV_PYTHON = r"""#!@BASH@
if [[ "$1 $2" == "-m pip" ]]; then shift 2; exec "$STUB_BIN/pip-impl" venv "$@"; fi
exit 0
"""

VENV_PIP = r"""#!@BASH@
exec "$STUB_BIN/pip-impl" venv "$@"
"""

PIPX = r"""#!@BASH@
printf 'pipx umask=%s %s\n' "$(umask)" "$*" >> "$STUB_LOG"
state="$STUB_STATE/pipx.version"
entry="$STUB_HOME/.local/bin/cbcl"
case "$1" in
  list)
    if [[ -f "$state" ]]; then echo "cubicle-communicator $(cat "$state")"; fi
    ;;
  uninstall)
    rm -f "$state"
    if [[ -z "${STUB_KEEP_ENTRY:-}" ]]; then rm -f "$entry"; fi
    ;;
  install)
    source="${!#}"
    fail="${STUB_PIPX_FAIL:-}"
    if [[ "$fail" == all || ( "$fail" == tarball && "$source" == *archive* ) ]]; then
      # pipx keeps an existing install when the source cannot be fetched,
      # but removes it when the install fails part-way (`install --force`
      # deletes the old venv). STUB_PIPX_REMOVES_OLD models the second
      # case, to check that the script reports what is really left.
      if [[ -n "${STUB_PIPX_REMOVES_OLD:-}" ]]; then rm -f "$state" "$entry"; fi
      echo "pipx: stub cannot install $source" >&2
      exit 1
    fi
    echo "${STUB_NEW_VERSION:-9.9.9}" > "$state"
    mkdir -p "$STUB_HOME/.local/bin"
    printf '#!%s\n' "$STUB_BIN/python3" > "$entry"
    chmod +x "$entry"
    ;;
esac
exit 0
"""

NOOP = "#!@BASH@\nexit 0\n"

SYSTEMCTL = r"""#!@BASH@
# systemctl is-enabled|is-active --quiet <unit>: cbcl.service is in the
# states listed in STUB_SYSTEMD ("enabled", "active"); other units are not.
printf 'systemctl %s\n' "$*" >> "$STUB_LOG"
[[ "${!#}" == cbcl.service ]] || exit 1
case "$1" in
  is-enabled) [[ " ${STUB_SYSTEMD:-} " == *" enabled "* ]] ;;
  is-active) [[ " ${STUB_SYSTEMD:-} " == *" active "* ]] ;;
  *) exit 1 ;;
esac
"""

LAUNCHCTL = r"""#!@BASH@
# launchctl list <label> succeeds while the job is loaded (STUB_LAUNCHD_LOADED).
printf 'launchctl %s\n' "$*" >> "$STUB_LOG"
if [[ "$1" == list ]]; then [[ -n "${STUB_LAUNCHD_LOADED:-}" ]]; fi
"""

LAUNCHD_PLIST = "~/Library/LaunchAgents/ai.cbcl.communicator.plist"
# A system service is stopped and started with sudo, unless the script runs as root.
SUDO = "" if os.geteuid() == 0 else "sudo "


class Machine:
    """A private HOME and PATH for one run of the installer."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.bin = root / "bin"
        self.state = root / "state"
        self.log_file = root / "calls.log"
        for directory in (self.home, self.bin, self.state):
            directory.mkdir()
        self.log_file.touch()
        for name in SYSTEM_TOOLS:
            real = shutil.which(name)
            assert real, f"this test needs {name} on PATH"
            (self.bin / name).symlink_to(real)
        self._stub("pip-impl", PIP_IMPL)
        for name in ("python3", "python3.12", "python3.13"):
            self._stub(name, PYTHON)
        self._stub("venv-python", VENV_PYTHON)
        self._stub("venv-pip", VENV_PIP)
        self._stub("docker", NOOP)
        self._stub("git", NOOP)
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "TMPDIR": str(root),
            "STUB_BIN": str(self.bin),
            "STUB_HOME": str(self.home),
            "STUB_LOG": str(self.log_file),
            "STUB_STATE": str(self.state),
        }

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body.replace("@BASH@", BASH))
        path.chmod(0o755)

    def with_pipx(self) -> "Machine":
        self._stub("pipx", PIPX)
        return self

    def with_systemd(self, *states: str) -> "Machine":
        """A systemctl that reports cbcl.service as ``states`` ("enabled", "active")."""
        self._stub("systemctl", SYSTEMCTL)
        self.env["STUB_SYSTEMD"] = " ".join(states)
        return self

    def with_launchd_job(self, *, loaded: bool = True) -> "Machine":
        """A launchctl and the plist the service docs have Mac users create,
        with the job ``loaded`` (it runs cbcl) or not."""
        self._stub("launchctl", LAUNCHCTL)
        if loaded:
            self.env["STUB_LAUNCHD_LOADED"] = "1"
        plist = self.home / LAUNCHD_PLIST.removeprefix("~/")
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist/>\n")
        return self

    def installed(self, scope: str, version: str) -> "Machine":
        """Pretend cbcl ``version`` is installed via pipx, the user site or a venv."""
        (self.state / f"{scope}.version").write_text(version + "\n")
        return self

    def is_installed(self, scope: str) -> bool:
        return (self.state / f"{scope}.version").exists()

    def paired(self) -> "Machine":
        (self.home / ".cubicle").mkdir(exist_ok=True)
        (self.home / ".cubicle" / "config.yaml").write_text("platform_url: https://x\n")
        return self

    def pid_file(self, content: str) -> "Machine":
        (self.home / ".cubicle").mkdir(exist_ok=True)
        (self.home / ".cubicle" / "communicator.pid").write_text(content)
        return self

    def user_site_entry_point(self) -> Path:
        entry = self.home / ".local" / "bin" / "cbcl"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(f"#!{self.bin / 'python3'}\n")
        entry.chmod(0o755)
        return entry

    def run(self, *args: str, stdin=None, umask: int | None = None, **env: str):
        environment = {**self.env, **env}
        for name in [key for key, value in environment.items() if value is None]:
            del environment[name]
        command = [BASH, "-s", "--", *args] if stdin is not None else [BASH, str(SCRIPT), *args]
        return subprocess.run(
            command,
            env=environment,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            preexec_fn=(lambda: os.umask(umask)) if umask is not None else None,
        )

    def calls(self, tool: str = "") -> list[str]:
        """Logged stub calls, optionally only those of one tool."""
        return [
            line for line in self.log_file.read_text().splitlines()
            if line.startswith(tool)
        ]

    def installs(self) -> list[str]:
        """Every install attempt, in order (pip and pipx)."""
        return [line for line in self.calls() if " install " in f"{line} "]


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return Machine(tmp_path)


@pytest.fixture
def running_cbcl(tmp_path: Path):
    """A live process whose command line is a cbcl daemon's (`…/cbcl start`)."""
    script = tmp_path / "fake-install" / "cbcl"
    script.parent.mkdir()
    script.write_text("while true; do sleep 1; done\n")
    process = subprocess.Popen([shutil.which("sh"), str(script), "start"])
    yield process.pid
    process.kill()
    process.wait()


@pytest.fixture
def another_program():
    """A live process that is not cbcl: what a reused PID points at."""
    process = subprocess.Popen([shutil.which("sleep"), "300"])
    yield process.pid
    process.kill()
    process.wait()


def warning_lines(result: subprocess.CompletedProcess) -> list[str]:
    """The script's stderr lines, without the ``!!!`` prefix of its warnings."""
    return [line.split("!!!", 1)[-1].strip() for line in result.stderr.splitlines()]


# ── --help and argument errors ──────────────────────────────────────


def test_help_prints_the_usage(machine: Machine) -> None:
    result = machine.run("--help")
    assert result.returncode == 0, result.stderr
    for expected in ("--venv <path>", "--ref <git-ref>", "--uninstall", "--force", "umask"):
        assert expected in result.stdout
    assert machine.calls() == [], "help installs nothing"


def test_help_works_when_the_script_is_piped_into_bash(machine: Machine) -> None:
    """The documented form: curl … | bash -s -- --help. ``$0`` is then "bash"."""
    with SCRIPT.open() as script:
        result = machine.run("--help", stdin=script)
    assert result.returncode == 0, result.stderr
    assert "--venv <path>" in result.stdout
    assert result.stderr == ""


def test_every_flag_has_a_row_on_the_public_install_page(machine: Machine) -> None:
    """The public install reference is deployed separately from the installer
    (``communicator/UPSTREAM.md``), so a flag added here is easily left out
    there. Every flag in the usage needs a row in that page's flag table."""
    if not INSTALL_PAGE.is_file():
        pytest.skip("landing docs not present (standalone communicator checkout)")
    usage = machine.run("--help").stdout
    flags = set(re.findall(r"^  (?:-h, )?(--[a-z-]+)", usage, flags=re.MULTILINE))
    assert {"--venv", "--ref", "--uninstall", "--force", "--help"} <= flags

    rows = set(
        re.findall(
            r"^\| `(?:-h`, `)?(--[a-z-]+)",
            INSTALL_PAGE.read_text(),
            flags=re.MULTILINE,
        )
    )
    assert flags <= rows, f"no row on the install page for: {sorted(flags - rows)}"


def test_unknown_flag_is_refused_with_the_usage(machine: Machine) -> None:
    result = machine.run("--bogus")
    assert result.returncode == 2
    assert "Unknown flag: --bogus" in result.stderr
    assert "--venv <path>" in result.stderr
    assert machine.calls() == []


@pytest.mark.parametrize(
    "arguments", [["--venv"], ["--ref"], ["--venv", "--force"], ["--ref", ""]],
)
def test_flag_without_its_value_is_refused_with_the_usage(machine: Machine, arguments) -> None:
    result = machine.run(*arguments)
    assert result.returncode == 2
    assert f"Missing value for {arguments[0]}." in result.stderr
    assert "--venv <path>" in result.stderr
    assert "unbound variable" not in result.stderr
    assert machine.calls() == []


# ── the running-daemon check ────────────────────────────────────────


@pytest.mark.parametrize("arguments", [[], ["--uninstall"], ["--venv", "{venv}"]])
def test_refuses_to_change_the_install_under_a_running_cbcl(
    machine: Machine, running_cbcl: int, tmp_path: Path, arguments,
) -> None:
    machine.with_pipx().installed("pipx", "1.0.0").pid_file(f"{running_cbcl}\n")
    result = machine.run(*(argument.format(venv=tmp_path / "venv") for argument in arguments))
    assert result.returncode == 1
    assert f"cbcl is running (PID {running_cbcl})" in result.stderr
    assert "cbcl maintenance enable --wait 300" in result.stderr
    assert "Refusing to change the install" in result.stderr
    assert machine.installs() == []
    assert machine.calls("pipx umask=0022 uninstall") == []
    assert machine.is_installed("pipx")


def test_refusal_without_a_service_names_the_cbcl_commands(
    machine: Machine, running_cbcl: int,
) -> None:
    """A systemctl that has no cbcl.service is not a reason to use it."""
    machine.with_pipx().with_systemd().pid_file(str(running_cbcl))
    result = machine.run()
    assert result.returncode == 1
    assert "cbcl stop" in warning_lines(result)
    assert "start with 'cbcl start -d'" in result.stderr
    assert "systemctl" not in result.stderr


@pytest.mark.parametrize(
    ("service", "stop", "start"),
    [
        ("systemd", f"{SUDO}systemctl stop cbcl", f"{SUDO}systemctl start cbcl"),
        ("launchd", f"launchctl unload {LAUNCHD_PLIST}", f"launchctl load {LAUNCHD_PLIST}"),
    ],
    ids=["systemd", "launchd"],
)
def test_refusal_stops_and_starts_a_service_through_its_service_manager(
    machine: Machine, running_cbcl: int, service: str, stop: str, start: str,
) -> None:
    """A hand-run `cbcl start -d` runs outside the service, and a forced
    `cbcl stop` is a failure that the service manager answers by starting
    cbcl again."""
    if service == "systemd":
        machine.with_systemd("enabled", "active")
    else:
        machine.with_launchd_job()
    machine.with_pipx().pid_file(str(running_cbcl))
    result = machine.run()
    assert result.returncode == 1
    warnings = warning_lines(result)
    assert stop in warnings
    assert "cbcl stop" not in warnings
    assert f"start with '{start}'" in result.stderr
    assert "cbcl start -d" not in result.stderr
    assert "started by hand" not in result.stderr
    assert machine.installs() == []


@pytest.mark.parametrize(
    ("service", "name", "start"),
    [
        ("systemd", "the systemd service cbcl.service", f"{SUDO}systemctl start cbcl"),
        ("launchd", "the launchd job ai.cbcl.communicator", f"launchctl load {LAUNCHD_PLIST}"),
    ],
    ids=["systemd", "launchd"],
)
def test_refusal_stops_a_cbcl_started_by_hand_next_to_its_service_by_hand(
    machine: Machine, running_cbcl: int, service: str, name: str, start: str,
) -> None:
    """The service manager cannot stop a cbcl it did not start (the old
    upgrade steps started it by hand); it starts the new one."""
    if service == "systemd":
        machine.with_systemd("enabled")
    else:
        machine.with_launchd_job(loaded=False)
    machine.with_pipx().pid_file(str(running_cbcl))
    result = machine.run()
    assert result.returncode == 1
    assert f"It runs outside {name} (it was started by hand)" in result.stderr
    assert "cbcl stop" in warning_lines(result)
    assert f"start with '{start}'" in result.stderr


def test_uninstall_refusal_names_nothing_to_start(machine: Machine, running_cbcl: int) -> None:
    machine.with_pipx().installed("pipx", "1.0.0").pid_file(str(running_cbcl))
    result = machine.run("--uninstall")
    assert result.returncode == 1
    assert "Removing it takes away files its workers are using" in result.stderr
    assert "Then re-run this installer." in warning_lines(result)
    assert "Installing over it" not in result.stderr
    assert "start with" not in result.stderr
    assert machine.is_installed("pipx")


def test_force_skips_the_running_daemon_check(machine: Machine, running_cbcl: int) -> None:
    machine.with_pipx().pid_file(str(running_cbcl))
    result = machine.run("--force")
    assert result.returncode == 0, result.stderr
    assert len(machine.installs()) == 1


@pytest.mark.parametrize("content", ["99999999\n", "not-a-pid\n", ""])
def test_dead_or_garbage_pid_file_does_not_block_the_install(machine: Machine, content) -> None:
    machine.with_pipx().pid_file(content)
    result = machine.run()
    assert result.returncode == 0, result.stderr
    assert "Refusing" not in result.stderr
    assert len(machine.installs()) == 1


def test_pid_reused_by_another_program_is_a_stale_file(
    machine: Machine, another_program: int,
) -> None:
    """The PID file outlives a killed daemon; the PID alone proves nothing."""
    machine.with_pipx().pid_file(str(another_program))
    result = machine.run()
    assert result.returncode == 0, result.stderr
    assert "Stale PID file" in result.stderr
    assert f"PID {another_program}" in result.stderr
    assert len(machine.installs()) == 1


def test_unset_home_does_not_abort_the_script(machine: Machine, tmp_path: Path) -> None:
    """`set -u` used to stop at "$HOME". --force: the real home is not ours to judge."""
    result = machine.run("--force", "--venv", str(tmp_path / "venv"), HOME=None)
    assert "unbound variable" not in result.stderr
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", [
    "/root/.local/share/pipx/venvs/cubicle-communicator/bin/python /root/.local/bin/cbcl start",
    "/opt/cbcl/bin/python /opt/cbcl/bin/cbcl start --daemon",
    "/opt/cbcl/bin/python -m src._daemon_entry",
    "/Users/A B/venv/bin/python3 /Users/A B/.local/bin/cbcl start",
    "python ./cbcl start",
    "/usr/sbin/sshd -D",
    "/opt/cbcl/bin/python /opt/cbcl/bin/cbcl status",
    "/opt/cbcl/bin/python /opt/cbcl/bin/cbcl maintenance enable --wait 300",
    "grep -r start /root/.local/bin/cbcl",
    "/opt/x/bin/python /opt/x/bin/cbcl-helper start",
    "/opt/cbcl/bin/python -m src.agent_worker --role worker",
])
def test_the_script_and_cbcl_agree_on_what_a_daemon_looks_like(command: str) -> None:
    """The pattern exists twice, as a bash and as a Python regular expression.

    The script must refuse exactly the processes that `cbcl start`, `stop`
    and `status` take for the daemon (``daemon._DAEMON_COMMAND``).
    """
    daemon = pytest.importorskip("src.daemon")
    declared = re.search(r"^DAEMON_COMMAND_RE='(.*)'$", SCRIPT.read_text(), re.MULTILINE)
    if declared is None:
        pytest.skip("this copy of the installer has no daemon identity check")
    bash_matches = subprocess.run(
        [BASH, "-c", '[[ "$1" =~ $2 ]]', "match", command, declared.group(1)],
        check=False,
    ).returncode == 0
    assert bash_matches == (daemon._DAEMON_COMMAND.search(command) is not None)


# ── umask ───────────────────────────────────────────────────────────


def test_installs_with_umask_022_whatever_the_caller_uses(machine: Machine) -> None:
    machine.with_pipx()
    result = machine.run(umask=0o077)
    assert result.returncode == 0, result.stderr
    assert "Your shell's umask is 0077; this installer uses 022" in result.stderr
    assert machine.calls(), "the stubs were called"
    assert all(" umask=0022 " in line for line in machine.calls())


def test_usual_umask_is_not_commented_on(machine: Machine) -> None:
    machine.with_pipx()
    result = machine.run(umask=0o022)
    assert result.returncode == 0, result.stderr
    assert "umask" not in result.stderr


# ── sources ─────────────────────────────────────────────────────────


def test_tarball_is_tried_before_git(machine: Machine) -> None:
    machine.with_pipx()
    result = machine.run(STUB_PIPX_FAIL="tarball")
    assert result.returncode == 0, result.stderr
    assert "retrying via git" in result.stdout
    assert machine.installs() == [
        f"pipx umask=0022 install --force {TARBALL.format(ref='main')}",
        f"pipx umask=0022 install --force {GIT.format(ref='main')}",
    ]


def test_ref_selects_the_tag_in_both_sources(machine: Machine, tmp_path: Path) -> None:
    result = machine.run("--ref", "v1.2.3", "--venv", str(tmp_path / "venv"), STUB_PIP_FAIL="tarball")
    assert result.returncode == 0, result.stderr
    assert [line.split()[-1] for line in machine.installs()] == [
        TARBALL.format(ref="v1.2.3"), GIT.format(ref="v1.2.3"),
    ]


def test_externally_managed_python_gets_the_pipx_guidance_not_a_git_retry(
    machine: Machine,
) -> None:
    result = machine.run(STUB_PIP_FAIL="pep668")
    assert result.returncode == 1
    assert "PEP 668" in result.stderr
    assert "sudo apt install pipx" in result.stdout
    assert "--venv" in result.stderr
    assert len(machine.installs()) == 1, "another source cannot fix the interpreter"
    assert "retrying via git" not in result.stdout


# ── a failed install says what is left and how to recover ───────────


def test_failed_pipx_install_reports_the_surviving_version(machine: Machine) -> None:
    machine.with_pipx().installed("pipx", "1.0.0").paired()
    result = machine.run(STUB_PIPX_FAIL="all")
    assert result.returncode == 1
    assert len(machine.installs()) == 2
    assert "cbcl 1.0.0 is still installed" in result.stderr
    assert "To keep serving on cbcl 1.0.0 now:" in result.stderr
    assert "cbcl start -d" in result.stderr
    assert "cbcl maintenance disable" in result.stderr
    assert "re-run this installer" in result.stderr
    assert "--ref v1.0.0" not in result.stderr, "the previous version is still there"
    assert "Install failed." in result.stderr
    assert "Done." not in result.stdout


def test_failed_install_on_an_unpaired_machine_names_nothing_to_start(machine: Machine) -> None:
    machine.with_pipx().installed("pipx", "1.0.0")
    result = machine.run(STUB_PIPX_FAIL="all")
    assert result.returncode == 1
    assert "cbcl 1.0.0 is still installed" in result.stderr
    assert "cbcl start -d" not in result.stderr


def test_failed_install_that_lost_the_old_version_names_it(machine: Machine) -> None:
    """The report is what is on the machine, not what the tool should leave."""
    machine.with_pipx().installed("pipx", "1.0.0").paired()
    result = machine.run(STUB_PIPX_FAIL="all", STUB_PIPX_REMOVES_OLD="1")
    assert result.returncode == 1
    assert "cbcl 1.0.0 was installed before this run and is NOT installed now" in result.stderr
    assert "--ref v1.0.0" in result.stderr
    assert "To keep serving" not in result.stderr
    assert "Then: cbcl start -d" in result.stderr


def test_failed_install_starts_a_service_through_its_service_manager(machine: Machine) -> None:
    machine.with_pipx().with_systemd("enabled").installed("pipx", "1.0.0").paired()
    result = machine.run(STUB_PIPX_FAIL="all")
    assert result.returncode == 1
    assert "To keep serving on cbcl 1.0.0 now:" in result.stderr
    assert f"{SUDO}systemctl start cbcl" in warning_lines(result)
    assert "cbcl start -d" not in result.stderr


def test_failed_install_that_lost_the_old_version_starts_the_service(machine: Machine) -> None:
    machine.with_pipx().with_launchd_job(loaded=False).installed("pipx", "1.0.0").paired()
    result = machine.run(STUB_PIPX_FAIL="all", STUB_PIPX_REMOVES_OLD="1")
    assert result.returncode == 1
    assert f"Then: launchctl load {LAUNCHD_PLIST}, and cbcl maintenance disable" in result.stderr
    assert "cbcl start -d" not in result.stderr


def test_failed_install_unloads_a_launchd_job_that_is_still_loaded(machine: Machine) -> None:
    """`launchctl load` fails on a loaded job (`cbcl stop` leaves it loaded)."""
    machine.with_pipx().with_launchd_job().installed("pipx", "1.0.0").paired()
    result = machine.run(STUB_PIPX_FAIL="all")
    assert result.returncode == 1
    warnings = warning_lines(result)
    unload = f"launchctl unload {LAUNCHD_PLIST}"
    load = f"launchctl load {LAUNCHD_PLIST}"
    assert unload in warnings and load in warnings
    assert warnings.index(unload) < warnings.index(load)


def test_failed_install_that_lost_the_old_version_unloads_a_loaded_job(machine: Machine) -> None:
    machine.with_pipx().with_launchd_job().installed("pipx", "1.0.0").paired()
    result = machine.run(STUB_PIPX_FAIL="all", STUB_PIPX_REMOVES_OLD="1")
    assert result.returncode == 1
    assert (
        f"Then: launchctl unload {LAUNCHD_PLIST}, launchctl load {LAUNCHD_PLIST}, "
        "and cbcl maintenance disable"
    ) in result.stderr


@pytest.mark.parametrize("venv", [True, False], ids=["venv", "user-site"])
def test_failed_pip_install_ends_with_the_recovery_not_a_raw_error(
    machine: Machine, tmp_path: Path, venv: bool,
) -> None:
    arguments = ["--venv", str(tmp_path / "venv")] if venv else []
    result = machine.run(*arguments, STUB_PIP_FAIL="all")
    assert result.returncode == 1
    assert len(machine.installs()) == 2
    assert "ERROR: stub pip cannot install" in result.stderr, "the tool's own error stays visible"
    assert "cbcl was not installed before this run and is not installed now" in result.stderr
    assert "To recover:" in result.stderr
    assert "Install failed." in result.stderr


# ── re-running the same version really reinstalls ───────────────────


@pytest.mark.parametrize("venv", [True, False], ids=["venv", "user-site"])
def test_same_version_rerun_reinstalls_the_package(
    machine: Machine, tmp_path: Path, venv: bool,
) -> None:
    """pip skips a URL requirement whose version is already installed."""
    scope = "venv" if venv else "site"
    arguments = ["--venv", str(tmp_path / "venv")] if venv else []
    if venv:
        assert machine.run(*arguments, STUB_NEW_VERSION="1.0.0").returncode == 0
        machine.log_file.write_text("")
    else:
        machine.installed("site", "1.0.0")

    result = machine.run(*arguments, STUB_NEW_VERSION="1.0.0")

    assert result.returncode == 0, result.stderr
    installs = machine.installs()
    assert len(installs) == 2
    assert "--upgrade" in installs[0] and "--force-reinstall" not in installs[0]
    assert f"pip[{scope}]" in installs[1]
    assert "--force-reinstall --no-deps" in installs[1]
    assert installs[1].endswith(TARBALL.format(ref="main"))
    assert "Reinstalled cbcl 1.0.0" in result.stdout


def test_upgrade_does_not_reinstall_twice(machine: Machine) -> None:
    machine.installed("site", "1.0.0")
    result = machine.run(STUB_NEW_VERSION="2.0.0")
    assert result.returncode == 0, result.stderr
    assert len(machine.installs()) == 1
    assert "--force-reinstall" not in machine.installs()[0]
    assert "Installed cbcl 2.0.0; it replaces cbcl 1.0.0." in result.stdout


def test_reinstall_reuses_the_source_that_worked(machine: Machine) -> None:
    """A tarball that failed once is not tried again for the reinstall."""
    machine.installed("site", "1.0.0")
    result = machine.run(STUB_NEW_VERSION="1.0.0", STUB_PIP_FAIL="tarball")
    assert result.returncode == 0, result.stderr
    assert [line.split()[-1] for line in machine.installs()] == [
        TARBALL.format(ref="main"), GIT.format(ref="main"), GIT.format(ref="main"),
    ]
    assert "--force-reinstall --no-deps" in machine.installs()[2]
    assert result.stdout.count("retrying via git") == 1


def test_first_install_reports_the_installed_version(machine: Machine) -> None:
    result = machine.run(STUB_NEW_VERSION="2.0.0")
    assert result.returncode == 0, result.stderr
    assert machine.installs() == [
        f"pip[site] umask=0022 install --user --upgrade --quiet {TARBALL.format(ref='main')}",
    ]
    assert "Installed cbcl 2.0.0." in result.stdout


def test_pipx_rerun_always_reinstalls(machine: Machine) -> None:
    machine.with_pipx().installed("pipx", "1.0.0")
    result = machine.run(STUB_NEW_VERSION="1.0.0")
    assert result.returncode == 0, result.stderr
    assert machine.installs() == [
        f"pipx umask=0022 install --force {TARBALL.format(ref='main')}",
    ]
    assert "Reinstalled cbcl 1.0.0" in result.stdout


# ── next steps ──────────────────────────────────────────────────────


def test_new_machine_gets_the_pairing_steps(machine: Machine) -> None:
    machine.with_pipx()
    result = machine.run()
    assert result.returncode == 0, result.stderr
    assert "Pair this machine with your Cubicle company" in result.stdout
    assert "Run 'cbcl setup'" in result.stdout
    assert "maintenance disable" not in result.stdout
    assert "NOT ON YOUR PATH" in result.stderr, "~/.local/bin is not on this PATH"


def test_paired_machine_gets_the_upgrade_steps_not_the_pairing_steps(machine: Machine) -> None:
    """After an upgrade the step people forget is reopening the offices."""
    machine.with_pipx().installed("pipx", "1.0.0").paired()
    result = machine.run()
    assert result.returncode == 0, result.stderr
    for expected in (
        "already paired", "cbcl start -d", "cbcl status",
        "cbcl maintenance status", "cbcl maintenance disable",
        "offices start NO new work until step 4",
    ):
        assert expected in result.stdout
    assert "Mint a token" not in result.stdout
    assert "cbcl setup" not in result.stdout


def test_venv_install_names_the_venv_cbcl_in_the_next_steps(machine: Machine, tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    result = machine.paired().run("--venv", str(venv))
    assert result.returncode == 0, result.stderr
    assert f"{venv}/bin/cbcl start -d" in result.stdout
    assert f'export PATH="{venv}/bin:$PATH"' in result.stdout


@pytest.mark.parametrize(
    ("states", "name", "start"),
    [
        (("enabled",), "the systemd service cbcl.service", f"{SUDO}systemctl start cbcl"),
        (("active",), "the systemd service cbcl.service", f"{SUDO}systemctl start cbcl"),
        (None, "the launchd job ai.cbcl.communicator", f"launchctl load {LAUNCHD_PLIST}"),
    ],
    ids=["systemd-enabled", "systemd-running", "launchd"],
)
def test_paired_machine_starts_a_service_through_its_service_manager(
    machine: Machine, states, name: str, start: str,
) -> None:
    if states is None:
        machine.with_launchd_job(loaded=False)
    else:
        machine.with_systemd(*states)
    machine.with_pipx().installed("pipx", "1.0.0").paired()
    result = machine.run()
    assert result.returncode == 0, result.stderr
    assert f"cbcl is set up as {name}, so step 1 starts it through" in result.stdout
    assert re.search(rf"^  1\. {re.escape(start)} +# start cbcl$", result.stdout, re.MULTILINE)
    assert "cbcl start -d" not in result.stdout
    assert "cbcl maintenance disable" in result.stdout
    # An active unit with no running cbcl in this account (the installer
    # runs as another user, say) is not stopped first: that would interrupt
    # work this install did not touch.
    assert "systemctl stop" not in result.stdout
    assert "offices start NO new work until step 4" in result.stdout


def test_paired_machine_without_a_service_starts_cbcl_by_hand(machine: Machine) -> None:
    machine.with_pipx().with_systemd().installed("pipx", "1.0.0").paired()
    result = machine.run()
    assert result.returncode == 0, result.stderr
    assert re.search(r"^  1\. cbcl start -d +# start cbcl$", result.stdout, re.MULTILINE)
    assert "set up as" not in result.stdout
    assert "systemctl" not in result.stdout


def step_line(number: int, command: str, comment: str) -> str:
    """A pattern for one numbered line of the next steps."""
    return rf"^  {number}\. {re.escape(command)} +# {re.escape(comment)}$"


@pytest.mark.parametrize(
    ("service", "stop", "comment", "start"),
    [
        (None, "cbcl stop", "stop the cbcl still running", "cbcl start -d"),
        (
            "systemd-running",
            f"{SUDO}systemctl stop cbcl",
            "stop the cbcl still running",
            f"{SUDO}systemctl start cbcl",
        ),
        (
            "systemd-by-hand",
            "cbcl stop",
            "it was started by hand, outside the service",
            f"{SUDO}systemctl start cbcl",
        ),
        (
            "launchd-running",
            f"launchctl unload {LAUNCHD_PLIST}",
            "stop the cbcl still running",
            f"launchctl load {LAUNCHD_PLIST}",
        ),
    ],
    ids=["no-service", "systemd-running", "systemd-by-hand", "launchd-running"],
)
def test_forced_install_over_a_running_cbcl_stops_it_before_the_start(
    machine: Machine, running_cbcl: int, service, stop: str, comment: str, start: str,
) -> None:
    """--force installs over a cbcl that keeps running the code it started
    with. `systemctl start` does nothing for a unit that is already active
    and `launchctl load` fails on a loaded job, so the steps stop it first."""
    if service == "systemd-running":
        machine.with_systemd("enabled", "active")
    elif service == "systemd-by-hand":
        machine.with_systemd("enabled")
    elif service == "launchd-running":
        machine.with_launchd_job()
    machine.with_pipx().installed("pipx", "1.0.0").paired().pid_file(str(running_cbcl))
    result = machine.run("--force")
    assert result.returncode == 0, result.stderr
    assert f"cbcl is still running (PID {running_cbcl})" in result.stdout
    assert re.search(step_line(1, stop, comment), result.stdout, re.MULTILINE)
    assert re.search(step_line(2, start, "start cbcl"), result.stdout, re.MULTILINE)
    assert "offices start NO new work until step 5" in result.stdout
    if service is not None:
        assert "so step 2 starts it through" in result.stdout


def test_paired_machine_unloads_a_launchd_job_that_is_still_loaded(machine: Machine) -> None:
    """`cbcl stop` ends a cbcl that the launchd job runs but leaves the job
    loaded, and `launchctl load` fails on a loaded job."""
    machine.with_launchd_job().with_pipx().installed("pipx", "1.0.0").paired()
    result = machine.run()
    assert result.returncode == 0, result.stderr
    assert "The launchd job ai.cbcl.communicator is still loaded" in result.stdout
    assert re.search(
        step_line(1, f"launchctl unload {LAUNCHD_PLIST}", "unload the job"),
        result.stdout,
        re.MULTILINE,
    )
    assert re.search(
        step_line(2, f"launchctl load {LAUNCHD_PLIST}", "start cbcl"),
        result.stdout,
        re.MULTILINE,
    )
    assert "so step 2 starts it through launchd" in result.stdout
    assert "offices start NO new work until step 5" in result.stdout



# ── --uninstall ─────────────────────────────────────────────────────


def test_uninstall_removes_a_pipx_install_with_pipx(machine: Machine) -> None:
    """`pip uninstall` exits 0 for a package it does not have: pipx must be asked."""
    machine.with_pipx().installed("pipx", "1.0.0")
    result = machine.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert machine.calls("pipx umask=0022 uninstall") == [
        "pipx umask=0022 uninstall cubicle-communicator",
    ]
    assert not machine.is_installed("pipx")
    assert "Your data under ~/.cubicle/ was left untouched" in result.stdout


def test_uninstall_removes_a_venv_install_from_that_venv(machine: Machine, tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    assert machine.run("--venv", str(venv)).returncode == 0
    machine.log_file.write_text("")

    result = machine.run("--uninstall", "--venv", str(venv))

    assert result.returncode == 0, result.stderr
    assert "pip[venv] umask=0022 uninstall -y cubicle-communicator" in machine.calls()
    assert not machine.is_installed("venv")
    assert venv.is_dir(), "the venv is the operator's to remove"
    assert f"rm -rf {venv}" in result.stdout


def test_uninstall_removes_a_user_site_install_with_its_own_python(machine: Machine) -> None:
    """Not whichever `pip` is first on PATH: the Python the entry point names."""
    entry = machine.installed("site", "1.0.0").user_site_entry_point()
    result = machine.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert "pip[site] umask=0022 uninstall -y cubicle-communicator" in machine.calls()
    assert not machine.is_installed("site")
    assert f"the Python behind {entry}" in result.stdout
    assert "Done." in result.stdout


@pytest.mark.parametrize("quoted", [True, False], ids=["space", "long-path"])
def test_uninstall_reads_the_python_of_a_sh_launcher(
    machine: Machine, tmp_path: Path, quoted: bool,
) -> None:
    """pip writes a "#!/bin/sh" launcher when the interpreter path has a
    space (quoted) or is too long for a shebang (bare); the interpreter is
    on its second line."""
    folder = tmp_path / ("my tools" if quoted else "a-very-long-tools-folder")
    folder.mkdir()
    python = folder / "python3"
    shutil.copy(machine.bin / "python3", python)
    named = f'"{python}"' if quoted else str(python)
    entry = machine.installed("site", "1.0.0").user_site_entry_point()
    entry.write_text(
        "#!/bin/sh\n"
        f"'''exec' {named} \"$0\" \"$@\"\n"
        "' '''\n"
        "# -*- coding: utf-8 -*-\n"
    )
    result = machine.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert "pip[site] umask=0022 uninstall -y cubicle-communicator" in machine.calls()
    assert not machine.is_installed("site")
    assert f"the Python behind {entry} ({python})" in result.stdout


def test_uninstall_says_when_it_cannot_tell_a_launchers_python(machine: Machine) -> None:
    entry = machine.installed("site", "1.0.0").user_site_entry_point()
    entry.write_text("#!/bin/sh\nexec something-else \"$@\"\n")
    result = machine.run("--uninstall")
    assert result.returncode == 1
    assert f"Found {entry}, but could not tell which Python it runs on." in result.stderr
    assert "No cbcl install found" not in result.stderr
    assert "Nothing was removed." in result.stderr
    assert [line for line in machine.calls() if " uninstall" in line] == []
    assert machine.is_installed("site")


def test_uninstall_leaves_a_python_that_no_cbcl_command_leads_to(machine: Machine) -> None:
    """A Python on PATH that merely has the package (a development checkout,
    another environment) is not what the operator asked to remove."""
    machine.installed("site", "1.0.0")
    result = machine.run("--uninstall")
    assert result.returncode == 1
    assert "No cbcl install found" in result.stderr
    assert [line for line in machine.calls() if " uninstall" in line] == []
    assert machine.is_installed("site")


def test_uninstall_with_nothing_installed_says_so(machine: Machine) -> None:
    machine.with_pipx()
    result = machine.run("--uninstall")
    assert result.returncode == 1
    assert "No cbcl install found" in result.stderr
    assert "--uninstall --venv <path>" in result.stderr
    assert "Nothing was removed." in result.stderr
    assert "Done." not in result.stdout
    assert [line for line in machine.calls() if " uninstall" in line] == []


def test_uninstall_refuses_a_venv_that_does_not_hold_cbcl(machine: Machine, tmp_path: Path) -> None:
    missing = machine.run("--uninstall", "--venv", str(tmp_path / "nowhere"))
    assert missing.returncode == 1
    assert "No venv at" in missing.stderr

    venv = tmp_path / "venv"
    assert machine.run("--venv", str(venv)).returncode == 0
    (machine.state / "venv.version").unlink()
    empty = machine.run("--uninstall", "--venv", str(venv))
    assert empty.returncode == 1
    assert "cbcl is not installed in the venv" in empty.stderr
    assert "Nothing was removed." in empty.stderr


def test_uninstall_reports_a_second_install_that_remains(machine: Machine) -> None:
    machine.with_pipx().installed("pipx", "1.0.0")
    leftover = machine.user_site_entry_point()
    result = machine.run("--uninstall", STUB_KEEP_ENTRY="1")
    assert result.returncode == 1
    assert not machine.is_installed("pipx")
    assert f"cbcl is still installed at {leftover}" in result.stderr
    assert "Run this again" in result.stderr
    assert "Done." not in result.stdout


@pytest.mark.parametrize(
    ("service", "commands"),
    [
        ("systemd", [
            f"{SUDO}systemctl disable --now cbcl",
            f"{SUDO}rm /etc/systemd/system/cbcl.service    # the unit file you created",
            f"{SUDO}systemctl daemon-reload",
        ]),
        ("launchd", [f"launchctl unload {LAUNCHD_PLIST}", f"rm {LAUNCHD_PLIST}"]),
    ],
    ids=["systemd", "launchd"],
)
def test_uninstall_says_how_to_remove_the_service_left_behind(
    machine: Machine, service: str, commands: list[str],
) -> None:
    """The service would keep trying to start the cbcl that was removed."""
    if service == "systemd":
        machine.with_systemd("enabled")
    else:
        machine.with_launchd_job()
    machine.with_pipx().installed("pipx", "1.0.0")
    result = machine.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert not machine.is_installed("pipx")
    assert "is still set up as the" in result.stderr
    warnings = warning_lines(result)
    for command in commands:
        assert command in warnings


def test_uninstall_without_a_service_says_nothing_about_one(machine: Machine) -> None:
    machine.with_pipx().with_systemd().installed("pipx", "1.0.0")
    result = machine.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert "still set up as" not in result.stderr
    assert "systemctl" not in result.stderr


def test_uninstall_leaves_alone_a_service_that_another_cbcl_may_use(
    machine: Machine, tmp_path: Path,
) -> None:
    venv = tmp_path / "venv"
    assert machine.run("--venv", str(venv)).returncode == 0
    leftover = machine.with_systemd("enabled").user_site_entry_point()
    result = machine.run("--uninstall", "--venv", str(venv))
    assert result.returncode == 0, result.stderr
    assert f"another cbcl is still installed at {leftover}" in result.stdout
    assert "still set up as" not in result.stderr
