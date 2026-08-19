"""In-memory per-user cooldowns to blunt command spam. No DB needed —
losing this state on restart is fine, it's just an abuse throttle."""

import time


def check_cooldown(bot_data: dict, bucket: str, user_id: int, seconds: float) -> bool:
    """Return True if the action is allowed now (and record it); False if the
    caller is still within the cooldown window.

    `bucket` isolates independent cooldowns (e.g. "buy", "support") so they
    don't interfere with each other.
    """
    store = bot_data.setdefault("_cooldowns", {}).setdefault(bucket, {})
    now = time.monotonic()
    last = store.get(user_id)
    if last is not None and now - last < seconds:
        return False
    store[user_id] = now
    if len(store) > 5000:  # keep memory bounded for a long-running process
        for key in list(store)[:1000]:
            store.pop(key, None)
    return True
