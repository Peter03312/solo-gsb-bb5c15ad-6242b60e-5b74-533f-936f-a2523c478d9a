"""时间工具：统一使用时区感知时间并以 UTC 存储/比较。"""
from __future__ import annotations

from datetime import datetime, timezone


def to_utc(dt: datetime) -> datetime:
    """把时区感知的 datetime 转为 UTC；拒绝 naive datetime。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间戳必须带时区信息（例如 2026-10-01T00:00:00+00:00 或 Z）")
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    """输出 ISO-8601 UTC 字符串。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_stored(value: str) -> datetime:
    """解析库里存的 ISO-8601 字符串。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
