"""读取不暴露在界面中的 Keithley 3706A TSP 通道路由。

Series 3700A 使用四位全局通道号：第一位是 1-6 的槽号，后三位由插卡定义。
配置文件保存完整物理通道号，后端不会依据卡型号或界面序号猜测接线。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


_ROUTE = re.compile(r"^[1-6][0-9]{3}$")


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """四个逻辑通道对应的已验证 3706A Channel List。"""

    channels: dict[str, tuple[str, ...]]
    source_path: Path

    @property
    def all_routes(self) -> tuple[str, ...]:
        """按首次出现顺序返回所有配置中使用的物理触点。"""

        return tuple(
            dict.fromkeys(
                route
                for channel in self.channels.values()
                for route in channel
            )
        )

    def list_text(self, channel: str) -> str:
        """生成可直接放入 TSP 字符串参数的逗号分隔 Channel List。"""

        return ",".join(self.channels[channel])

    @property
    def all_list_text(self) -> str:
        """返回全部已配置物理触点的 TSP Channel List。"""

        return ",".join(self.all_routes)


def load_routing(
    path: Path | None = None,
) -> RoutingConfig:
    """读取并严格验证 routing.toml。

    每个逻辑通道必须恰好配置四个不重复触点。不同逻辑通道之间允许共享公共电压线，
    例如默认的 ``1005`` 和 ``1015``。这里只能验证地址形状；具体通道是否存在、
    是否允许一起闭合以及接线是否正确，仍由 3706A 回读和首次硬件验证裁决。
    """

    source = (
        path
        if path is not None
        else Path(__file__).with_name("routing.toml")
    )
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            f"cannot read routing configuration: {exc}"
        ) from exc
    if raw.get("format_version") != 1:
        raise ValueError("routing format_version must be 1")
    table = raw.get("channels")
    if not isinstance(table, dict):
        raise ValueError("routing [channels] table is missing")
    channels: dict[str, tuple[str, ...]] = {}
    for index in range(1, 5):
        key = f"ch{index}"
        values = table.get(key)
        if (
            not isinstance(values, list)
            or len(values) != 4
            or any(not isinstance(item, str) for item in values)
        ):
            raise ValueError(
                f"routing {key} must contain exactly four channel addresses"
            )
        normalized = tuple(item.strip() for item in values)
        if len(set(normalized)) != 4:
            raise ValueError(
                f"routing {key} contains duplicate channel addresses"
            )
        invalid = [
            item
            for item in normalized
            if _ROUTE.fullmatch(item) is None
        ]
        if invalid:
            raise ValueError(
                f"routing {key} contains invalid 3706A addresses: "
                + ", ".join(invalid)
            )
        channels[key] = normalized
    return RoutingConfig(
        channels=channels,
        source_path=source.resolve(),
    )


__all__ = ["RoutingConfig", "load_routing"]
