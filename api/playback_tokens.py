"""Short-lived signed tokens for public, login-free video playback URLs.

These tokens reduce permanent URL sharing; they are not a substitute for student
authentication because the playback-access endpoint is intentionally public.
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from config.settings import SECRET_KEY

# Keep long lectures working while making URLs temporary.
PLAYBACK_TOKEN_TTL_SECONDS = 6 * 60 * 60


def create_playback_token(video_id: str, message_id: int, ttl_seconds: int = PLAYBACK_TOKEN_TTL_SECONDS) -> str:
    payload = {
        "video_id": str(video_id),
        "message_id": int(message_id),
        "exp": int(time.time()) + max(30, min(int(ttl_seconds), PLAYBACK_TOKEN_TTL_SECONDS)),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(SECRET_KEY.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    signature_text = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded}.{signature_text}"


def verify_playback_token(token: Optional[str], video_id: Optional[str], message_id: int) -> bool:
    if not token or not video_id:
        return False
    try:
        encoded, signature_text = token.split(".", 1)
        expected = hmac.new(SECRET_KEY.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        padding = "=" * (-len(signature_text) % 4)
        actual = base64.urlsafe_b64decode(signature_text + padding)
        if not hmac.compare_digest(expected, actual):
            return False
        payload_padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + payload_padding).decode("utf-8"))
        return (
            str(payload.get("video_id")) == str(video_id)
            and int(payload.get("message_id", -1)) == int(message_id)
            and int(payload.get("exp", 0)) >= int(time.time())
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
