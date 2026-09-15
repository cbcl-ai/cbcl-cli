"""Container-private CLI input files, never placed in the shared workspace."""

import uuid

SESSION_FILE_DIRECTORY = "/tmp/cbcl-session-files"

ENSURE_DIRECTORY_PROGRAM = """
import os
import stat
import sys

directory = sys.argv[1]
os.makedirs(directory, mode=0o700, exist_ok=True)
metadata = os.lstat(directory)
if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
    raise RuntimeError("Session directory ownership or type is unsafe")
os.chmod(directory, 0o700)
"""

WRITE_FILE_PROGRAM = """
import os
import sys

descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(descriptor, "wb") as destination:
    while True:
        chunk = sys.stdin.buffer.read(65536)
        if not chunk:
            break
        destination.write(chunk)
"""


def session_file_path(kind: str) -> str:
    if kind not in {"prompt", "mcp"}:
        raise ValueError("Unsupported session input file")
    suffix = ".json" if kind == "mcp" else ""
    return f"{SESSION_FILE_DIRECTORY}/.{kind}-{uuid.uuid4().hex}{suffix}"
