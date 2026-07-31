"""Keithley 6221 + 2182A Delta 与可选 7001 的 Measurement Module 后端。

协议依据：

- 6221 用户手册第 5 节：``SOUR:DELT:*``、``INIT:IMM``、``TRAC:DATA?``；
- 2182A 由 6221 的 RS-232 转发口控制，不单独占用电脑的 VISA 资源；
- 7001 Quick Reference Guide：``ROUT:CLOS``、``ROUT:OPEN`` 与查询回读。

关键安全边界：

- Enable 不发送电流设置；只发现资源并探测可选 7001；
- Apply 前后均确认 6221 为零电流且输出关闭；
- 共享模式只在 ``begin_sequence`` ARM 一次，ARM 后等待至少 3 秒并查询确认；
- 独立模式每次切换前 Abort/Clear，切换后重新配置和 ARM；
- 7001 的任何运行期通信或回读错误立即中止，不自动重发切换/触发命令；
- compliance abort 和 cold switching 固定开启，通道设置必须落在仪表合法命令范围；
- Stop/Error/Disable/SEQ 完成均 Abort、清零、关闭输出并打开 7001 全部触点。

本模块仍是 Beta。自动化测试只能验证命令状态机，不能证明真实开关卡接线、6221 实际
零电流回读语义或 DUT 的功耗边界正确。
"""

from __future__ import annotations

import importlib
import math
import re
import statistics
import time
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
    ARM_SETTLE_SECONDS,
    DEVICE_COMPLIANCE_MAX_V,
    DEVICE_COMPLIANCE_MIN_V,
    DEVICE_CURRENT_LIMIT_A,
    FILTER_TYPES,
    MAX_DELTA_COUNT,
    MODE_INDEPENDENT,
    MODE_SHARED,
    STATUS_CODE_INVALID_TRACE,
    STATUS_CODE_NORMAL,
    STATUS_CODE_OVER_RANGE,
    VOLTAGE_RANGES,
    default_delta_settings,
    default_settings,
)
from .quantities import parse_quantity
from .routing import RoutingConfig, load_routing


class InstrumentTransport(Protocol):
    """后端使用的最小 VISA 接口，测试用内存替身也实现同一契约。"""

    def write(self, command: str) -> None: ...

    def query(
        self,
        command: str,
        timeout_seconds: float | None = None,
    ) -> str: ...

    def close(self) -> None: ...


TransportFactory = Callable[[str, float], InstrumentTransport]
ResourceLister = Callable[[], tuple[str, ...]]
Waiter = Callable[[ModuleOperationContext, float], None]

# 长时间 ``*OPC?`` 不再拥有可配置的单通道超时，而是共享本次 Measure 的核心总
# 预算。提前预留两秒，使通信错误仍有机会在核心强制终止 worker 前执行安全关闭。
_OPERATION_CLEANUP_RESERVE_SECONDS = 2.0


class PyVisaTransport:
    """惰性导入 PyVISA，并为单次长测量临时扩大读超时。"""

    def __init__(
        self,
        resource: str,
        timeout_seconds: float,
    ) -> None:
        pyvisa = importlib.import_module("pyvisa")
        self._manager = pyvisa.ResourceManager()
        self._instrument = self._manager.open_resource(resource)
        self._instrument.timeout = max(
            1,
            int(float(timeout_seconds) * 1000),
        )
        self._instrument.write_termination = "\n"
        self._instrument.read_termination = "\n"

    @staticmethod
    def list_resources() -> tuple[str, ...]:
        """列出当前 VISA 层能看到的 GPIB 资源。"""

        pyvisa = importlib.import_module("pyvisa")
        manager = pyvisa.ResourceManager()
        try:
            return tuple(
                str(item)
                for item in manager.list_resources()
                if str(item).upper().startswith("GPIB")
            )
        finally:
            manager.close()

    def write(self, command: str) -> None:
        self._instrument.write(command)

    def query(
        self,
        command: str,
        timeout_seconds: float | None = None,
    ) -> str:
        if timeout_seconds is None:
            return str(self._instrument.query(command))
        original = self._instrument.timeout
        self._instrument.timeout = max(
            1,
            int(float(timeout_seconds) * 1000),
        )
        try:
            return str(self._instrument.query(command))
        finally:
            self._instrument.timeout = original

    def close(self) -> None:
        try:
            self._instrument.close()
        finally:
            self._manager.close()


