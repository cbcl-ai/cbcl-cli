"""SSH key fingerprint extraction.

Uses ``ssh-keygen`` (already on every dev box, no new Python deps)
to derive the public key from a private key, then computes the
standard OpenSSH SHA256 fingerprint via stdlib hashlib + base64.

Refuses encrypted/passphrase-protected keys outright — we can't
prompt the user for a passphrase from a WS handler, and silently
storing an encrypted key would defeat the "drop it in
~/.ssh/, it just works" UX.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Sentinels that flag an encrypted private key. The user must
# decrypt the key on their own machine BEFORE pasting it into the
# UI.
_ENCRYPTED_MARKERS = (
    # Legacy PEM-style encrypted keys.
    "Proc-Type: 4,ENCRYPTED",
    "DEK-Info:",
    # Modern OpenSSH encrypted keys carry an explicit cipher line.
    # We detect via ssh-keygen below for the modern format because
    # the body is binary; this marker is the PEM fallback.
    "ENCRYPTED PRIVATE KEY",
)


class SshKeyParseError(Exception):
    """Raised when the input isn't a usable private key."""


@dataclass(slots=True)
class FingerprintResult:
    fingerprint: str  # "SHA256:<base64-no-padding>"
    public_key: str   # "ssh-ed25519 AAAA... [comment]"
    key_type: str     # "ssh-ed25519", "ssh-rsa", etc.


def compute_fingerprint(private_key_pem: str) -> FingerprintResult:
    """Derive the public key from a private key and compute the
    SHA256 fingerprint.

    Writes the private key to a temporary file with mode 0600,
    calls ``ssh-keygen -y -P "" -f <tmpfile>`` to extract the
    public key, parses out the base64 blob, and computes
    ``SHA256:base64(sha256(blob))`` — the same fingerprint
    ``ssh-keygen -lf`` outputs.

    Raises ``SshKeyParseError`` for: encrypted keys, malformed
    input, missing ssh-keygen, unexpected output.
    """
    text = private_key_pem.strip()
    if not text:
        raise SshKeyParseError("empty key")

    if not text.startswith("-----BEGIN"):
        raise SshKeyParseError(
            "input doesn't look like a PEM/OpenSSH private key "
            "(expected '-----BEGIN' header)",
        )

    for marker in _ENCRYPTED_MARKERS:
        if marker in text:
            raise SshKeyParseError(
                "encrypted/passphrase-protected keys are not "
                "supported. Decrypt the key first "
                "(e.g. `ssh-keygen -p -f keyfile` to remove the "
                "passphrase) and try again.",
            )

    tmp = tempfile.NamedTemporaryFile(
        prefix="cbcl-ssh-", suffix=".key",
        delete=False, mode="w", encoding="utf-8",
    )
    try:
        # PEM-style keys generally end with a newline; modern OpenSSH
        # private keys MUST end with a newline or ssh-keygen warns
        # "no newline at end of file". Normalise.
        body = text if text.endswith("\n") else text + "\n"
        tmp.write(body)
        tmp.flush()
        tmp.close()
        os.chmod(tmp.name, 0o600)

        try:
            result = subprocess.run(
                ["ssh-keygen", "-y", "-P", "", "-f", tmp.name],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError as exc:
            raise SshKeyParseError(
                "ssh-keygen is not installed on the cbcl host. "
                "Install OpenSSH (it ships by default on macOS/Linux) "
                "and try again.",
            ) from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            # Common: encrypted key path (modern OpenSSH format
            # bypasses the marker check above) — ssh-keygen says
            # "key_load_private_type: incorrect passphrase".
            if "passphrase" in stderr.lower() or "incorrect passphrase" in stderr.lower():
                raise SshKeyParseError(
                    "encrypted/passphrase-protected keys are not "
                    "supported. Remove the passphrase with "
                    "`ssh-keygen -p -f keyfile` first.",
                )
            raise SshKeyParseError(
                f"ssh-keygen rejected the key: {stderr or 'unknown error'}",
            )

        public_key = (result.stdout or "").strip()
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass

    if not public_key:
        raise SshKeyParseError("ssh-keygen produced no public key output")

    # Output shape: "<type> <base64-blob> [optional comment]"
    parts = public_key.split(None, 2)
    if len(parts) < 2:
        raise SshKeyParseError(
            f"ssh-keygen output not in expected format: {public_key!r}",
        )
    key_type, blob_b64 = parts[0], parts[1]
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except Exception as exc:
        raise SshKeyParseError(
            "failed to decode the public key blob — input may be "
            "corrupted",
        ) from exc

    digest = hashlib.sha256(blob).digest()
    fp = "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")
    return FingerprintResult(
        fingerprint=fp, public_key=public_key, key_type=key_type,
    )
