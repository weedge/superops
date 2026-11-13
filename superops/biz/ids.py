from __future__ import annotations

import secrets

from .timeutil import now


def new_id(prefix: str) -> str:
    return f"{prefix}_{now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
