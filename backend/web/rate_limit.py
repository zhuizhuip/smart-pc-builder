"""简单的内存限流器（单进程演示用，防止突发刷量烧 API 费用）。"""
from __future__ import annotations

import time
from collections import defaultdict
from functools import wraps
from typing import Callable

from flask import jsonify, request

# 每个来源（IP + 路径）的最近请求时间戳，进程内内存存储，重启即清零
_requests: dict[str, list[float]] = defaultdict(list)


def rate_limit(limit: int = 20, window: int = 60) -> Callable:
    """限制每个来源在 window 秒内最多 limit 次请求，超限返回 429。"""

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            key = f"{ip}:{request.path}"
            now = time.time()
            # 清掉窗口外的旧记录
            _requests[key] = [t for t in _requests[key] if now - t < window]
            if len(_requests[key]) >= limit:
                return jsonify({"ok": False, "error": "操作太频繁，请稍后再试"}), 429
            _requests[key].append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