class Keithley6221DeltaBackend(ModuleBackend):
    """6221/2182A Delta 测量和四路 7001 路由状态机。"""

    _TRACE_TOKEN = re.compile(
        r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
        r"(?:[eE][+-]?\d+)?(?:\s*[vV])?$"
    )

    def __init__(
        self,
        *,
        transport_factory: TransportFactory | None = None,
        resource_lister: ResourceLister | None = None,
        waiter: Waiter | None = None,
        routing: RoutingConfig | None = None,
    ) -> None:
        self._transport_factory = (
            transport_factory or PyVisaTransport
        )
        self._resource_lister = (
            resource_lister or PyVisaTransport.list_resources
        )
        self._waiter = (
            waiter
            or (
                lambda context, seconds: context.interruptible_sleep(
                    seconds
                )
            )
        )
        try:
            self.routing = routing or load_routing()
        except ValueError as exc:
            raise ModuleError(
                str(exc),
                "K6221_ROUTING_INVALID",
                "routing.toml",
            ) from exc

        self.desired_settings: dict[str, Any] = (
            default_settings()
        )
        self.applied_settings: dict[str, Any] | None = None
        self.transport_6221: InstrumentTransport | None = None
        self.transport_7001: InstrumentTransport | None = None
        self.identity_6221 = ""
        self.identity_2182a = ""
        self.identity_7001 = ""
        self.switcher_available = False
        self.switcher_detection_complete = False
        self.sequence_active = False
        self.armed = False
        self.active_channel = ""
        self.last_resistance: float | None = None
        self.last_current: float | None = None
        self.last_stddev: float | None = None
        self.last_status = "Idle"

    def initialize(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """发现资源并只探测可选 7001，不连接或配置 6221 输出。"""

        self.desired_settings = self._normalized_settings(
            settings,
            require_6221=False,
            require_measurement=False,
            operation_timeout_seconds=(
                context.operation_timeout_seconds
            ),
        )
        resources: tuple[str, ...]
        try:
            resources = tuple(
                sorted(
                    set(self._resource_lister()),
                    key=str.casefold,
                )
            )
            context.resolve_warning(
                "K6221_RESOURCE_DISCOVERY_FAILED"
            )
        except Exception as exc:
            resources = ()
            context.warning(
                "GPIB resource discovery failed: "
                f"{type(exc).__name__}: {exc}",
                "K6221_RESOURCE_DISCOVERY_FAILED",
            )

        self._detect_switcher_on_enable(context)
        self.applied_settings = None
        self.sequence_active = False
        self.armed = False
        self.active_channel = ""
        self.last_status = "Initialized"
        status = self._status(
            available_resources=resources,
        )
        context.update_status(status)
        return status

    def apply_settings(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """连接、识别并写入一套安全的待 ARM 设置，但不开始输出。"""

        normalized = self._normalized_settings(
            settings,
            require_6221=True,
            require_measurement=True,
            operation_timeout_seconds=(
                context.operation_timeout_seconds
            ),
            ch1_only=not self.switcher_available,
        )
        self.desired_settings = deepcopy(normalized)
        try:
            self._connect_6221(normalized, context)
            if self.switcher_available:
                self._connect_7001(normalized, context)
            self._enter_safe_state(context)
            self._verify_2182a(context)
            first_channel = self._enabled_channels(
                normalized
            )[0]
            selected = self._channel_settings(
                normalized,
                first_channel,
            )
            self._configure_delta(selected, context)
            # Apply 结束时再次清零。配置命令本身不应打开输出，但这一确认可捕获
            # 前面遗留状态或仪表异常行为。
            self._enter_safe_state(context)
        except Exception:
            self._best_effort_safe_state(context)
            raise

        self.applied_settings = normalized
        self.last_status = "Settings applied - output off"
        status = self._status()
        context.update_status(status)
        return status

    def begin_sequence(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """在第一条 SEQ 指令前建立本次运行状态。

        共享模式在这里完成唯一一次 ARM；独立模式只确认安全状态，等每个通道路由
        闭合并应用自己的设置后再 ARM。
        """

        settings = self._require_applied()
        try:
            self._enter_safe_state(context)
            if settings["mode"] == MODE_SHARED:
                self._configure_delta(
                    settings["shared"],
                    context,
                )
                self._arm_delta(context)
            self.sequence_active = True
            self.last_status = (
                "Armed - waiting for software trigger"
                if self.armed
                else "Sequence ready"
            )
        except Exception:
            self.sequence_active = False
            self._best_effort_safe_state(context)
            raise
        status = self._status()
        context.update_status(status)
        return status

    def measure(
        self,
        context: ModuleOperationContext,
    ) -> None:
        """顺序测量全部有效通道；异常返回核心前先直接请求本模块安全关闭。

        核心随后仍会调用 ``end_sequence("error"|"stopped")`` 做严格确认。这里的
        best-effort 不是替代该生命周期，而是缩短 7001/6221 通信失败到 Abort、清零、
        打开触点之间的时间；协作 Stop 在 checkpoint 抛出的异常也走同一路径。
        """

        operation_deadline = (
            time.monotonic()
            + float(context.operation_timeout_seconds)
            - _OPERATION_CLEANUP_RESERVE_SECONDS
        )
        try:
            self._measure_impl(
                context,
                operation_deadline,
            )
        except Exception:
            self.sequence_active = False
            self.last_status = (
                "Measurement interrupted - safety shutdown requested"
            )
            self._best_effort_safe_state(context)
            raise

    def _measure_impl(
        self,
        context: ModuleOperationContext,
        operation_deadline: float,
    ) -> None:
        """实现正常测量路径，并逐通道产生一行 DAT 与 rawdata。"""

        settings = self._require_ready()
        channels = self._enabled_channels(settings)
        for channel in channels:
            context.checkpoint()
            selected = self._channel_settings(
                settings,
                channel,
            )
            if settings["mode"] == MODE_INDEPENDENT:
                self._enter_safe_source_state(context)
                self._switch_channel(
                    channel,
                    settings,
                    context,
                )
                self._configure_delta(selected, context)
                self._arm_delta(context)
            else:
                self._verify_armed(context)
                self._verify_zero_current(context)
                self._switch_channel(
                    channel,
                    settings,
                    context,
                )

            raw_values, issues, status_code = (
                self._trigger_and_read(
                    selected,
                    context,
                    operation_deadline,
                )
            )
            current = self._effective_current(selected)
            row: dict[str, Any] = {
                "Channel": int(channel[2:]),
                "Current": current,
                "SampleCount": len(raw_values),
                "StatusCode": status_code,
            }
            if issues:
                # “通道 Error”是数据状态，不是框架 Error 事件。以 Warning 报告后
                # SEQ 继续，且不把部分有效样本伪装成正式电阻。
                context.warning(
                    f"{channel.upper()} Delta readings are invalid: "
                    + "; ".join(issues),
                    "K6221_READING_WARNING",
                    channel,
                )
                self.last_resistance = None
                self.last_stddev = None
                self.last_status = (
                    "Voltage overrange"
                    if status_code
                    == STATUS_CODE_OVER_RANGE
                    else "Invalid Delta trace"
                )
            else:
                resistances = [
                    voltage / current
                    for voltage in raw_values
                ]
                mean = statistics.fmean(resistances)
                stddev = (
                    statistics.stdev(resistances)
                    if len(resistances) > 1
                    else 0.0
                )
                row.update(
                    {
                        "Resistance": mean,
                        "StdDev": stddev,
                    }
                )
                context.resolve_warning(
                    "K6221_READING_WARNING",
                    channel,
                )
                self.last_resistance = mean
                self.last_stddev = stddev
                self.last_status = "Normal"
            self.last_current = current
            self.active_channel = channel.upper()
            context.emit_row(
                row,
                raw_values=raw_values,
            )
            context.update_status(self._status())
            context.checkpoint()

    def end_sequence(
        self,
        reason: str,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """对 completed/stopped/error 使用相同的严格安全收尾。"""

        self.sequence_active = False
        self.armed = False
        try:
            self._enter_safe_state(context)
        finally:
            # 即使严格安全确认抛错，内部状态也不能继续显示 Running/Armed。
            self.sequence_active = False
            self.armed = False
            self.active_channel = ""
        self.last_status = f"Sequence {reason} - output off"
        status = self._status()
        context.update_status(status)
        return status

    def abort(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """Disable/退出时尽力安全关闭，再释放两个 VISA 会话。"""

        self.sequence_active = False
        self.armed = False
        failure: ModuleError | None = None
        try:
            self._enter_safe_state(context)
        except ModuleError as exc:
            failure = exc
        finally:
            self._close_transports()
            self.active_channel = ""
            self.applied_settings = None
        self.last_status = (
            "Disabled - safe state unconfirmed"
            if failure is not None
            else "Disabled"
        )
        status = self._status()
        context.update_status(status)
        if failure is not None:
            raise failure
        return status

    def read_status(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """只读刷新 Armed、输出和 7001 状态，不隐式 Apply。"""

        if self.transport_6221 is not None:
            armed = self._query_6221(
                "SOUR:DELT:ARM?",
                context,
            )
            self.armed = self._parse_switch(
                armed,
                "SOUR:DELT:ARM?",
            )
            output = self._parse_switch(
                self._query_6221("OUTP?", context),
                "OUTP?",
            )
            self.last_status = (
                "Armed" if self.armed else "Connected"
            ) + (" / Output on" if output else " / Output off")
        status = self._status()
        context.update_status(status)
        return status

    def manual_action(
        self,
        action: str,
        payload: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        """处理 Idle 时的资源刷新、连接测试和安全关闭。"""

        if action == "refresh_resources":
            try:
                resources = tuple(
                    sorted(
                        set(self._resource_lister()),
                        key=str.casefold,
                    )
                )
            except Exception as exc:
                raise ModuleWarning(
                    "GPIB resource discovery failed: "
                    f"{type(exc).__name__}: {exc}",
                    "K6221_RESOURCE_DISCOVERY_FAILED",
                ) from exc
            context.resolve_warning(
                "K6221_RESOURCE_DISCOVERY_FAILED"
            )
            status = self._status(
                available_resources=resources,
            )
            context.update_status(status)
            return status
        if action == "test_connection":
            candidate = payload.get(
                "settings",
                self.desired_settings,
            )
            if not isinstance(candidate, Mapping):
                raise ModuleError(
                    "Test Connections settings payload must "
                    "be a mapping",
                    "K6221_INVALID_SETTINGS",
                    "settings",
                )
            settings = self._normalized_settings(
                candidate,
                require_6221=True,
                require_measurement=False,
                operation_timeout_seconds=(
                    context.operation_timeout_seconds
                ),
            )
            self._test_connections(settings, context)
            status = self._status()
            context.update_status(status)
            return status
        if action == "safe_off":
            self._enter_safe_state(context)
            self.last_status = "Output off / all routes open"
            status = self._status()
            context.update_status(status)
            return status
        return (
            super().manual_action(
                action,
                payload,
                context,
            )
            or {}
        )

    def _detect_switcher_on_enable(
        self,
        context: ModuleOperationContext,
    ) -> None:
        """7001 缺失只在 Enable 阶段降级为 CH1-only。"""

        resource = str(
            self.desired_settings["resource_7001"]
        ).strip()
        self.switcher_detection_complete = True
        self.switcher_available = False
        self.identity_7001 = ""
        if not resource:
            context.resolve_warning(
                "K6221_SWITCHER_UNAVAILABLE"
            )
            return
        transport: InstrumentTransport | None = None
        try:
            transport = self._transport_factory(
                resource,
                float(
                    self.desired_settings[
                        "io_timeout_seconds"
                    ]
                ),
            )
            identity = transport.query("*IDN?").strip()
            self._validate_identity(
                identity,
                "7001",
                "Keithley 7001",
            )
        except Exception as exc:
            context.warning(
                "Keithley 7001 was not available during Enable; "
                "the module will measure CH1 only until it is "
                "Disabled and Enabled again: "
                f"{type(exc).__name__}: {exc}",
                "K6221_SWITCHER_UNAVAILABLE",
                resource,
            )
            return
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
        self.switcher_available = True
        self.identity_7001 = identity
        context.resolve_warning(
            "K6221_SWITCHER_UNAVAILABLE",
            resource,
        )

    def _test_connections(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        """临时连接并识别仪表，不改变当前 Apply/Armed 状态。"""

        test_6221: InstrumentTransport | None = None
        test_7001: InstrumentTransport | None = None
        try:
            test_6221 = self._transport_factory(
                str(settings["resource_6221"]),
                float(settings["io_timeout_seconds"]),
            )
            identity_6221 = test_6221.query("*IDN?").strip()
            self._validate_identity(
                identity_6221,
                "6221",
                "Keithley 6221",
            )
            self.identity_6221 = identity_6221
            if str(settings["resource_7001"]).strip():
                test_7001 = self._transport_factory(
                    str(settings["resource_7001"]),
                    float(settings["io_timeout_seconds"]),
                )
                identity_7001 = test_7001.query("*IDN?").strip()
                self._validate_identity(
                    identity_7001,
                    "7001",
                    "Keithley 7001",
                )
                self.identity_7001 = identity_7001
            context.resolve_warning(
                "K6221_CONNECTION_TEST_FAILED"
            )
            self.last_status = "Connection test passed"
        except Exception as exc:
            raise ModuleWarning(
                "Connection test failed: "
                f"{type(exc).__name__}: {exc}",
                "K6221_CONNECTION_TEST_FAILED",
            ) from exc
        finally:
            for transport in (test_7001, test_6221):
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass

    def _connect_6221(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        self._close_transport_6221()
        try:
            self.transport_6221 = self._transport_factory(
                str(settings["resource_6221"]),
                float(settings["io_timeout_seconds"]),
            )
            identity = self.transport_6221.query(
                "*IDN?"
            ).strip()
            self._validate_identity(
                identity,
                "6221",
                "Keithley 6221",
            )
        except ModuleError:
            self._close_transport_6221()
            raise
        except Exception as exc:
            self._close_transport_6221()
            raise ModuleError(
                "Unable to connect to Keithley 6221: "
                f"{type(exc).__name__}: {exc}",
                "K6221_CONNECTION_FAILED",
                str(settings["resource_6221"]),
            ) from exc
        self.identity_6221 = identity
        context.checkpoint()

    def _connect_7001(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        """初始化时存在的 7001 此后失败即为 Error，不再静默降级。"""

        self._close_transport_7001()
        resource = str(settings["resource_7001"])
        try:
            self.transport_7001 = self._transport_factory(
                resource,
                float(settings["io_timeout_seconds"]),
            )
            identity = self.transport_7001.query(
                "*IDN?"
            ).strip()
            self._validate_identity(
                identity,
                "7001",
                "Keithley 7001",
            )
        except ModuleError:
            self._close_transport_7001()
            raise
        except Exception as exc:
            self._close_transport_7001()
            raise ModuleError(
                "Keithley 7001 was present during Enable but "
                "could not be connected for Apply",
                "K6221_SWITCHER_CONNECTION_FAILED",
                resource,
            ) from exc
        self.identity_7001 = identity
        context.checkpoint()

    @staticmethod
    def _validate_identity(
        identity: str,
        model: str,
        label: str,
    ) -> None:
        normalized = identity.upper()
        if "KEITHLEY" not in normalized or model not in normalized:
            raise ModuleError(
                f"Expected {label}, received {identity!r}",
                "K6221_IDENTITY_MISMATCH",
                identity,
            )

    def _verify_2182a(
        self,
        context: ModuleOperationContext,
    ) -> None:
        present = self._query_6221(
            "SOUR:DELT:NVPRESENT?",
            context,
        )
        if not self._parse_switch(
            present,
            "SOUR:DELT:NVPRESENT?",
        ):
            raise ModuleError(
                "6221 does not report a compatible 2182A on "
                "its RS-232 connection",
                "K6221_2182A_NOT_PRESENT",
            )
        identity = self._serial_query(
            "*IDN?",
            context,
        )
        normalized = identity.upper()
        if "KEITHLEY" not in normalized or "2182A" not in normalized:
            raise ModuleError(
                "Expected Keithley 2182A through the 6221 "
                f"serial link, received {identity!r}",
                "K6221_2182A_IDENTITY_MISMATCH",
                identity,
            )
        self.identity_2182a = identity

    def _configure_delta(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        """写入并逐项回读一套 Delta/2182A 设置；不 ARM。"""

        high = float(settings["high_current"])
        low = float(settings["low_current"])
        compliance = float(settings["compliance"])
        delay = float(settings["delta_delay"])
        count = int(settings["count"])

        # 明确使用预数学电压，软件再按有效反转电流换算电阻。
        for command in (
            "UNIT V",
            "FORM:DATA ASC",
            "FORM:ELEM READ",
            "SENS:AVER:STAT OFF",
            f"SOUR:CURR:COMP {self._scpi_number(compliance)}",
            f"SOUR:DELT:HIGH {self._scpi_number(high)}",
            f"SOUR:DELT:LOW {self._scpi_number(low)}",
            f"SOUR:DELT:DEL {self._scpi_number(delay)}",
            f"SOUR:DELT:COUN {count}",
            "SOUR:SWE:COUN 1",
            "SOUR:DELT:CSW ON",
            "SOUR:DELT:CAB ON",
            f"TRAC:POIN {count}",
        ):
            self._write_6221(command, context)

        self._configure_2182a(settings, context)
        self._expect_float(
            "SOUR:CURR:COMP?",
            compliance,
            context,
        )
        self._expect_float(
            "SOUR:DELT:HIGH?",
            high,
            context,
        )
        self._expect_float(
            "SOUR:DELT:LOW?",
            low,
            context,
        )
        self._expect_float(
            "SOUR:DELT:DEL?",
            delay,
            context,
        )
        self._expect_integer(
            "SOUR:DELT:COUN?",
            count,
            context,
        )
        for query in (
            "SOUR:DELT:CSW?",
            "SOUR:DELT:CAB?",
        ):
            if not self._parse_switch(
                self._query_6221(query, context),
                query,
            ):
                raise ModuleError(
                    f"{query} did not read back ON",
                    "K6221_SETTINGS_VERIFY_FAILED",
                    query,
                )
        self._raise_if_instrument_error(context)

    def _configure_2182a(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        voltage_range = str(settings["voltage_range"])
        ranges = {
            key: value
            for key, _label, value in VOLTAGE_RANGES
        }
        if voltage_range == "auto":
            self._serial_write(
                "VOLT:RANG:AUTO ON",
                context,
            )
            self._expect_serial_switch(
                "VOLT:RANG:AUTO?",
                True,
                context,
            )
        else:
            value = ranges[voltage_range]
            assert value is not None
            self._serial_write(
                "VOLT:RANG:AUTO OFF",
                context,
            )
            self._serial_write(
                f"VOLT:RANG {self._scpi_number(value)}",
                context,
            )
            self._expect_serial_switch(
                "VOLT:RANG:AUTO?",
                False,
                context,
            )
            actual = self._parse_float(
                self._serial_query(
                    "VOLT:RANG?",
                    context,
                ),
                "VOLT:RANG?",
            )
            if not math.isclose(
                actual,
                value,
                rel_tol=1.0e-6,
                abs_tol=1.0e-12,
            ):
                raise ModuleError(
                    "2182A voltage range readback does not "
                    "match the requested setting",
                    "K6221_SETTINGS_VERIFY_FAILED",
                    "VOLT:RANG",
                )

        nplc = int(settings["nplc"])
        self._serial_write(
            f"VOLT:NPLC {nplc}",
            context,
        )
        actual_nplc = self._parse_float(
            self._serial_query("VOLT:NPLC?", context),
            "VOLT:NPLC?",
        )
        if not math.isclose(
            actual_nplc,
            float(nplc),
            rel_tol=0.0,
            abs_tol=1.0e-9,
        ):
            raise ModuleError(
                "2182A NPLC readback does not match",
                "K6221_SETTINGS_VERIFY_FAILED",
                "VOLT:NPLC",
            )

        analog = bool(settings["analog_filter_enabled"])
        digital = bool(
            settings["digital_filter_enabled"]
        )
        self._serial_write(
            f"VOLT:LPAS {'ON' if analog else 'OFF'}",
            context,
        )
        self._serial_write(
            "VOLT:DFIL:TCON MOV",
            context,
        )
        self._serial_write(
            "VOLT:DFIL:COUN "
            f"{int(settings['digital_filter_count'])}",
            context,
        )
        self._serial_write(
            "VOLT:DFIL:WIND "
            + self._scpi_number(
                float(
                    settings[
                        "digital_filter_window_percent"
                    ]
                )
            ),
            context,
        )
        self._serial_write(
            f"VOLT:DFIL:STAT {'ON' if digital else 'OFF'}",
            context,
        )
        self._expect_serial_switch(
            "VOLT:LPAS?",
            analog,
            context,
        )
        self._expect_serial_switch(
            "VOLT:DFIL:STAT?",
            digital,
            context,
        )

    def _arm_delta(
        self,
        context: ModuleOperationContext,
    ) -> None:
        """发送 ARM，等待用户指定的 3 秒，再查询确认。"""

        self._write_6221("SOUR:DELT:ARM", context)
        self.armed = False
        self.last_status = "Arming"
        context.update_status(self._status())
        self._waiter(context, ARM_SETTLE_SECONDS)
        self._verify_armed(context)
        self._raise_if_instrument_error(context)
        self.last_status = "Armed - waiting for software trigger"
        context.update_status(self._status())

    def _verify_armed(
        self,
        context: ModuleOperationContext,
    ) -> None:
        reply = self._query_6221(
            "SOUR:DELT:ARM?",
            context,
        )
        self.armed = self._parse_switch(
            reply,
            "SOUR:DELT:ARM?",
        )
        if not self.armed:
            raise ModuleError(
                "6221 did not enter Delta Armed state",
                "K6221_ARM_FAILED",
            )

    def _trigger_and_read(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
        operation_deadline: float,
    ) -> tuple[
        tuple[float, ...],
        tuple[str, ...],
        int,
    ]:
        """软件触发有限 Delta 采集并读取预数学电压缓冲。"""

        self._verify_armed(context)
        self._write_6221("INIT:IMM", context)
        completion_timeout = (
            operation_deadline - time.monotonic()
        )
        if (
            not math.isfinite(completion_timeout)
            or completion_timeout <= 0
        ):
            raise ModuleError(
                "The core Measure operation has no time left "
                "for Delta acquisition",
                "K6221_MEASURE_TIMEOUT_UNSAFE",
            )
        completion = self._query_6221(
            "*OPC?",
            context,
            timeout_seconds=completion_timeout,
        )
        if completion.strip() not in {"1", "+1"}:
            raise ModuleError(
                f"6221 returned unexpected completion state "
                f"{completion!r}",
                "K6221_MEASUREMENT_NOT_COMPLETE",
            )
        trace = self._query_6221(
            "TRAC:DATA?",
            context,
        )
        self._raise_if_instrument_error(context)
        self._verify_armed(context)
        self._verify_zero_current(context)
        return self._parse_trace(
            trace,
            int(settings["count"]),
        )

    def _parse_trace(
        self,
        reply: str,
        expected_count: int,
    ) -> tuple[
        tuple[float, ...],
        tuple[str, ...],
        int,
    ]:
        """解析纯 READ 格式，并产生本模块自己的整数数据质量码。"""

        stripped = reply.strip()
        tokens = (
            re.split(r"[,;\r\n]+", stripped)
            if stripped
            else []
        )
        values: list[float] = []
        issues: list[str] = []
        invalid_trace = False
        over_range = False
        for index, token in enumerate(tokens, start=1):
            item = token.strip()
            if self._TRACE_TOKEN.fullmatch(item) is None:
                invalid_trace = True
                issues.append(
                    f"sample {index} is not numeric ({item!r})"
                )
                continue
            try:
                value = float(
                    re.sub(r"[vV]\s*$", "", item).strip()
                )
            except ValueError:
                invalid_trace = True
                issues.append(
                    f"sample {index} cannot be parsed"
                )
                continue
            if not math.isfinite(value):
                invalid_trace = True
                issues.append(
                    f"sample {index} is not finite"
                )
                continue
            values.append(value)
            # 2182A DCV1 最大 100 V 量程允许约 20% overrange；超过 120 V 的
            # 数字通常是 overflow sentinel 或格式错位，仍原样写 rawdata。
            if abs(value) > 120.0:
                over_range = True
                issues.append(
                    f"sample {index} exceeds the 2182A range"
                )
        if len(tokens) != expected_count:
            invalid_trace = True
            issues.append(
                f"expected {expected_count} samples, "
                f"received {len(tokens)}"
            )
        if len(values) != len(tokens):
            invalid_trace = True
            issues.append(
                f"only {len(values)} samples were numeric"
            )
        return (
            tuple(values),
            tuple(dict.fromkeys(issues)),
            (
                STATUS_CODE_INVALID_TRACE
                if invalid_trace
                else (
                    STATUS_CODE_OVER_RANGE
                    if over_range
                    else STATUS_CODE_NORMAL
                )
            ),
        )

    def _switch_channel(
        self,
        channel: str,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        if not self.switcher_available:
            if channel != "ch1":
                raise ModuleError(
                    "7001 is unavailable; only CH1 may be measured",
                    "K6221_SWITCHER_UNAVAILABLE",
                    channel,
                )
            self.active_channel = "CH1"
            return
        if self.transport_7001 is None:
            raise ModuleError(
                "7001 transport is not connected",
                "K6221_SWITCHER_NOT_CONNECTED",
            )

        # Break-before-make：先打开全部触点并查询所有配置路由，再闭合目标四路并
        # 逐项确认。任何不确定状态都在触发电流前成为 Error。
        self._write_7001("ROUT:OPEN ALL", context)
        opened = self._query_7001(
            "ROUT:OPEN? " + self.routing.all_list_text,
            context,
        )
        self._expect_route_states(
            opened,
            len(self.routing.all_routes),
            expected=True,
            command="ROUT:OPEN?",
        )
        target = self.routing.list_text(channel)
        self._write_7001(
            "ROUT:CLOS " + target,
            context,
        )
        closed = self._query_7001(
            "ROUT:CLOS? " + target,
            context,
        )
        self._expect_route_states(
            closed,
            len(self.routing.channels[channel]),
            expected=True,
            command="ROUT:CLOS?",
        )
        self.active_channel = channel.upper()
        context.update_status(self._status())
        self._waiter(
            context,
            float(settings["switch_settle_seconds"]),
        )

    @staticmethod
    def _expect_route_states(
        reply: str,
        count: int,
        *,
        expected: bool,
        command: str,
    ) -> None:
        tokens = [
            item.strip()
            for item in re.split(r"[,;\s]+", reply.strip())
            if item.strip()
        ]
        wanted = "1" if expected else "0"
        if len(tokens) != count or any(
            token not in {wanted, f"+{wanted}"}
            for token in tokens
        ):
            raise ModuleError(
                f"{command} returned {reply!r}; expected "
                f"{count} confirmed states of {wanted}",
                "K6221_SWITCHER_VERIFY_FAILED",
                command,
            )

    def _enter_safe_state(
        self,
        context: ModuleOperationContext,
    ) -> None:
        """严格 Abort/Clear/输出关闭并打开全部路由。"""

        # Enable 后尚未 Apply 时没有打开任何仪表会话，也没有由本 worker 产生的
        # 输出或路由需要确认。此路径允许用户直接 Disable，而不会把“未连接”误报
        # 成“安全关闭失败”。
        if (
            self.transport_6221 is None
            and self.transport_7001 is None
        ):
            self.armed = False
            self.active_channel = ""
            return
        self._enter_safe_source_state(context)
        if self.switcher_available:
            if self.transport_7001 is None:
                raise ModuleError(
                    "Cannot confirm 7001 routes are open because "
                    "the switcher is not connected",
                    "K6221_SAFE_STATE_FAILED",
                    "7001",
                )
            self._write_7001("ROUT:OPEN ALL", context)
            reply = self._query_7001(
                "ROUT:OPEN? " + self.routing.all_list_text,
                context,
            )
            self._expect_route_states(
                reply,
                len(self.routing.all_routes),
                expected=True,
                command="ROUT:OPEN?",
            )
        self.active_channel = ""

    def _enter_safe_source_state(
        self,
        context: ModuleOperationContext,
    ) -> None:
        if self.transport_6221 is None:
            self.armed = False
            return
        self._write_6221("SOUR:SWE:ABOR", context)
        self._write_6221("SOUR:CLE", context)
        output = self._parse_switch(
            self._query_6221("OUTP?", context),
            "OUTP?",
        )
        current = self._parse_float(
            self._query_6221("SOUR:CURR?", context),
            "SOUR:CURR?",
        )
        self.armed = False
        if output or not self._is_zero_current(current):
            raise ModuleError(
                "6221 output-off/zero-current state could not "
                "be confirmed",
                "K6221_SAFE_STATE_FAILED",
                "6221",
            )

    def _verify_zero_current(
        self,
        context: ModuleOperationContext,
    ) -> None:
        current = self._parse_float(
            self._query_6221(
                "SOUR:CURR?",
                context,
            ),
            "SOUR:CURR?",
        )
        if not self._is_zero_current(current):
            raise ModuleError(
                "6221 did not return to zero current before "
                "a route change",
                "K6221_NONZERO_DURING_SWITCH",
                self.active_channel,
            )

    @staticmethod
    def _is_zero_current(value: float) -> bool:
        # 2 nA range的编程分辨率为 100 fA；允许一个最小步进的回读误差。
        return abs(value) <= 100.0e-15

    def _best_effort_safe_state(
        self,
        context: ModuleOperationContext,
    ) -> None:
        """异常路径不掩盖原始错误，但尽可能执行安全命令。"""

        if self.transport_6221 is not None:
            for command in (
                "SOUR:SWE:ABOR",
                "SOUR:CLE",
            ):
                try:
                    self.transport_6221.write(command)
                except Exception:
                    pass
        if (
            self.switcher_available
            and self.transport_7001 is not None
        ):
            try:
                self.transport_7001.write("ROUT:OPEN ALL")
            except Exception:
                pass
        self.armed = False
        self.active_channel = ""
        try:
            context.update_status(self._status())
        except Exception:
            pass

    def _write_6221(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> None:
        transport = self.transport_6221
        if transport is None:
            raise ModuleError(
                "6221 is not connected",
                "K6221_NOT_CONNECTED",
            )
        context.checkpoint()
        try:
            transport.write(command)
        except Exception as exc:
            raise ModuleError(
                f"6221 command failed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "K6221_COMMUNICATION_FAILED",
                command,
            ) from exc
        context.checkpoint()

    def _query_6221(
        self,
        command: str,
        context: ModuleOperationContext,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        transport = self.transport_6221
        if transport is None:
            raise ModuleError(
                "6221 is not connected",
                "K6221_NOT_CONNECTED",
            )
        context.checkpoint()
        try:
            result = transport.query(
                command,
                timeout_seconds,
            )
        except Exception as exc:
            raise ModuleError(
                f"6221 query failed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "K6221_COMMUNICATION_FAILED",
                command,
            ) from exc
        context.checkpoint()
        return str(result).strip()

    def _write_7001(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> None:
        transport = self.transport_7001
        if transport is None:
            raise ModuleError(
                "7001 is not connected",
                "K6221_SWITCHER_NOT_CONNECTED",
            )
        context.checkpoint()
        try:
            transport.write(command)
        except Exception as exc:
            raise ModuleError(
                f"7001 command failed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "K6221_SWITCHER_COMMUNICATION_FAILED",
                command,
            ) from exc
        context.checkpoint()

    def _query_7001(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> str:
        transport = self.transport_7001
        if transport is None:
            raise ModuleError(
                "7001 is not connected",
                "K6221_SWITCHER_NOT_CONNECTED",
            )
        context.checkpoint()
        try:
            result = transport.query(command)
        except Exception as exc:
            raise ModuleError(
                f"7001 query failed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "K6221_SWITCHER_COMMUNICATION_FAILED",
                command,
            ) from exc
        context.checkpoint()
        return str(result).strip()

    def _serial_write(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> None:
        escaped = command.replace('"', '""')
        self._write_6221(
            f'SYST:COMM:SER:SEND "{escaped}"',
            context,
        )

    def _serial_query(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> str:
        self._serial_write(command, context)
        return self._query_6221(
            "SYST:COMM:SER:ENT?",
            context,
        )

    def _raise_if_instrument_error(
        self,
        context: ModuleOperationContext,
    ) -> None:
        reply = self._query_6221("SYST:ERR?", context)
        matched = re.match(
            r"^\s*([+-]?\d+)\s*(?:,|$)",
            reply,
        )
        if matched is None:
            raise ModuleError(
                f"6221 returned invalid error status {reply!r}",
                "K6221_INVALID_REPLY",
                "SYST:ERR?",
            )
        if int(matched.group(1)) != 0:
            raise ModuleError(
                f"6221 reported an instrument error: {reply}",
                "K6221_INSTRUMENT_ERROR",
                reply,
            )

    def _expect_float(
        self,
        query: str,
        expected: float,
        context: ModuleOperationContext,
    ) -> None:
        actual = self._parse_float(
            self._query_6221(query, context),
            query,
        )
        tolerance = max(
            1.0e-15,
            abs(expected) * 1.0e-8,
        )
        if not math.isclose(
            actual,
            expected,
            rel_tol=1.0e-8,
            abs_tol=tolerance,
        ):
            raise ModuleError(
                f"{query} returned {actual:g}, expected "
                f"{expected:g}",
                "K6221_SETTINGS_VERIFY_FAILED",
                query,
            )

    def _expect_integer(
        self,
        query: str,
        expected: int,
        context: ModuleOperationContext,
    ) -> None:
        actual = self._parse_float(
            self._query_6221(query, context),
            query,
        )
        if not math.isclose(
            actual,
            float(expected),
            rel_tol=0.0,
            abs_tol=1.0e-9,
        ):
            raise ModuleError(
                f"{query} returned {actual:g}, expected "
                f"{expected}",
                "K6221_SETTINGS_VERIFY_FAILED",
                query,
            )

    def _expect_serial_switch(
        self,
        query: str,
        expected: bool,
        context: ModuleOperationContext,
    ) -> None:
        actual = self._parse_switch(
            self._serial_query(query, context),
            query,
        )
        if actual is not expected:
            raise ModuleError(
                f"2182A {query} readback does not match",
                "K6221_SETTINGS_VERIFY_FAILED",
                query,
            )

    @staticmethod
    def _parse_switch(
        reply: str,
        command: str,
    ) -> bool:
        normalized = reply.strip().upper()
        if normalized in {"1", "+1", "ON"}:
            return True
        if normalized in {"0", "+0", "OFF"}:
            return False
        raise ModuleError(
            f"{command} returned invalid state {reply!r}",
            "K6221_INVALID_REPLY",
            command,
        )

    @staticmethod
    def _parse_float(
        reply: str,
        command: str,
    ) -> float:
        token = reply.strip().split(",", 1)[0].strip()
        try:
            result = float(token)
        except ValueError as exc:
            raise ModuleError(
                f"{command} returned invalid number {reply!r}",
                "K6221_INVALID_REPLY",
                command,
            ) from exc
        if not math.isfinite(result):
            raise ModuleError(
                f"{command} returned a non-finite number",
                "K6221_INVALID_REPLY",
                command,
            )
        return result

    @staticmethod
    def _scpi_number(value: float) -> str:
        return f"{float(value):.12g}"

    @staticmethod
    def _effective_current(
        settings: Mapping[str, Any],
    ) -> float:
        # 6221 的 Delta 电压对应高、低电流差的一半；对称 +I/-I 时正好等于 I。
        return (
            float(settings["high_current"])
            - float(settings["low_current"])
        ) / 2.0

    def _enabled_channels(
        self,
        settings: Mapping[str, Any],
    ) -> list[str]:
        if not self.switcher_available:
            if not bool(settings["channels"]["ch1"]["enabled"]):
                raise ModuleError(
                    "Enable CH1 when 7001 is unavailable",
                    "K6221_INVALID_SETTINGS",
                    "channels.ch1.enabled",
                )
            return ["ch1"]
        channels = [
            key
            for key, values in settings["channels"].items()
            if bool(values["enabled"])
        ]
        if not channels:
            raise ModuleError(
                "Enable at least one channel",
                "K6221_INVALID_SETTINGS",
                "channels",
            )
        return channels

    @staticmethod
    def _channel_settings(
        settings: Mapping[str, Any],
        channel: str,
    ) -> Mapping[str, Any]:
        if settings["mode"] == MODE_SHARED:
            return settings["shared"]
        return settings["independent"][channel]

    def _require_applied(self) -> dict[str, Any]:
        if self.applied_settings is None:
            raise ModuleError(
                "Apply Settings before starting a SEQ",
                "K6221_SETTINGS_NOT_APPLIED",
            )
        if self.transport_6221 is None:
            raise ModuleError(
                "6221 transport is not connected",
                "K6221_NOT_CONNECTED",
            )
        if (
            self.switcher_available
            and self.transport_7001 is None
        ):
            raise ModuleError(
                "7001 transport is not connected",
                "K6221_SWITCHER_NOT_CONNECTED",
            )
        return self.applied_settings

    def _require_ready(self) -> dict[str, Any]:
        settings = self._require_applied()
        if not self.sequence_active:
            raise ModuleError(
                "The module sequence lifecycle has not started",
                "K6221_SEQUENCE_NOT_ACTIVE",
            )
        return settings

    def _status(
        self,
        *,
        available_resources: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        status: dict[str, Any] = {
            "State": self.last_status,
            "6221": self.identity_6221 or "Not connected",
            "2182A": self.identity_2182a or "Not verified",
            "7001": (
                self.identity_7001
                if self.switcher_available
                else "Unavailable - CH1 only"
            ),
            "Armed": self.armed,
            "Sequence Active": self.sequence_active,
            "Active Channel": self.active_channel or "-",
            "Routing Config": str(
                self.routing.source_path
            ),
            "ARM Wait": f"{ARM_SETTLE_SECONDS:g} s",
        }
        if self.last_resistance is not None:
            status["Last Resistance (Ohm)"] = (
                self.last_resistance
            )
        if self.last_current is not None:
            status["Last Current (A)"] = self.last_current
        if self.last_stddev is not None:
            status["Last StdDev (Ohm)"] = self.last_stddev
        if available_resources is not None:
            status["Available GPIB Resources"] = list(
                available_resources
            )
        return status

    def _close_transport_6221(self) -> None:
        if self.transport_6221 is not None:
            try:
                self.transport_6221.close()
            finally:
                self.transport_6221 = None

    def _close_transport_7001(self) -> None:
        if self.transport_7001 is not None:
            try:
                self.transport_7001.close()
            finally:
                self.transport_7001 = None

    def _close_transports(self) -> None:
        first_error: Exception | None = None
        for closer in (
            self._close_transport_7001,
            self._close_transport_6221,
        ):
            try:
                closer()
            except Exception as exc:
                first_error = first_error or exc
        # close 方法异常时也必须丢弃本地引用；不能让后续 UI 误以为还能复用一个
        # 已经部分关闭、状态未知的 VISA 会话。
        self.transport_7001 = None
        self.transport_6221 = None
        if first_error is not None:
            raise ModuleError(
                "A VISA resource could not be released: "
                f"{type(first_error).__name__}: {first_error}",
                "K6221_RESOURCE_RELEASE_FAILED",
            ) from first_error

    def _normalized_settings(
        self,
        raw: Mapping[str, Any],
        *,
        require_6221: bool,
        require_measurement: bool,
        operation_timeout_seconds: float,
        ch1_only: bool = False,
    ) -> dict[str, Any]:
        """合并默认值并在 worker 内重做全部边界验证。"""

        if not isinstance(raw, Mapping):
            raise ModuleError(
                "Module settings must be a table",
                "K6221_INVALID_SETTINGS",
            )
        defaults = default_settings()
        result: dict[str, Any] = {}
        for key in (
            "resource_6221",
            "resource_7001",
        ):
            value = str(
                raw.get(key, defaults[key])
            ).strip()
            if value and not value.upper().startswith(
                "GPIB"
            ):
                raise ModuleError(
                    f"{key} must be a GPIB VISA resource",
                    "K6221_INVALID_SETTINGS",
                    key,
                )
            result[key] = value
        if require_6221 and not result["resource_6221"]:
            raise ModuleError(
                "Select the Keithley 6221 GPIB resource",
                "K6221_INVALID_SETTINGS",
                "resource_6221",
            )

        mode = str(
            raw.get("mode", defaults["mode"])
        ).strip()
        if mode not in {
            MODE_SHARED,
            MODE_INDEPENDENT,
        }:
            raise ModuleError(
                "mode must be shared_armed or "
                "independent_rearm",
                "K6221_INVALID_SETTINGS",
                "mode",
            )
        result["mode"] = mode
        result["io_timeout_seconds"] = self._number(
            raw.get(
                "io_timeout_seconds",
                defaults["io_timeout_seconds"],
            ),
            0.1,
            30.0,
            "io_timeout_seconds",
        )
        result["switch_settle_seconds"] = self._number(
            raw.get(
                "switch_settle_seconds",
                defaults["switch_settle_seconds"],
            ),
            0.0,
            300.0,
            "switch_settle_seconds",
        )
        raw_channels = raw.get(
            "channels",
            defaults["channels"],
        )
        if not isinstance(raw_channels, Mapping):
            raise ModuleError(
                "channels must be a table",
                "K6221_INVALID_SETTINGS",
                "channels",
            )
        result["channels"] = {}
        for index in range(1, 5):
            key = f"ch{index}"
            supplied = raw_channels.get(key, {})
            if not isinstance(supplied, Mapping):
                raise ModuleError(
                    f"{key} settings must be a table",
                    "K6221_INVALID_SETTINGS",
                    f"channels.{key}",
                )
            result["channels"][key] = {
                "enabled": self._boolean(
                    supplied.get(
                        "enabled",
                        defaults["channels"][key][
                            "enabled"
                        ],
                    ),
                    f"channels.{key}.enabled",
                )
            }

        result["shared"] = self._normalize_delta(
            raw.get("shared", defaults["shared"]),
            defaults["shared"],
            "shared",
        )
        independent_raw = raw.get(
            "independent",
            defaults["independent"],
        )
        if not isinstance(independent_raw, Mapping):
            raise ModuleError(
                "independent must be a table",
                "K6221_INVALID_SETTINGS",
                "independent",
            )
        result["independent"] = {}
        for index in range(1, 5):
            key = f"ch{index}"
            result["independent"][key] = (
                self._normalize_delta(
                    independent_raw.get(
                        key,
                        defaults["independent"][key],
                    ),
                    defaults["independent"][key],
                    f"independent.{key}",
                )
            )

        if require_measurement:
            enabled = (
                ["ch1"]
                if ch1_only
                else [
                    key
                    for key, channel in result[
                        "channels"
                    ].items()
                    if channel["enabled"]
                ]
            )
            if ch1_only and not result["channels"][
                "ch1"
            ]["enabled"]:
                raise ModuleError(
                    "Enable CH1 because 7001 is unavailable",
                    "K6221_INVALID_SETTINGS",
                    "channels.ch1.enabled",
                )
            if not enabled:
                raise ModuleError(
                    "Enable at least one channel",
                    "K6221_INVALID_SETTINGS",
                    "channels",
                )
            active_settings = (
                [result["shared"]]
                if mode == MODE_SHARED
                else [
                    result["independent"][key]
                    for key in enabled
                ]
            )
            for selected in active_settings:
                self._validate_active_delta(selected)
            self._validate_measure_duration(
                result,
                enabled,
                float(operation_timeout_seconds),
            )
        return result

    def _normalize_delta(
        self,
        raw: object,
        defaults: Mapping[str, Any],
        prefix: str,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ModuleError(
                f"{prefix} must be a settings table",
                "K6221_INVALID_SETTINGS",
                prefix,
            )
        try:
            high = parse_quantity(
                raw.get(
                    "high_current",
                    defaults["high_current"],
                ),
                expected_unit="A",
            )
            low = parse_quantity(
                raw.get(
                    "low_current",
                    defaults["low_current"],
                ),
                expected_unit="A",
            )
            compliance = parse_quantity(
                raw.get(
                    "compliance",
                    defaults["compliance"],
                ),
                expected_unit="V",
            )
            delay = parse_quantity(
                raw.get(
                    "delta_delay",
                    defaults["delta_delay"],
                ),
                expected_unit="s",
            )
        except ValueError as exc:
            raise ModuleError(
                f"{prefix} contains an invalid SI value: {exc}",
                "K6221_INVALID_SETTINGS",
                prefix,
            ) from exc
        if not 0 <= high <= DEVICE_CURRENT_LIMIT_A:
            raise ModuleError(
                f"{prefix}.high_current must be from 0 to "
                f"{DEVICE_CURRENT_LIMIT_A:g} A",
                "K6221_INVALID_SETTINGS",
                f"{prefix}.high_current",
            )
        if not -DEVICE_CURRENT_LIMIT_A <= low <= 0:
            raise ModuleError(
                f"{prefix}.low_current must be from "
                f"{-DEVICE_CURRENT_LIMIT_A:g} to 0 A",
                "K6221_INVALID_SETTINGS",
                f"{prefix}.low_current",
            )
        if not (
            DEVICE_COMPLIANCE_MIN_V
            <= compliance
            <= DEVICE_COMPLIANCE_MAX_V
        ):
            raise ModuleError(
                f"{prefix}.compliance must be from "
                f"{DEVICE_COMPLIANCE_MIN_V:g} to "
                f"{DEVICE_COMPLIANCE_MAX_V:g} V",
                "K6221_INVALID_SETTINGS",
                f"{prefix}.compliance",
            )
        if not 0 <= delay <= 9999.999:
            raise ModuleError(
                f"{prefix}.delta_delay must be from 0 to "
                "9999.999 s",
                "K6221_INVALID_SETTINGS",
                f"{prefix}.delta_delay",
            )
        voltage_range = str(
            raw.get(
                "voltage_range",
                defaults["voltage_range"],
            )
        ).strip().casefold()
        if voltage_range not in {
            key for key, _label, _value in VOLTAGE_RANGES
        }:
            raise ModuleError(
                f"{prefix}.voltage_range is invalid",
                "K6221_INVALID_SETTINGS",
                f"{prefix}.voltage_range",
            )
        filter_type = str(
            raw.get(
                "digital_filter_type",
                defaults["digital_filter_type"],
            )
        ).strip().casefold()
        if filter_type not in {
            key for key, _label in FILTER_TYPES
        }:
            raise ModuleError(
                f"{prefix}.digital_filter_type is invalid",
                "K6221_INVALID_SETTINGS",
                f"{prefix}.digital_filter_type",
            )
        return {
            "high_current": high,
            "low_current": low,
            "compliance": compliance,
            "delta_delay": delay,
            "count": self._integer(
                raw.get("count", defaults["count"]),
                1,
                MAX_DELTA_COUNT,
                f"{prefix}.count",
            ),
            "voltage_range": voltage_range,
            # 使用 1-50 PLC 可同时适用于 50 Hz 与 60 Hz 电网；6221 ARM 要求整数。
            "nplc": self._integer(
                raw.get("nplc", defaults["nplc"]),
                1,
                50,
                f"{prefix}.nplc",
            ),
            "analog_filter_enabled": self._boolean(
                raw.get(
                    "analog_filter_enabled",
                    defaults["analog_filter_enabled"],
                ),
                f"{prefix}.analog_filter_enabled",
            ),
            "digital_filter_enabled": self._boolean(
                raw.get(
                    "digital_filter_enabled",
                    defaults["digital_filter_enabled"],
                ),
                f"{prefix}.digital_filter_enabled",
            ),
            "digital_filter_type": filter_type,
            "digital_filter_count": self._integer(
                raw.get(
                    "digital_filter_count",
                    defaults["digital_filter_count"],
                ),
                1,
                100,
                f"{prefix}.digital_filter_count",
            ),
            "digital_filter_window_percent": (
                self._number(
                    raw.get(
                        "digital_filter_window_percent",
                        defaults[
                            "digital_filter_window_percent"
                        ],
                    ),
                    0.0,
                    10.0,
                    f"{prefix}."
                    "digital_filter_window_percent",
                )
            ),
        }

    @staticmethod
    def _validate_active_delta(
        settings: Mapping[str, Any],
    ) -> None:
        high = float(settings["high_current"])
        low = float(settings["low_current"])
        if high <= 0 or low >= 0:
            raise ModuleError(
                "Enabled Delta settings require positive High "
                "current and negative Low current",
                "K6221_INVALID_SETTINGS",
                "delta_current",
            )
        current = Keithley6221DeltaBackend._effective_current(
            settings
        )
        if not math.isfinite(current) or current <= 0:
            raise ModuleError(
                "Delta current span must be non-zero",
                "K6221_INVALID_SETTINGS",
                "delta_current",
            )

    @staticmethod
    def _estimated_channel_seconds(
        settings: Mapping[str, Any],
    ) -> float:
        # 50 Hz 是更保守的公共电网基准；首次 Delta 点需要三次 A/D，之后每个
        # 新 A/D 产生一个移动平均 Delta 点。
        conversions = int(settings["count"]) + 2
        per_conversion = (
            float(settings["nplc"]) / 50.0
            + float(settings["delta_delay"])
            + 0.02
        )
        return conversions * per_conversion

    def _validate_measure_duration(
        self,
        settings: Mapping[str, Any],
        enabled: list[str],
        operation_timeout_seconds: float,
    ) -> None:
        # operation_timeout_seconds 是每个生命周期调用各自的上限。共享模式的唯一
        # ARM 位于 begin_sequence，不能再次计入 Measure；独立模式则每个通道都在
        # Measure 内 ARM。Begin 与 Measure 必须分别证明能够在同一个调用上限内完成。
        begin_worst_case = 5.0 + (
            ARM_SETTLE_SECONDS
            if settings["mode"] == MODE_SHARED
            else 0.0
        )
        available = max(
            0.0,
            operation_timeout_seconds
            - _OPERATION_CLEANUP_RESERVE_SECONDS,
        )
        if (
            not math.isfinite(operation_timeout_seconds)
            or operation_timeout_seconds <= 0
            or begin_worst_case > available
        ):
            raise ModuleError(
                f"Worst-case Begin Sequence time "
                f"{begin_worst_case:.1f} s does not fit the "
                "core module operation timeout "
                f"{operation_timeout_seconds:.1f} s; increase "
                "[modules] operation_timeout_seconds and "
                "restart",
                "K6221_BEGIN_TIMEOUT_UNSAFE",
            )

        measure_arm_total = (
            0.0
            if settings["mode"] == MODE_SHARED
            else ARM_SETTLE_SECONDS * len(enabled)
        )
        measurement_total = sum(
            self._estimated_channel_seconds(
                settings["shared"]
                if settings["mode"] == MODE_SHARED
                else settings["independent"][key]
            )
            for key in enabled
        )
        switching_total = (
            float(settings["switch_settle_seconds"])
            * len(enabled)
        )
        worst_case = (
            measure_arm_total
            + measurement_total
            + switching_total
            + 5.0
        )
        if (
            not math.isfinite(operation_timeout_seconds)
            or operation_timeout_seconds <= 0
            or worst_case > available
        ):
            raise ModuleError(
                f"Worst-case Measure time {worst_case:.1f} s "
                "does not fit the core module operation timeout "
                f"{operation_timeout_seconds:.1f} s; shorten "
                "Delta count/delay, disable channels, or increase "
                "[modules] operation_timeout_seconds and restart",
                "K6221_MEASURE_TIMEOUT_UNSAFE",
            )

    @staticmethod
    def _integer(
        value: Any,
        minimum: int,
        maximum: int,
        name: str,
    ) -> int:
        if isinstance(value, bool):
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "K6221_INVALID_SETTINGS",
                name,
            )
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "K6221_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            isinstance(value, float)
            and value != result
        ) or not minimum <= result <= maximum:
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "K6221_INVALID_SETTINGS",
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
        if isinstance(value, bool):
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "K6221_INVALID_SETTINGS",
                name,
            )
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "K6221_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            not math.isfinite(result)
            or not minimum <= result <= maximum
        ):
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "K6221_INVALID_SETTINGS",
                name,
            )
        return result

    @staticmethod
    def _boolean(value: Any, name: str) -> bool:
        if not isinstance(value, bool):
            raise ModuleError(
                f"{name} must be true or false",
                "K6221_INVALID_SETTINGS",
                name,
            )
        return value


__all__ = [
    "InstrumentTransport",
    "Keithley6221DeltaBackend",
    "PyVisaTransport",
]
