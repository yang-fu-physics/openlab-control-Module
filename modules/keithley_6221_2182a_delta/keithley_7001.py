"""Keithley 7001 的 SCPI 路由命令和严格读回解析。"""

from __future__ import annotations


IDENTIFY = "*IDN?"
OPEN_ALL = "ROUT:OPEN ALL"


def validate_identity(identity: str) -> bool:
    normalized = identity.upper()
    return "KEITHLEY" in normalized and "7001" in normalized


def channel_list(routes: tuple[str, ...]) -> str:
    return "(@" + ",".join(routes) + ")"


def open_query(routes: tuple[str, ...]) -> str:
    return "ROUT:OPEN? " + channel_list(routes)


def close_command(routes: tuple[str, ...]) -> str:
    return "ROUT:CLOS " + channel_list(routes)


def close_query(routes: tuple[str, ...]) -> str:
    return "ROUT:CLOS? " + channel_list(routes)


def parse_route_states(reply: str, count: int) -> tuple[bool, ...]:
    tokens = [item.strip().upper() for item in reply.split(",")]
    if len(tokens) != count:
        raise ValueError(f"expected {count} states, received {len(tokens)}")
    states: list[bool] = []
    for token in tokens:
        if token in {"1", "+1", "ON"}:
            states.append(True)
        elif token in {"0", "+0", "OFF"}:
            states.append(False)
        else:
            raise ValueError(f"invalid route state {token!r}")
    return tuple(states)


__all__ = [
    "IDENTIFY",
    "OPEN_ALL",
    "validate_identity",
    "open_query",
    "close_command",
    "close_query",
    "parse_route_states",
]
