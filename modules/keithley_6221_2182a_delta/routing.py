"""读取不暴露在界面中的 Keithley 7001 通道路由。

7001 的地址取决于卡类型。配置文件保存的是手册定义的完整 Channel List 项，而不是
让后端猜测卡型号。默认值按用户给出的同一张非矩阵卡、Slot 1 配置；若实际卡使用
矩阵行列地址，可把项目改成 ``1!row!column``，无需修改 Python 代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


_ROUTE = re.compile(
    r"^[12]![1-9][0-9]*(?:![1-9][0-9]*)?$"
)


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """四个逻辑通道对应的已验证 7001 Channel List。"""

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
        """生成 ``(@1!1,1!11,...)`` SCPI Channel List。"""

        return "(@" + ",".join(self.channels[channel]) + ")"

    @property
    def all_list_text(self) -> str:
        return "(@" + ",".join(self.all_routes) + ")"


def load_routing(
    path: Path | None = None,
) -> RoutingConfig:
    """读取并严格验证 routing.toml。

    每个逻辑通道必须恰好配置四个不重复触点。不同逻辑通道之间允许共享公共电压线，
    例如默认的 ``1!5`` 和 ``1!15``。
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
                f"routing {key} contains invalid 7001 addresses: "
                + ", ".join(invalid)
            )
        channels[key] = normalized
    return RoutingConfig(
        channels=channels,
        source_path=source.resolve(),
    )


__all__ = ["RoutingConfig", "load_routing"]
