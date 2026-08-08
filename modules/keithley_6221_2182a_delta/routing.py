"""读取不暴露在界面中的 7001 与 3706A 物理路由。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


_ROUTE_PATTERNS = {
    "7001": re.compile(r"^[12]![1-9][0-9]*(?:![1-9][0-9]*)?$"),
    "3706a": re.compile(r"^[1-6][0-9]{3}$"),
}


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """某一种切换器的四个逻辑通道路由。"""

    switcher_type: str
    channels: dict[str, tuple[str, ...]]
    source_path: Path

    @property
    def all_routes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                route
                for channel in self.channels.values()
                for route in channel
            )
        )


@dataclass(frozen=True, slots=True)
class RoutingTable:
    """同一个 ``routing.toml`` 中的全部切换器路由。"""

    switchers: dict[str, RoutingConfig]
    source_path: Path

    def for_switcher(self, switcher_type: str) -> RoutingConfig:
        try:
            return self.switchers[switcher_type]
        except KeyError as exc:
            raise ValueError(f"routing for {switcher_type!r} is not configured") from exc


def _load_channels(
    raw: object,
    *,
    switcher_type: str,
    source: Path,
) -> RoutingConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"routing [switchers.{switcher_type}.channels] table is missing")
    pattern = _ROUTE_PATTERNS[switcher_type]
    channels: dict[str, tuple[str, ...]] = {}
    for index in range(1, 5):
        key = f"ch{index}"
        values = raw.get(key)
        if (
            not isinstance(values, list)
            or len(values) != 4
            or any(not isinstance(item, str) for item in values)
        ):
            raise ValueError(
                f"routing {switcher_type}.{key} must contain exactly four addresses"
            )
        normalized = tuple(item.strip() for item in values)
        if len(set(normalized)) != 4:
            raise ValueError(f"routing {switcher_type}.{key} contains duplicate addresses")
        invalid = [item for item in normalized if pattern.fullmatch(item) is None]
        if invalid:
            raise ValueError(
                f"routing {switcher_type}.{key} contains invalid addresses: "
                + ", ".join(invalid)
            )
        channels[key] = normalized
    return RoutingConfig(switcher_type, channels, source.resolve())


def load_routing(path: Path | None = None) -> RoutingTable:
    """严格读取两个切换器分区；不接受旧的单 ``[channels]`` 格式。"""

    source = path if path is not None else Path(__file__).with_name("routing.toml")
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read routing configuration: {exc}") from exc
    if raw.get("format_version") != 2:
        raise ValueError("routing format_version must be 2")
    switchers = raw.get("switchers")
    if not isinstance(switchers, dict):
        raise ValueError("routing [switchers] table is missing")
    configs = {
        name: _load_channels(
            switchers.get(name, {}).get("channels")
            if isinstance(switchers.get(name), dict)
            else None,
            switcher_type=name,
            source=source,
        )
        for name in ("7001", "3706a")
    }
    return RoutingTable(configs, source.resolve())


__all__ = ["RoutingConfig", "RoutingTable", "load_routing"]
