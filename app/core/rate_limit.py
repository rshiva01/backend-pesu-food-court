from app.core.redis import get_redis


async def hit(key: str, limit: int, window_s: int) -> bool:
    """Fixed-window counter. Returns True if this hit is within the limit."""
    r = get_redis()
    k = f"rl:{key}"
    n = int(await r.incr(k))
    if n == 1:
        await r.expire(k, window_s)
    return bool(n <= limit)


async def reset(key: str) -> None:
    await get_redis().delete(f"rl:{key}")
