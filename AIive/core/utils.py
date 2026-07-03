"""
公共工具模块

提供项目通用的装饰器和工具函数。
"""

import threading
import ctypes
from functools import wraps
from typing import Callable, TypeVar, Any

T = TypeVar("T")


def timeout(seconds: int = 10) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    超时装饰器，用于限制操作时间。

    Args:
        seconds: 超时秒数，默认 10 秒

    Returns:
        装饰器函数

    Raises:
        TimeoutError: 操作超过指定时间
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            result: list[T | None] = [None]
            error: list[BaseException | None] = [None]

            def target() -> None:
                try:
                    result[0] = func(*args, **kwargs)
                except BaseException as e:
                    error[0] = e

            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            thread.join(timeout=seconds)

            if thread.is_alive():
                tid = thread.ident
                if tid is not None:
                    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(tid),
                        ctypes.py_object(SystemExit),
                    )
                    if res > 1:
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(
                            ctypes.c_ulong(tid), None
                        )
                thread.join(2)
                raise TimeoutError(f"操作超时（{seconds}秒）")

            if error[0] is not None:
                raise error[0]

            return result[0]  # type: ignore[return-value]

        return wrapper  # type: ignore[return-value]
    return decorator
