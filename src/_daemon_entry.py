"""Private entry point for a fresh, detached Communicator interpreter."""

import json
import sys

from src.config import Config
from src.daemon import _run_daemon_process


def main() -> None:
    config = Config(**json.load(sys.stdin))
    sys.stdin.close()
    _run_daemon_process(config)


if __name__ == "__main__":
    main()
