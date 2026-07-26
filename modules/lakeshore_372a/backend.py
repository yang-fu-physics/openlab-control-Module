"""Lake Shore Model 372 AC Resistance Bridge 的 Measurement Module 后端。

用户界面沿用实验室常用的“372A”名称，协议实现依据 Model 372 手册中的
``FREQ/FILTER/INSET/INTYPE/SCAN`` 设置命令和
``RDGR/QRDG/RDGPWR/RDGST`` 读数命令。模块仍是未经过真实 GPIB 控制器、VISA
实现和仪表固件验证的 Beta，不能把仿真测试通过理解成硬件安全认证。

安全原则是：Enable 只发现资源，不连接、不 Apply；Apply 对每项写入做读回并保持激励
分流；Measure 才逐通道临时解除分流；异常、Stop、Disable 和应用退出都尽力恢复分流。
任何无法读回确认分流的情况都报告 Error，而不是仅凭本机进程退出宣称仪表安全。
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol

from labcontrol.measurement.api import (
    ModuleBackend,
    ModuleError,
    ModuleOperationContext,
    ModuleWarning,
)

from .constants import (
    CURRENT_EXCITATIONS,
    STATUS_BITS,
    default_settings,
)


class InstrumentTransport(Protocol):
    """后端所需的最小通信接口，便于使用真实 PyVISA 或测试替身。"""

    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


TransportFactory = Callable[
    [str, float],
    InstrumentTransport,
]
ResourceLister = Callable[[], tuple[str, ...]]
Waiter = Callable[[ModuleOperationContext, float], None]


class PyVisaTransport:
    """完全位于模块 worker 内的惰性 PyVISA 适配器。

    主进程不 import PyVISA，也不持有 ResourceManager/Instrument；模块被强制回收时，
    VISA 对象随独立进程一起释放。仪表 I/O timeout 在打开资源后立即换算为毫秒设置。
    """

    def __init__(
        self,
        resource_name: str,
        timeout_seconds: float,
    ) -> None:
        """打开单个 VISA session，并把核心的秒制超时转换成 PyVISA 毫秒。"""

        pyvisa = importlib.import_module("pyvisa")
        self._manager = pyvisa.ResourceManager()
        try:
            self._instrument = self._manager.open_resource(
                resource_name
            )
            self._instrument.timeout = max(
                1,
                int(timeout_seconds * 1000),
            )
            self._instrument.read_termination = "\n"
            self._instrument.write_termination = "\n"
        except Exception:
            self._manager.close()
            raise

    @staticmethod
    def list_resources() -> tuple[str, ...]:
        """枚举并排序 GPIB VISA 资源；不把串口/USB/TCPIP 混入下拉框。"""

        pyvisa = importlib.import_module("pyvisa")
        manager = pyvisa.ResourceManager()
        try:
            resources = tuple(
                str(item)
                for item in manager.list_resources()
            )
        finally:
            manager.close()
        return tuple(
            sorted(
                {
                    item
                    for item in resources
                    if item.upper().startswith("GPIB")
                },
                key=str.casefold,
            )
        )

    def write(self, command: str) -> None:
        """向仪表发送一条已经过后端构造的绝对设置命令。"""

        self._instrument.write(command)

    def query(self, command: str) -> str:
        """发送查询并保留 PyVISA 返回的原始文本，解析由后端集中完成。"""

        return str(self._instrument.query(command))

    def close(self) -> None:
        """先关闭仪表 session，再关闭其 ResourceManager。"""

        try:
            self._instrument.close()
        finally:
            self._manager.close()


class LakeShore372ABackend(ModuleBackend):
    """372A 生命周期、设置读回、逐通道测量和异常分流状态机。

    每个 Enabled 通道的测量顺序固定为：

    1. 以“Enabled + Shunted”重新写入并核对该通道配置；
    2. ``SCAN channel,0`` 切换到物理输入并读回确认；
    3. 解除该通道分流，等待 Change Pause；
    4. 获取第一份核心温场快照；
    5. 等待 Scan Dwell，再获取时间戳更新的第二份快照；
    6. 读取电阻、正交分量、功率和 8 位读数状态，计算相角/电流；
    7. 写一行只含该 R 槽位的数据，并按配置或异常路径恢复分流。

    R1-R4 是 DAT 的固定逻辑槽位，不等同于仪表物理输入号；四个物理输入由设置独立选择
    且必须互不重复。
    """

    def __init__(
        self,
        transport_factory: TransportFactory | None = None,
        resource_lister: ResourceLister | None = None,
        waiter: Waiter | None = None,
    ) -> None:
        self._transport_factory = (
            transport_factory or PyVisaTransport
        )
        self._resource_lister = (
            resource_lister
            or PyVisaTransport.list_resources
        )
        self._waiter = (
            waiter
            or (
                lambda context, seconds:
                context.interruptible_sleep(seconds)
            )
        )
        self.transport: InstrumentTransport | None = None
        self.desired_settings = default_settings()
        self.applied_settings: dict[str, Any] = {}
        self.identity = ""
        self.sequence_active = False
        self.last_values: dict[str, Any] = {}
        self.available_resources: tuple[str, ...] = ()

    def initialize(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """Enable 阶段只规范化设置和发现 GPIB，不连接仪表、不发送设置。"""

        self._require_live_context(context)
        self.desired_settings = self._normalized_settings(
            settings,
            require_resource=False,
            operation_timeout_seconds=(
                context.operation_timeout_seconds
            ),
        )
        discovery_message = ""
        try:
            self.available_resources = (
                self._resource_lister()
            )
        except Exception as exc:
            # 资源发现失败不阻止模块窗口打开；用户仍可手动输入 VISA resource。错误
            # 只显示在 Status，真正 Apply 时会再次严格验证并连接。
            self.available_resources = ()
            discovery_message = (
                f"{type(exc).__name__}: {exc}"
            )
        status = {
            "Connection": "Disconnected",
            "Resource": (
                self.desired_settings["resource"]
                or "Not selected"
            ),
            "Identity": "Not queried",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "Excitation": "Shunted",
            "Available GPIB Resources": list(
                self.available_resources
            ),
            "Resource Discovery": (
                discovery_message or "Completed"
            ),
            "Last Channel": "-",
            "Last Resistance (Ohm)": "-",
            "Last Phase (deg)": "-",
            "Last Current (A)": "-",
            "Last Status": "-",
        }
        context.update_status(status)
        return status

    def apply_settings(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """连接并发送用户确认的设置，每个写入都必须通过查询读回。

        所有通道在 Apply 时强制写成 Shunted，即使保存设置来自旧版本也不会仅因 Apply
        打开激励。任一步失败都会尽力分流已涉及通道、关闭 transport，并且不更新
        ``applied_settings``。
        """

        desired = self._normalized_settings(
            settings,
            require_resource=True,
            operation_timeout_seconds=(
                context.operation_timeout_seconds
            ),
        )
        self.desired_settings = deepcopy(desired)
        self._connect(
            desired["resource"],
            float(desired["io_timeout_seconds"]),
        )
        try:
            # FREQ 的第一个参数 0 表示全局/测量输入组；写后立即 FREQ? 核对索引。
            self._write(
                f"FREQ 0,{desired['frequency_index']}",
                context,
            )
            self._expect_integers(
                "FREQ? 0",
                (int(desired["frequency_index"]),),
                context,
            )
            for slot in range(1, 5):
                channel = desired["channels"][
                    f"r{slot}"
                ]
                self._configure_channel(
                    channel,
                    enabled=bool(channel["enabled"]),
                    shunted=True,
                    context=context,
                )
        except Exception:
            # 清理路径不依赖 context checkpoint，Stop 已到达时仍会直接尝试分流。
            self._best_effort_shunt_settings(desired)
            self._close_transport()
            raise
        # 只有全部读回一致后才把 desired 提升为 applied，Measure 绝不使用半套设置。
        self.applied_settings = deepcopy(desired)
        status = {
            "Connection": "Connected",
            "Resource": desired["resource"],
            "Identity": self.identity,
            "Applied Settings": "Applied; excitation shunted",
            "Excitation": "Shunted",
            "Estimated Measure Time (s)": (
                self._estimated_measure_seconds(desired)
            ),
        }
        context.update_status(status)
        return status

    def begin_sequence(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """确认已经 Apply，并标记本次 SEQ；此时仍保持全部激励分流。"""

        self._require_ready()
        self.sequence_active = True
        status = {
            "Sequence": "Running",
            "Excitation": "Shunted",
        }
        context.update_status(status)
        return status

    def measure(
        self,
        context: ModuleOperationContext,
    ) -> None:
        """按 R1→R4 顺序测量 Enabled 通道，每个通道独立 emit 一行稀疏数据。"""

        self._require_ready()
        settings = self.applied_settings
        self._validate_measure_duration(
            settings,
            context.operation_timeout_seconds,
        )
        for slot in range(1, 5):
            channel = settings["channels"][f"r{slot}"]
            if not channel["enabled"]:
                continue
            # 通道之间的 checkpoint 使 Stop 不会开始下一个输入。
            context.checkpoint()
            self._measure_channel(
                slot,
                channel,
                settings,
                context,
            )

    def _measure_channel(
        self,
        slot: int,
        channel: Mapping[str, Any],
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        """执行单通道完整事务，并在 finally 中处理分流确认。"""

        input_channel = int(channel["input_channel"])
        failure: Exception | None = None
        try:
            # 先以 shunted=True 重写完整配置，再切换 SCAN，最后才解除分流。即使上次
            # 测量中断，也不会直接在未知配置下打开激励。
            self._configure_channel(
                channel,
                enabled=True,
                shunted=True,
                context=context,
            )
            self._switch_channel(
                input_channel,
                context,
            )
            self._set_shunt(
                channel,
                shunted=False,
                context=context,
            )
            context.update_status({
                "Excitation": (
                    f"Active on input {input_channel}"
                ),
                "Last Channel": f"R{slot} / input {input_channel}",
            })
            self._waiter(
                context,
                float(settings["pause_seconds"]),
            )
            # 第一份温场快照位于 Change Pause 之后、Dwell 之前。
            first = context.sample_system()
            self._waiter(
                context,
                float(settings["dwell_seconds"]),
            )
            second = context.sample_system()
            # 平均函数还会要求第二份 temperature/field 时间戳严格更新，避免把同一份
            # 缓存读数重复两次伪装成时间平均。
            temperature, field = (
                self._averaged_system_values(
                    first,
                    second,
                )
            )
            resistance = self._query_float(
                f"RDGR? {input_channel}",
                context,
            )
            quadrature = self._query_float(
                f"QRDG? {input_channel}",
                context,
            )
            power = self._query_float(
                f"RDGPWR? {input_channel}",
                context,
            )
            status_bits = self._query_status(
                input_channel,
                context,
            )
            phase = math.degrees(
                # QRDG 是正交分量，RDGR 是同相电阻分量；atan2 保留正确象限。
                math.atan2(
                    quadrature,
                    resistance,
                )
            )
            current = self._excitation_current(
                channel,
                resistance,
                quadrature,
                power,
            )
            status, details = self._status(
                status_bits
            )
            self._publish_reading_warning(
                slot,
                input_channel,
                status,
                details,
                context,
            )
            row = {
                # manifest 为四个槽位预声明列。本行只填写当前槽位，其他 R/Phase/
                # Current/Status 列由核心 DAT writer 留空。
                "TemperatureAverage": temperature,
                "FieldAverage": field,
                f"R{slot}": resistance,
                f"Phase{slot}": phase,
                f"Current{slot}": current,
                f"Status{slot}": status,
            }
            context.emit_row(row)
            self.last_values = {
                "slot": slot,
                "input_channel": input_channel,
                "resistance": resistance,
                "phase": phase,
                "current": current,
                "status": status,
            }
            context.update_status({
                "Last Channel": (
                    f"R{slot} / input {input_channel}"
                ),
                "Last Resistance (Ohm)": resistance,
                "Last Phase (deg)": phase,
                "Last Current (A)": current,
                "Last Status": (
                    status
                    if not details
                    else f"{status}: {details}"
                ),
            })
        except Exception as exc:
            failure = exc
            raise
        finally:
            # 用户可显式关闭“每通道读完分流”，但任何异常都无条件尝试分流。成功路径
            # 若不分流，最终 end_sequence/abort 仍会对全部 Enabled 通道分流。
            should_shunt = (
                bool(settings["shunt_after_read"])
                or failure is not None
            )
            if should_shunt:
                cleanup_error = (
                    self._best_effort_shunt_channel(
                        channel
                    )
                )
                context.update_status({
                    "Excitation": "Shunted",
                })
                if cleanup_error is not None:
                    # 原始测量异常与 cleanup 异常同时存在时，分流未确认具有更高安全
                    # 优先级；用 from failure 保留原始异常链供诊断。
                    raise ModuleError(
                        "Could not confirm excitation shunt "
                        f"for input {input_channel}: "
                        f"{cleanup_error}",
                        "LS372_SHUNT_FAILED",
                        f"input {input_channel}",
                    ) from failure

    def end_sequence(
        self,
        reason: str,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """对 completed/stopped/error 一律分流全部 Enabled 通道。

        任一通道读回失败会让核心把 Run 标为 Faulted；``sequence_active`` 无论如何都
        清除，使后续 Disable 仍可执行。
        """

        try:
            self._shunt_all(context)
        except Exception:
            context.update_status({
                "Sequence": reason.title(),
                "Excitation": "Shunt unconfirmed",
            })
            raise
        finally:
            self.sequence_active = False
        status = {
            "Sequence": reason.title(),
            "Excitation": "Shunted",
        }
        context.update_status(status)
        return status

    def abort(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """Disable/应用退出时逐通道尽力分流，然后无条件关闭 VISA transport。

        分流失败不会阻止本机资源释放，但会在断开后抛出 Error 并显示
        ``Shunt unconfirmed``，提醒用户到仪表面板人工确认。
        """

        errors = self._best_effort_shunt_settings(
            self.applied_settings
        )
        self._close_transport()
        self.sequence_active = False
        self.applied_settings = {}
        status = {
            "Connection": "Disconnected",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "Excitation": (
                "Shunted"
                if not errors
                else "Shunt unconfirmed"
            ),
        }
        context.update_status(status)
        if errors:
            raise ModuleError(
                "One or more 372A inputs could not be "
                "shunted before disconnect: "
                + "; ".join(errors),
                "LS372_SHUNT_FAILED",
            )
        return status

    def read_status(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """只读查询 ``*IDN?`` 验证当前连接，没有连接时不隐式重连。"""

        if self.transport is None:
            return {
                "Connection": "Disconnected",
                "Sequence": (
                    "Running"
                    if self.sequence_active
                    else "Idle"
                ),
            }
        identity = self._query_text("*IDN?", context)
        self._validate_identity(identity)
        self.identity = identity
        return {
            "Connection": "Connected",
            "Resource": (
                self.applied_settings.get("resource")
                or self.desired_settings.get("resource")
                or "Unknown"
            ),
            "Identity": identity,
            "Sequence": (
                "Running"
                if self.sequence_active
                else "Idle"
            ),
        }

    def manual_action(
        self,
        action: str,
        payload: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """处理 Idle 时的资源刷新和连接测试；两者都不会 Apply 仪表设置。"""

        if action == "refresh_resources":
            try:
                self.available_resources = (
                    self._resource_lister()
                )
            except Exception as exc:
                raise ModuleWarning(
                    f"GPIB resource discovery failed: "
                    f"{type(exc).__name__}: {exc}",
                    "LS372_RESOURCE_DISCOVERY_FAILED",
                ) from exc
            status = {
                "Available GPIB Resources": list(
                    self.available_resources
                ),
                "Resource Discovery": "Completed",
                "Last Action": (
                    f"Found {len(self.available_resources)} "
                    "GPIB resource(s)"
                ),
            }
        elif action == "test_connection":
            # 使用 Settings 页“当前尚未保存/Apply”的值，使用户可以先验证新地址。
            supplied = payload.get("settings")
            source = (
                supplied
                if isinstance(supplied, Mapping)
                else self.desired_settings
            )
            settings = self._normalized_settings(
                source,
                require_resource=True,
                operation_timeout_seconds=(
                    context.operation_timeout_seconds
                ),
            )
            self._connect(
                settings["resource"],
                float(settings["io_timeout_seconds"]),
            )
            status = {
                "Connection": "Connected",
                "Resource": settings["resource"],
                "Identity": self.identity,
                "Applied Settings": "Not applied",
                "Last Action": "Connection test passed",
            }
        else:
            return (
                super().manual_action(
                    action,
                    payload,
                    context,
                )
                or {}
            )
        context.update_status(status)
        return status

    @staticmethod
    def _require_live_context(
        context: ModuleOperationContext,
    ) -> None:
        """拒绝缺少实时快照和可中断等待能力的旧核心 API。"""

        if (
            not callable(
                getattr(context, "sample_system", None)
            )
            or not callable(
                getattr(
                    context,
                    "interruptible_sleep",
                    None,
                )
            )
        ):
            raise ModuleError(
                "This module requires the live, interruptible "
                "Measurement Module context added after "
                "OpenLab Control 0.11.0 Beta 2 or newer",
                "LS372_CORE_API_TOO_OLD",
            )

    def _connect(
        self,
        resource: str,
        timeout_seconds: float,
    ) -> None:
        """关闭旧 session，打开新资源并在接管前严格验证 ``*IDN?``。"""

        self._close_transport()
        transport: InstrumentTransport | None = None
        try:
            transport = self._transport_factory(
                resource,
                timeout_seconds,
            )
            identity = str(
                transport.query("*IDN?")
            ).strip()
            self._validate_identity(identity)
        except Exception as exc:
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass
            raise ModuleError(
                f"Could not connect to {resource}: "
                f"{type(exc).__name__}: {exc}",
                "LS372_CONNECTION_FAILED",
                resource,
            ) from exc
        self.transport = transport
        self.identity = identity

    @staticmethod
    def _validate_identity(identity: str) -> None:
        """宽容厂商字符串中的空格/下划线/连字符，但必须明确包含 MODEL372。"""

        compact = re.sub(
            r"[\s_-]+",
            "",
            identity,
        ).upper()
        if "MODEL372" not in compact:
            raise ModuleError(
                f"Expected Lake Shore Model 372, received "
                f"{identity!r}",
                "LS372_IDENTITY_MISMATCH",
            )

    def _write(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> None:
        """通过统一重试入口发送命令，禁止调用者绕过通信错误归一化。"""

        self._call_with_retry(
            command,
            lambda transport: transport.write(command),
            context,
        )

    def _query_text(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> str:
        """查询非空文本；空回复与传输异常一样不能被当作有效读回。"""

        result = self._call_with_retry(
            command,
            lambda transport: transport.query(command),
            context,
        )
        text = str(result).strip()
        if not text:
            raise ModuleError(
                f"Model 372 returned an empty reply to "
                f"{command}",
                "LS372_INVALID_REPLY",
                command,
            )
        return text

    def _call_with_retry(
        self,
        command: str,
        operation: Callable[
            [InstrumentTransport],
            Any,
        ],
        context: ModuleOperationContext,
    ) -> Any:
        """在固定次数内重连重试，并把最终失败升级为 ModuleError。

        本模块写命令都是绝对设置（不是递增/触发型命令），且关键设置随后都有查询读回；
        因此短暂 I/O 失败后允许重发。重连只重新验证仪表身份，不声称恢复全部内部设置，
        这也是 372A 仍需真机断线测试的原因。
        """

        settings = (
            self.applied_settings
            or self.desired_settings
        )
        attempts = int(
            settings.get("retry_attempts", 1)
        )
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            transport = self.transport
            if transport is None:
                last_error = RuntimeError(
                    "Model 372 transport is disconnected"
                )
                if attempt >= attempts:
                    break
                try:
                    self._reopen_transport(settings)
                except Exception as reopen_error:
                    last_error = reopen_error
                continue
            try:
                result = operation(transport)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                context.warning(
                    f"Model 372 I/O failed; retrying "
                    f"({attempt}/{attempts}): "
                    f"{type(exc).__name__}: {exc}",
                    "LS372_IO_RETRY",
                    command,
                )
                self._waiter(context, 0.2)
                try:
                    self._reopen_transport(settings)
                except Exception as reopen_error:
                    last_error = reopen_error
                continue
            context.resolve_warning(
                "LS372_IO_RETRY",
                command,
            )
            return result
        context.resolve_warning(
            "LS372_IO_RETRY",
            command,
        )
        assert last_error is not None
        raise ModuleError(
            f"Model 372 I/O failed after {attempts} "
            f"attempt(s) for {command}: "
            f"{type(last_error).__name__}: {last_error}",
            "LS372_COMMUNICATION_FAILED",
            command,
        ) from last_error

    def _reopen_transport(
        self,
        settings: Mapping[str, Any],
    ) -> None:
        """重建同一资源并只验证身份；不静默修改 ``applied_settings``。"""

        resource = str(settings["resource"])
        timeout = float(settings["io_timeout_seconds"])
        self._close_transport()
        transport = self._transport_factory(
            resource,
            timeout,
        )
        try:
            identity = str(
                transport.query("*IDN?")
            ).strip()
            self._validate_identity(identity)
        except Exception:
            transport.close()
            raise
        self.transport = transport
        self.identity = identity

    def _configure_channel(
        self,
        channel: Mapping[str, Any],
        *,
        enabled: bool,
        shunted: bool,
        context: ModuleOperationContext,
    ) -> None:
        """按 FILTER→INSET→INTYPE 顺序完整配置一个物理输入并逐项核对。

        这里故意不依赖仪表中残留的局部设置：每次都发送完整的绝对参数，随后用对应
        ``...?`` 查询要求整数元组完全一致。这样断线重连或前一次中断后不会在未知配置
        上仅切换 shunt 位。
        """

        settings = (
            self.applied_settings
            or self.desired_settings
        )
        input_channel = int(channel["input_channel"])
        self._write(
            "FILTER "
            f"{input_channel},"
            f"{1 if settings['filter_enabled'] else 0},"
            f"{settings['filter_settle_seconds']},"
            f"{settings['filter_window_percent']}",
            context,
        )
        self._expect_integers(
            f"FILTER? {input_channel}",
            (
                1 if settings["filter_enabled"] else 0,
                int(settings["filter_settle_seconds"]),
                int(settings["filter_window_percent"]),
            ),
            context,
        )
        self._write(
            "INSET "
            f"{input_channel},"
            f"{1 if enabled else 0},"
            f"{settings['dwell_seconds']},"
            f"{settings['pause_seconds']},"
            "0,2",
            context,
        )
        self._expect_integers(
            f"INSET? {input_channel}",
            (
                1 if enabled else 0,
                int(settings["dwell_seconds"]),
                int(settings["pause_seconds"]),
                0,
                2,
            ),
            context,
        )
        self._write(
            self._intype_command(
                channel,
                shunted=shunted,
            ),
            context,
        )
        self._verify_intype(
            channel,
            shunted=shunted,
            context=context,
        )

    def _switch_channel(
        self,
        input_channel: int,
        context: ModuleOperationContext,
    ) -> None:
        """切换 SCAN 到指定输入并关闭仪表内部自动扫描，然后读回确认。"""

        self._write(
            f"SCAN {input_channel},0",
            context,
        )
        self._expect_integers(
            "SCAN?",
            (input_channel, 0),
            context,
        )

    def _set_shunt(
        self,
        channel: Mapping[str, Any],
        *,
        shunted: bool,
        context: ModuleOperationContext,
    ) -> None:
        """用完整 INTYPE 设置改变分流位，并要求仪表返回完全一致的配置。"""

        self._write(
            self._intype_command(
                channel,
                shunted=shunted,
            ),
            context,
        )
        self._verify_intype(
            channel,
            shunted=shunted,
            context=context,
        )

    def _verify_intype(
        self,
        channel: Mapping[str, Any],
        *,
        shunted: bool,
        context: ModuleOperationContext,
    ) -> None:
        """核对 INTYPE 的模式、量程、自动量程、分流和首选单位全部字段。"""

        self._expect_integers(
            f"INTYPE? {channel['input_channel']}",
            self._intype_values(
                channel,
                shunted=shunted,
            ),
            context,
        )

    def _expect_integers(
        self,
        command: str,
        expected: tuple[int, ...],
        context: ModuleOperationContext,
    ) -> None:
        """解析逗号分隔的整数读回，字段数量和值都必须与期望完全相同。

        不做“只比较关键字段”的宽松处理，因为遗漏字段可能掩盖仪表拒绝设置、固件协议
        差异或通道仍处于未确认的激励状态。
        """

        reply = self._query_text(command, context)
        try:
            actual = tuple(
                int(part.strip(), 10)
                for part in reply.split(",")
            )
        except ValueError as exc:
            raise ModuleError(
                f"Model 372 returned an invalid settings "
                f"reply to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            ) from exc
        if actual != expected:
            raise ModuleError(
                f"Model 372 settings readback mismatch for "
                f"{command}: expected {expected}, "
                f"read back {actual}",
                "LS372_SETTINGS_VERIFY_FAILED",
                command,
            )

    def _query_float(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> float:
        """读取一个有限浮点数；NaN/Inf 不允许进入 DAT 或后续电流计算。"""

        reply = self._query_text(command, context)
        try:
            value = float(reply)
        except ValueError as exc:
            raise ModuleError(
                f"Model 372 returned a non-numeric reply "
                f"to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            ) from exc
        if not math.isfinite(value):
            raise ModuleError(
                f"Model 372 returned a non-finite reply "
                f"to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            )
        return value

    def _query_status(
        self,
        input_channel: int,
        context: ModuleOperationContext,
    ) -> int:
        """读取 RDGST 的 8 位状态字，并拒绝负数或超出一个字节的回复。"""

        command = f"RDGST? {input_channel}"
        reply = self._query_text(command, context)
        try:
            value = int(reply, 10)
        except ValueError as exc:
            raise ModuleError(
                f"Model 372 returned an invalid status "
                f"to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            ) from exc
        if not 0 <= value <= 255:
            raise ModuleError(
                f"Model 372 returned an out-of-range status "
                f"to {command}: {value}",
                "LS372_INVALID_REPLY",
                command,
            )
        return value

    @staticmethod
    def _status(
        status_bits: int,
    ) -> tuple[str, str]:
        """把状态位归为正常、超量程或超 compliance，并保留全部原始位名称。

        CS_OVL（bit 0）对电流源安全最直接，因此优先映射为 ``OVER_COMPLIANCE``；
        其余任意置位统一映射为 ``OVER_RANGE``，详细列仍记录所有置位名称。
        """

        details = "|".join(
            label
            for bit, label in STATUS_BITS
            if status_bits & bit
        )
        if status_bits & 1:
            return "OVER_COMPLIANCE", details
        if status_bits:
            return "OVER_RANGE", details
        return "NORMAL", ""

    @staticmethod
    def _publish_reading_warning(
        slot: int,
        input_channel: int,
        status: str,
        details: str,
        context: ModuleOperationContext,
    ) -> None:
        """按逻辑槽位和物理输入发布可恢复 Warning，并在状态恢复时解除。

        ``R槽位/物理输入`` 作为去重上下文：同一故障连续出现只提示一次，不同通道互不
        吞并；compliance 与普通量程告警互斥，状态切换时先解除旧类型。
        """

        warning_context = (
            f"R{slot}/input {input_channel}"
        )
        if status == "OVER_COMPLIANCE":
            context.resolve_warning(
                "LS372_OVER_RANGE",
                warning_context,
            )
            context.warning(
                f"R{slot} input {input_channel} exceeded "
                f"current-source compliance ({details})",
                "LS372_OVER_COMPLIANCE",
                warning_context,
            )
        elif status == "OVER_RANGE":
            context.resolve_warning(
                "LS372_OVER_COMPLIANCE",
                warning_context,
            )
            context.warning(
                f"R{slot} input {input_channel} is outside "
                f"the valid measurement range ({details})",
                "LS372_OVER_RANGE",
                warning_context,
            )
        else:
            context.resolve_warning(
                "LS372_OVER_COMPLIANCE",
                warning_context,
            )
            context.resolve_warning(
                "LS372_OVER_RANGE",
                warning_context,
            )

    @staticmethod
    def _excitation_current(
        channel: Mapping[str, Any],
        resistance: float,
        quadrature: float,
        power: float,
    ) -> float:
        """返回本行所用的激励电流，单位为 A。

        电流模式直接使用手册量程表中的设定值。电压模式没有直接电流读数，因此按仪表
        报告的耗散功率和同相电阻估算 ``sqrt(abs(P)/abs(R))``；零电阻或非有限结果
        视为数据错误。``quadrature`` 目前不参与该耗散模型，参数保留用于明确调用语义。
        """

        if channel["excitation_mode"] == "current":
            values = {
                index: current
                for index, _label, current
                in CURRENT_EXCITATIONS
            }
            return values[
                int(channel["excitation_range"])
            ]
        del quadrature
        dissipative_resistance = abs(resistance)
        if dissipative_resistance <= 0:
            raise ModuleError(
                "Cannot estimate voltage-mode current from "
                "zero resistance",
                "LS372_CURRENT_CALCULATION_FAILED",
            )
        current = math.sqrt(
            abs(power) / dissipative_resistance
        )
        if not math.isfinite(current):
            raise ModuleError(
                "Calculated excitation current is not finite",
                "LS372_CURRENT_CALCULATION_FAILED",
            )
        return current

    @classmethod
    def _averaged_system_values(
        cls,
        first: Mapping[str, Mapping[str, Any]],
        second: Mapping[str, Mapping[str, Any]],
    ) -> tuple[float, float]:
        """从 Dwell 前后两份核心快照计算主温度(K)和主磁场(Oe)算术平均。

        第二份快照必须使用与第一份相同的设备 ID，且时间戳严格增加；不能在两次读取间
        偷换主设备，也不能把同一缓存值重复平均。单位转换完成后才做平均。
        """

        first_temperature = cls._primary_snapshot(
            first,
            "temperature",
        )
        second_temperature = cls._same_snapshot(
            second,
            first_temperature[0],
            "temperature",
        )
        first_field = cls._primary_snapshot(
            first,
            "field",
        )
        second_field = cls._same_snapshot(
            second,
            first_field[0],
            "field",
        )
        cls._require_newer_snapshot(
            first_temperature,
            second_temperature,
        )
        cls._require_newer_snapshot(
            first_field,
            second_field,
        )
        temperature = (
            cls._temperature_kelvin(
                first_temperature[1],
                first_temperature[2],
            )
            + cls._temperature_kelvin(
                second_temperature[1],
                second_temperature[2],
            )
        ) / 2.0
        field = (
            cls._field_oersted(
                first_field[1],
                first_field[2],
            )
            + cls._field_oersted(
                second_field[1],
                second_field[2],
            )
        ) / 2.0
        return temperature, field

    @staticmethod
    def _primary_snapshot(
        system: Mapping[str, Mapping[str, Any]],
        kind: str,
    ) -> tuple[str, float, str, float]:
        """确定温度或磁场的主快照，返回设备 ID、值、单位和时间戳。

        选择优先级固定为显式 ``role=primary``、启用控制的设备、其余设备；同级按设备
        ID 排序，避免字典插入顺序让同一数据集在不同进程中选择不同设备。
        """

        candidates = [
            (str(device_id), values)
            for device_id, values in system.items()
            if str(values.get("kind", "")).casefold()
            == kind
        ]
        candidates.sort(
            key=lambda item: (
                0
                if str(
                    item[1].get("role", "")
                ).casefold()
                == "primary"
                else (
                    1
                    if bool(
                        item[1].get(
                            "control_enabled",
                            False,
                        )
                    )
                    else 2
                ),
                item[0].casefold(),
            )
        )
        if not candidates:
            raise ModuleError(
                f"No {kind} device is present in the "
                "OpenLab system snapshot",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                kind,
            )
        device_id, values = candidates[0]
        return LakeShore372ABackend._snapshot_values(
            device_id,
            values,
            kind,
        )

    @staticmethod
    def _same_snapshot(
        system: Mapping[str, Mapping[str, Any]],
        device_id: str,
        kind: str,
    ) -> tuple[str, float, str, float]:
        """从第二份系统快照提取同一设备，禁止在 Dwell 中途切换数据来源。"""

        values = system.get(device_id)
        if values is None:
            raise ModuleError(
                f"{kind.title()} device {device_id} is missing "
                "from the second system snapshot",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        return LakeShore372ABackend._snapshot_values(
            device_id,
            values,
            kind,
        )

    @staticmethod
    def _snapshot_values(
        device_id: str,
        values: Mapping[str, Any],
        kind: str,
    ) -> tuple[str, float, str, float]:
        """验证核心设备快照处于连接状态，并含有限的 current/timestamp。"""

        if not bool(values.get("connected", True)):
            raise ModuleError(
                f"{kind.title()} device {device_id} is "
                "disconnected",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        try:
            current = float(values["current"])
            timestamp = float(values["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModuleError(
                f"{kind.title()} device {device_id} has no "
                "valid current value or timestamp",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            ) from exc
        if (
            not math.isfinite(current)
            or not math.isfinite(timestamp)
        ):
            raise ModuleError(
                f"{kind.title()} device {device_id} returned "
                "a non-finite value or timestamp",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        return (
            device_id,
            current,
            str(values.get("unit", "")),
            timestamp,
        )

    @staticmethod
    def _require_newer_snapshot(
        first: tuple[str, float, str, float],
        second: tuple[str, float, str, float],
    ) -> None:
        """要求第二次采样时间戳严格更新，防止缓存读数冒充两次独立测量。"""

        if second[3] <= first[3]:
            raise ModuleError(
                f"OpenLab system snapshot for {first[0]} "
                "did not advance between pause and dwell "
                "samples",
                "LS372_SYSTEM_SNAPSHOT_NOT_FRESH",
                first[0],
            )

    @staticmethod
    def _temperature_kelvin(
        value: float,
        unit: str,
    ) -> float:
        """把核心允许的温度单位显式换算为 DAT 约定的 K。"""

        normalized = unit.strip().casefold()
        if normalized in {"k", "kelvin"}:
            return value
        if normalized in {"mk", "millikelvin"}:
            return value / 1000.0
        if normalized in {
            "c",
            "°c",
            "degc",
            "celsius",
        }:
            return value + 273.15
        raise ModuleError(
            f"Unsupported temperature unit: {unit!r}",
            "LS372_SYSTEM_UNIT_UNSUPPORTED",
            unit,
        )

    @staticmethod
    def _field_oersted(
        value: float,
        unit: str,
    ) -> float:
        """把核心允许的磁场单位显式换算为 DAT 约定的 Oe。"""

        normalized = unit.strip().casefold()
        if normalized in {"oe", "oersted", "g", "gauss"}:
            return value
        if normalized in {"koe", "kilo-oersted"}:
            return value * 1000.0
        if normalized in {"t", "tesla"}:
            return value * 10_000.0
        if normalized in {"mt", "millitesla"}:
            return value * 10.0
        raise ModuleError(
            f"Unsupported magnetic-field unit: {unit!r}",
            "LS372_SYSTEM_UNIT_UNSUPPORTED",
            unit,
        )

    def _shunt_all(
        self,
        context: ModuleOperationContext,
    ) -> None:
        """严格分流全部已 Apply 且 Enabled 的输入，并汇总所有失败后一次抛出。

        循环不会因第一个通道失败而停止，因而其余通道仍有机会进入安全状态；只要有一个
        通道未确认，整个清理动作就失败，调用者不能报告安全完成。
        """

        if not self.applied_settings:
            return
        errors: list[str] = []
        for slot in range(1, 5):
            channel = self.applied_settings[
                "channels"
            ][f"r{slot}"]
            if not channel["enabled"]:
                continue
            try:
                self._set_shunt(
                    channel,
                    shunted=True,
                    context=context,
                )
            except Exception as exc:
                errors.append(
                    f"input {channel['input_channel']}: "
                    f"{type(exc).__name__}: {exc}"
                )
        if errors:
            raise ModuleError(
                "Could not shunt all configured Model 372 "
                "inputs: "
                + "; ".join(errors),
                "LS372_SHUNT_FAILED",
            )

    def _best_effort_shunt_channel(
        self,
        channel: Mapping[str, Any],
    ) -> str | None:
        """在异常清理路径直接尝试一次分流及读回，不依赖已取消的操作上下文。

        此路径故意不调用普通重试/等待逻辑：Stop 或 worker 超时后 checkpoint 可能已拒绝
        继续执行。返回 ``None`` 只代表本次读回确认成功，字符串则是必须上报的原因。
        """

        transport = self.transport
        if transport is None:
            return "transport is disconnected"
        try:
            transport.write(
                self._intype_command(
                    channel,
                    shunted=True,
                )
            )
            reply = str(
                transport.query(
                    f"INTYPE? {channel['input_channel']}"
                )
            ).strip()
            actual = tuple(
                int(part.strip(), 10)
                for part in reply.split(",")
            )
            expected = self._intype_values(
                channel,
                shunted=True,
            )
            if actual != expected:
                return (
                    f"shunt readback mismatch: expected "
                    f"{expected}, read back {actual}"
                )
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def _best_effort_shunt_settings(
        self,
        settings: Mapping[str, Any],
    ) -> list[str]:
        """尽力处理设置中的全部 Enabled 通道，并返回所有未确认分流的说明。

        transport 已断开时不能把“没有可写对象”视为安全成功，因为仪表端激励状态未知。
        """

        if not settings:
            return []
        errors: list[str] = []
        channels = settings.get("channels", {})
        if not isinstance(channels, Mapping):
            return []
        for slot in range(1, 5):
            channel = channels.get(f"r{slot}")
            if (
                not isinstance(channel, Mapping)
                or not channel.get("enabled")
            ):
                continue
            if self.transport is None:
                errors.append(
                    f"input {channel.get('input_channel')}: "
                    "transport disconnected; shunt not confirmed"
                )
                continue
            error = self._best_effort_shunt_channel(
                channel
            )
            if error is not None:
                errors.append(
                    f"input {channel.get('input_channel')}: "
                    f"{error}"
                )
        return errors

    @staticmethod
    def _intype_command(
        channel: Mapping[str, Any],
        *,
        shunted: bool,
    ) -> str:
        """构造完整 INTYPE 绝对设置；末尾 ``2`` 固定首选单位为 Ohm。

        手册规定 current=1、voltage=0，分流开启=1。调用者不能只拼接最后一个分流字段，
        以免覆盖或继承未知的其余参数。
        """

        mode = (
            1
            if channel["excitation_mode"] == "current"
            else 0
        )
        return (
            "INTYPE "
            f"{channel['input_channel']},"
            f"{mode},"
            f"{channel['excitation_range']},"
            f"{channel['autorange']},"
            f"{channel['resistance_range']},"
            f"{1 if shunted else 0},"
            "2"
        )

    @staticmethod
    def _intype_values(
        channel: Mapping[str, Any],
        *,
        shunted: bool,
    ) -> tuple[int, ...]:
        """返回 INTYPE? 应读回的六个字段，不含查询中已指定的输入号。"""

        return (
            (
                1
                if channel["excitation_mode"] == "current"
                else 0
            ),
            int(channel["excitation_range"]),
            int(channel["autorange"]),
            int(channel["resistance_range"]),
            1 if shunted else 0,
            2,
        )

    def _close_transport(self) -> None:
        """先清空对象引用再关闭底层资源，避免 close 异常留下“似乎仍连接”的状态。"""

        transport = self.transport
        self.transport = None
        self.identity = ""
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _require_ready(self) -> None:
        """Measure/Run 只能使用完整 Apply 且仍连接的设置。"""

        if (
            self.transport is None
            or not self.applied_settings
        ):
            raise ModuleError(
                "Apply Settings and confirm the Model 372 "
                "connection before running Measure",
                "LS372_SETTINGS_NOT_APPLIED",
            )

    @classmethod
    def _normalized_settings(
        cls,
        settings: Mapping[str, Any],
        *,
        require_resource: bool,
        operation_timeout_seconds: float,
    ) -> dict[str, Any]:
        """把保存文件或 UI 提供的不可信设置规范化为严格、可发送的副本。

        资源名限制为单行 GPIB 地址以阻止命令/日志注入；所有数值按手册和 UI 边界再次
        校验；R1-R4 的物理输入必须互不重复且至少启用一个。最后把 Pause、Dwell、I/O
        重试的保守时间预算与核心单次操作总超时关联，避免已知必超时的配置进入 Apply。
        """

        defaults = default_settings()
        raw = dict(settings)
        result = {
            key: raw.get(key, value)
            for key, value in defaults.items()
            if key != "channels"
        }
        resource = str(result["resource"]).strip()
        if (
            len(resource) > 255
            or "\r" in resource
            or "\n" in resource
        ):
            raise ModuleError(
                "GPIB resource must be one line with at most "
                "255 characters",
                "LS372_INVALID_SETTINGS",
                "resource",
            )
        if require_resource and not resource:
            raise ModuleError(
                "Select a GPIB resource before Apply Settings",
                "LS372_INVALID_SETTINGS",
                "resource",
            )
        if (
            resource
            and not resource.upper().startswith("GPIB")
        ):
            raise ModuleError(
                "Lake Shore 372A resource must be a GPIB "
                "VISA resource",
                "LS372_INVALID_SETTINGS",
                resource,
            )
        result["resource"] = resource
        result["frequency_index"] = cls._integer(
            result["frequency_index"],
            1,
            5,
            "frequency_index",
        )
        result["pause_seconds"] = cls._integer(
            result["pause_seconds"],
            3,
            200,
            "pause_seconds",
        )
        result["dwell_seconds"] = cls._integer(
            result["dwell_seconds"],
            1,
            200,
            "dwell_seconds",
        )
        result["filter_enabled"] = cls._boolean(
            result["filter_enabled"],
            "filter_enabled",
        )
        result["filter_settle_seconds"] = (
            cls._integer(
                result["filter_settle_seconds"],
                1,
                200,
                "filter_settle_seconds",
            )
        )
        result["filter_window_percent"] = (
            cls._integer(
                result["filter_window_percent"],
                1,
                80,
                "filter_window_percent",
            )
        )
        result["io_timeout_seconds"] = cls._number(
            result["io_timeout_seconds"],
            0.1,
            30.0,
            "io_timeout_seconds",
        )
        result["retry_attempts"] = cls._integer(
            result["retry_attempts"],
            1,
            5,
            "retry_attempts",
        )
        result["shunt_after_read"] = cls._boolean(
            result["shunt_after_read"],
            "shunt_after_read",
        )

        raw_channels = raw.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raise ModuleError(
                "channels must be a settings table",
                "LS372_INVALID_SETTINGS",
                "channels",
            )
        channels: dict[str, dict[str, Any]] = {}
        physical_channels: list[int] = []
        for slot in range(1, 5):
            key = f"r{slot}"
            default_channel = defaults["channels"][key]
            supplied = raw_channels.get(key, {})
            if not isinstance(supplied, Mapping):
                raise ModuleError(
                    f"{key} settings must be a table",
                    "LS372_INVALID_SETTINGS",
                    key,
                )
            channel = {
                name: supplied.get(name, value)
                for name, value
                in default_channel.items()
            }
            channel["enabled"] = cls._boolean(
                channel["enabled"],
                f"{key}.enabled",
            )
            channel["input_channel"] = cls._integer(
                channel["input_channel"],
                1,
                16,
                f"{key}.input_channel",
            )
            mode = str(
                channel["excitation_mode"]
            ).strip().casefold()
            if mode not in {"current", "voltage"}:
                raise ModuleError(
                    f"{key}.excitation_mode must be current "
                    "or voltage",
                    "LS372_INVALID_SETTINGS",
                    key,
                )
            channel["excitation_mode"] = mode
            maximum_excitation = (
                22 if mode == "current" else 12
            )
            channel["excitation_range"] = (
                cls._integer(
                    channel["excitation_range"],
                    1,
                    maximum_excitation,
                    f"{key}.excitation_range",
                )
            )
            channel["autorange"] = cls._integer(
                channel["autorange"],
                0,
                1,
                f"{key}.autorange",
            )
            channel["resistance_range"] = (
                cls._integer(
                    channel["resistance_range"],
                    1,
                    22,
                    f"{key}.resistance_range",
                )
            )
            channels[key] = channel
            physical_channels.append(
                channel["input_channel"]
            )
        if len(set(physical_channels)) != 4:
            raise ModuleError(
                "R1-R4 physical input selections must be "
                "unique",
                "LS372_INVALID_SETTINGS",
                "channels",
            )
        if not any(
            channel["enabled"]
            for channel in channels.values()
        ):
            raise ModuleError(
                "Enable at least one R1-R4 channel",
                "LS372_INVALID_SETTINGS",
                "channels",
            )
        result["channels"] = channels
        cls._validate_measure_duration(
            result,
            operation_timeout_seconds,
        )
        return result

    @staticmethod
    def _estimated_measure_seconds(
        settings: Mapping[str, Any],
    ) -> float:
        """保守估计一次 Measure 的总耗时，用于 Apply 前的超时安全检查。

        每个通道包含 Pause+Dwell 和约一秒命令开销，再加一次完整通信重试预算及固定清理
        余量；这不是进度条的精确预测，而是拒绝明显不可能在总截止时间内完成的配置。
        """

        enabled = sum(
            bool(channel["enabled"])
            for channel in settings[
                "channels"
            ].values()
        )
        return (
            enabled
            * (
                float(settings["pause_seconds"])
                + float(settings["dwell_seconds"])
                + 1.0
            )
            + float(settings["io_timeout_seconds"])
            * int(settings["retry_attempts"])
            + 5.0
        )

    @classmethod
    def _validate_measure_duration(
        cls,
        settings: Mapping[str, Any],
        operation_timeout_seconds: float,
    ) -> None:
        """要求估算时间至少比核心总截止时间短两秒，为 IPC 返回和分流清理留余量。"""

        estimate = cls._estimated_measure_seconds(
            settings
        )
        timeout = float(operation_timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or estimate > max(0.0, timeout - 2.0)
        ):
            raise ModuleError(
                f"Estimated Measure time {estimate:.1f} s "
                f"does not fit the core operation timeout "
                f"{timeout:.1f} s; shorten pause/dwell or "
                "increase [modules] "
                "operation_timeout_seconds and restart",
                "LS372_MEASURE_TIMEOUT_UNSAFE",
            )

    @staticmethod
    def _integer(
        value: Any,
        minimum: int,
        maximum: int,
        name: str,
    ) -> int:
        """读取有界整数；显式拒绝 Python 中同时属于 int 的 bool 和小数浮点。"""

        if isinstance(value, bool):
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "LS372_INVALID_SETTINGS",
                name,
            )
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "LS372_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            isinstance(value, float)
            and value != result
        ) or not minimum <= result <= maximum:
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "LS372_INVALID_SETTINGS",
                name,
            )
        return result

    @staticmethod
    def _number(
        value: Any,
        minimum: float,
        maximum: float,
        name: str,
    ) -> float:
        """读取有界有限浮点数；拒绝 bool、NaN 和无穷值。"""

        if isinstance(value, bool):
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "LS372_INVALID_SETTINGS",
                name,
            )
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "LS372_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            not math.isfinite(result)
            or not minimum <= result <= maximum
        ):
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "LS372_INVALID_SETTINGS",
                name,
            )
        return result

    @staticmethod
    def _boolean(
        value: Any,
        name: str,
    ) -> bool:
        """只接受真正的布尔值，不把 0/1 或任意非空字符串静默转换。"""

        if not isinstance(value, bool):
            raise ModuleError(
                f"{name} must be true or false",
                "LS372_INVALID_SETTINGS",
                name,
            )
        return value


__all__ = [
    "InstrumentTransport",
    "LakeShore372ABackend",
    "PyVisaTransport",
]
