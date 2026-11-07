"""Strict parsing shared by content producers and job contracts."""

from typing import Any


def parse_reference_ids(value: Any, out: set[str]) -> bool:
    """收集引用字段里的字符串 ID；返回是否全部可解析。

    已知引用字段出现嵌套数组 / 对象 / 数字等异常结构时必须标记 unparseable
    （fail-closed），不能静默丢弃——否则该 Job 可能因解析不完整而漏检。
    """
    if value is None:
        return True
    if isinstance(value, str):
        # Producers use an empty optional scalar for absent Turn/Memory ids.
        # Empty array *elements* remain malformed below.
        if value == "":
            return True
        if not value.strip():
            return False
        out.add(value)
        return True
    if isinstance(value, (list, tuple, set)):
        ok = True
        for item in value:
            if isinstance(item, str) and item.strip():
                out.add(item)
            else:
                ok = False
        return ok
    return False
