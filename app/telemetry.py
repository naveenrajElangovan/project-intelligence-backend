from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid


LOGGER = logging.getLogger("app.telemetry")


def request_id(candidate: str | None) -> str:
    value = (candidate or "").strip()
    if value and len(value) <= 128 and all(character.isalnum() or character in "-_." for character in value):
        return value
    return str(uuid.uuid4())


def pseudonymous_user(object_id: str, key: str) -> str:
    if not key:
        return "unconfigured"
    return hmac.new(key.encode(), object_id.encode(), hashlib.sha256).hexdigest()[:24]


def started() -> float:
    return time.perf_counter()


def chat_event(
    *,
    request_id_value: str,
    project_id: str,
    user_hash: str,
    began: float,
    status_code: int,
    reason_code: str,
    model_profile: str,
) -> None:
    LOGGER.info(
        json.dumps(
            {
                "event": "backend_chat_complete",
                "request_id": request_id_value,
                "project_id": project_id,
                "user_hash": user_hash,
                "duration_ms": round((time.perf_counter() - began) * 1000, 2),
                "status_code": status_code,
                "reason_code": reason_code,
                "model_profile": model_profile,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
