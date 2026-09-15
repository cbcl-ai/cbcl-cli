"""Host-owned, revocable identities for office tool-proxy sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import secrets
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class ProxyCredentials:
    tool_token: str = field(repr=False)
    collections_token: str = field(repr=False)


@dataclass(frozen=True)
class ProxySession:
    caller: Mapping[str, Any]
    is_live: Callable[[], bool] = field(repr=False)
    collections_only: bool = False


class ProxySessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, ProxySession] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, caller: dict, is_live: Callable[[], bool]) -> ProxyCredentials:
        if caller.get("role") not in ("manager", "worker") or not caller.get("agent_name"):
            raise ValueError("A concrete supervisor identity is required")
        identity = MappingProxyType(dict(caller))
        credentials = ProxyCredentials(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        self._sessions[self._digest(credentials.tool_token)] = ProxySession(identity, is_live)
        self._sessions[self._digest(credentials.collections_token)] = ProxySession(identity, is_live, True)
        return credentials

    def resolve(self, token: str, *, collections: bool = False) -> ProxySession | None:
        if not token or len(token) > 256:
            return None
        session = self._sessions.get(self._digest(token))
        if session is None or (session.collections_only and not collections):
            return None
        try:
            return session if session.is_live() else None
        except Exception:
            return None

    def revoke(self, credentials: ProxyCredentials | None) -> None:
        if credentials is not None:
            for token in (credentials.tool_token, credentials.collections_token):
                self._sessions.pop(self._digest(token), None)

    def clear(self) -> None:
        self._sessions.clear()
