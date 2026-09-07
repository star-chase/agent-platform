import random
import time
from typing import Callable, TypeVar


T = TypeVar("T")


def is_retryable_error(error: Exception) -> bool:
    """判断异常是否属于瞬时错误，适合重试。"""
    if isinstance(error, TimeoutError):
        return True
    status_code = getattr(error, "status_code", None)
    return status_code == 429 or (status_code is not None and status_code >= 500)


def retry_with_backoff(
    fn: Callable[[], T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[], float] | None = None,
) -> T:
    """执行 fn；遇到瞬时错误时按指数退避重试。"""
    if max_retries < 0:
        raise ValueError("max_retries 不能小于 0")
    if base_delay < 0:
        raise ValueError("base_delay 不能小于 0")
    jitter_fn = jitter_fn or (lambda: random.uniform(0, base_delay * 0.1))
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as error:
            if not is_retryable_error(error) or attempt == max_retries:
                raise
            delay = base_delay * (2 ** attempt) + jitter_fn() 
            sleep_fn(delay)
    raise RuntimeError("重试逻辑异常结束")
