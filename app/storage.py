from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx


async def save_log(
    *,
    supabase_url: str,
    supabase_key: str,
    table: str,
    user_id: int | None,
    input_url: str,
    final_url: str | None,
    status: str,
    error: str | None = None,
) -> None:
    if not supabase_url or not supabase_key:
        return
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return
    payload = {
        "telegram_user_id": user_id,
        "input_url": input_url[:2048],
        "final_url": final_url[:2048] if final_url else None,
        "status": status,
        "error": error[:500] if error else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            await client.post(
                f"{supabase_url.rstrip('/')}/rest/v1/{table}",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=payload,
            )
    except httpx.HTTPError:
        # Logging must never make the resolver unavailable.
        return
