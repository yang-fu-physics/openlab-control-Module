"""Keithley 3706A 的 TSP 路由命令和严格闭合列表解析。"""

from __future__ import annotations

import re


IDENTIFY = "print(localnode.model)"
OPEN_ALL = 'channel.open("allslots")'
CLOSED_QUERY = 'print(channel.getclose("allslots"))'
_CLOSED_ROUTE_TOKEN = re.compile(r"^([1-6][0-9]{3})(?:\(([1-6][0-9]{3})\))?$")


def validate_identity(identity: str) -> bool:
    normalized = identity.strip().upper()
    return normalized == "3706" or re.fullmatch(
        r"3706A(?:-(?:S|NFP|SNFP))?", normalized
    ) is not None


def close_command(routes: tuple[str, ...]) -> str:
    return f'channel.exclusiveclose("{",".join(routes)}")'


def parse_closed_routes(reply: str) -> frozenset[str]:
    stripped = reply.strip()
    if not stripped:
        return frozenset()
    actual: list[str] = []
    for raw_token in stripped.split(","):
        token = raw_token.strip()
        matched = _CLOSED_ROUTE_TOKEN.fullmatch(token)
        if matched is None:
            raise ValueError(f"invalid closed-channel token {token!r}")
        actual.append(matched.group(1))
        if matched.group(2) is not None:
            actual.append(matched.group(2))
    result = frozenset(actual)
    if len(result) != len(actual):
        raise ValueError("closed-channel response contains duplicate routes")
    return result


__all__ = [
    "IDENTIFY",
    "OPEN_ALL",
    "CLOSED_QUERY",
    "validate_identity",
    "close_command",
    "parse_closed_routes",
]
