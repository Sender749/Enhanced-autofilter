"""Telegram Mini App auth: validate `initData` signed by Telegram with the bot token."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from config import BOT_TOKEN

_MAX_AGE = 24 * 3600


def validate_init_data(init_data: str) -> dict | None:
    """Returns the Telegram user dict if the signature is valid and fresh, else None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > _MAX_AGE:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None
