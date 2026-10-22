"""Map sndbx Web UI sessions to aiset principals."""

from __future__ import annotations

from typing import Any, Dict, Optional

from aiset.service import PERMISSIONS, Principal

VIEWER_PERMISSIONS = frozenset({"settings_read_source", "settings_read_effective"})
ALL_PERMISSIONS = frozenset(PERMISSIONS)


def principal_from_session(session: Optional[Dict[str, Any]]) -> Principal:
    """Return a principal for a session dict; anonymous gets no permissions."""
    if not session:
        return Principal(id="anonymous", permissions=frozenset())
    login = str(session.get("login", "") or "anonymous")
    permissions = set(session.get("permissions", []) or [])
    if "all" in permissions:
        return Principal(id=login, permissions=ALL_PERMISSIONS)
    return Principal(id=login, permissions=VIEWER_PERMISSIONS)
