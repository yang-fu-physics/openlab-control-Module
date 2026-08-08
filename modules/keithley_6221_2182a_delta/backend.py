"""Keithley 6221 + 2182A Delta 与显式可选切换器的 Measurement Module 后端。

协议依据：

- 6221 用户手册第 5 节：``SOUR:DELT:*``、``INIT:IMM``、``TRAC:DATA?``；
- 2182A 由 6221 的 RS-232 转发口控制，不单独占用电脑的 VISA 资源；
- 7001 使用 SCPI 路由；3706A 使用区分大小写的 TSP 路由。

关键安全边界：

- Enable 不连接仪表；Apply 按 None/7001/3706A 的明确选择连接；
- Apply 前后均确认 6221 为零电流且输出关闭；
- 共享模式只在 ``run_start`` 事件 ARM 一次，ARM 后等待至少 3 秒并查询确认；
- 独立模式每次切换前 Abort/Clear，切换后重新配置和 ARM；
- 切换器的任何运行期通信或回读错误立即中止，不自动重发切换/触发命令；
- compliance abort 和 cold switching 固定开启，通道设置必须落在仪表合法命令范围；
- Stop/Error/Disable/SEQ 完成均 Abort、清零、关闭输出并打开切换器全部触点。

本模块仍是 Beta。自动化测试只能验证命令状态机，不能证明真实开关卡接线、6221 实际
零电流回读语义或 DUT 的功耗边界正确。
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from labcontrol.module_api import (
    ModuleError,
    ModuleAPI,
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
    SWITCHER_3706A,
    SWITCHER_7001,
    SWITCHER_NONE,
    SWITCHER_TYPES,
    STATUS_CODE_INVALID_TRACE,
    STATUS_CODE_NORMAL,
    STATUS_CODE_OVER_RANGE,
    VOLTAGE_RANGES,
    default_delta_settings,
    default_settings,
)
from .quantities import parse_quantity
from .routing import RoutingTable, load_routing
from . import keithley_2182a
from . import keithley_3706a
from . import keithley_6221
from . import keithley_7001


TransportFactory = Callable[[str, float], keithley_6221.Transport]
ResourceLister = Callable[[], tuple[str, ...]]
Waiter = Callable[[ModuleAPI, float], None]

# 长时间 ``*OPC?`` 不再拥有可配置的单通道超时，而是共享本次 Measure 的核心总
# 预算。提前预留两秒，使通信错误仍有机会在核心强制终止 worker 前执行安全关闭。
_OPERATION_CLEANUP_RESERVE_SECONDS = 2.0


class Keithley6221DeltaBackend:
    """6221/2182A Delta 测量及可选 7001/3706A 四路路由状态机。"""

    columns = {
        "Channel": "",
        "Resistance": "Ohm",
        "Current": "A",
        "StdDev": "Ohm",
        "SampleCount": "",
        "StatusCode": "",
    }

    def __init__(
        self,
        *,
        transport_factory: TransportFactory | None = None,
        resource_lister: ResourceLister | None = None,
        waiter: Waiter | None = None,
        routing: RoutingTable | None = None,
    ) -> None:
        self._transport_factory = (
            transport_factory or keithley_6221.PyVisaTransport
        )
        self._resource_lister = (
            resource_lister or keithley_6221.PyVisaTransport.list_resources
        )
        self._waiter = (
            waiter
            or (
                lambda api, seconds: api.sleep(
                    seconds
                )
            )
        )
        try:
            self.routing_table = routing or load_routing()
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
        self.transport_6221: keithley_6221.Transport | None = None
        self.transport_switcher: keithley_6221.Transport | None = None
        self.identity_6221 = ""
        self.identity_2182a = ""
        self.identity_switcher = ""
        self.switcher_type = SWITCHER_NONE
        self.sequence_active = False
        self.armed = False
        self.active_channel = ""
        self.last_resistance: float | None = None
        self.last_current: float | None = None
        self.last_stddev: float | None = None
        self.last_status = "Idle"

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        """只发现资源并建立空闲状态，不连接或配置任何仪表。"""

        self.desired_settings = self._normalized_settings(
            default_settings(),
            require_6221=False,
            require_measurement=False,
            operation_timeout_seconds=(
                api.timeout
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
            api.warn("K6221_RESOURCE_DISCOVERY_FAILED", None)
        except Exception as exc:
            resources = ()
            api.warn(
                "K6221_RESOURCE_DISCOVERY_FAILED",
                "GPIB resource discovery failed: "
                f"{type(exc).__name__}: {exc}",
            )

        # Enable 不读取保存设置，也不探测切换器；只有 Apply 才按用户的明确选择连接。
        self.switcher_type = SWITCHER_NONE
        self.identity_switcher = ""
        self.applied_settings = None
        self.sequence_active = False
        self.armed = False
        self.active_channel = ""
        self.last_status = "Initialized"
        status = self._status(
            available_resources=resources,
        )
        api.status(status)
        return status

    def configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """连接、识别并写入一套安全的待 ARM 设置，但不开始输出。"""

        normalized = self._normalized_settings(
            settings,
            require_6221=True,
            require_measurement=True,
            operation_timeout_seconds=(
                api.timeout
            ),
        )
        # 重新 Apply 可能改变切换器类型或地址。必须先按“旧配置”确认零电流和全部
        # 路由断开，再释放旧会话；不能先覆盖 switcher_type 后让清理走错协议。
        if (
            self.transport_6221 is not None
            or self.transport_switcher is not None
        ):
            self._enter_safe_state(api)
            self._close_transports()
        self.desired_settings = deepcopy(normalized)
        self.switcher_type = str(normalized["switcher_type"])
        try:
            self._connect_6221(normalized, api)
            if self.switcher_type != SWITCHER_NONE:
                self._connect_switcher(normalized, api)
            else:
                self._close_transport_switcher()
            self._enter_safe_state(api)
            self._verify_2182a(api)
            first_channel = self._enabled_channels(
                normalized
            )[0]
            selected = self._channel_settings(
                normalized,
                first_channel,
            )
            self._configure_delta(selected, api)
            # Apply 结束时再次清零。配置命令本身不应打开输出，但这一确认可捕获
            # 前面遗留状态或仪表异常行为。
            self._enter_safe_state(api)
        except Exception:
            self._best_effort_safe_state(api)
            raise

        self.applied_settings = normalized
        self.last_status = "Settings applied - output off"
        status = self._status()
        api.status(status)
        return status

    def _run_start(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """在第一条 SEQ 指令前建立本次运行状态。

        共享模式在这里完成唯一一次 ARM；独立模式只确认安全状态，等每个通道路由
        闭合并应用自己的设置后再 ARM。
        """

        settings = self._require_applied()
        try:
            self._enter_safe_state(api)
            if settings["mode"] == MODE_SHARED:
                self._configure_delta(
                    settings["shared"],
                    api,
                )
                self._arm_delta(api)
            self.sequence_active = True
            self.last_status = (
                "Armed - waiting for software trigger"
                if self.armed
                else "Sequence ready"
            )
        except Exception:
            self.sequence_active = False
            self._best_effort_safe_state(api)
            raise
        status = self._status()
        api.status(status)
        return status

    def measure(
        self,
        slot: int,
        api: ModuleAPI,
    ) -> tuple[Mapping[str, Any], list[float]]:
        """测量核心当前调度的一个通道；异常返回核心前先请求安全关闭。

        核心随后仍会发送 ``run_end(error|stopped)`` 做严格确认。这里的
        best-effort 不是替代该生命周期，而是缩短 7001/6221 通信失败到 Abort、清零、
        打开触点之间的时间；协作 Stop 在 checkpoint 抛出的异常也走同一路径。
        """

        operation_deadline = (
            time.monotonic()
            + float(api.timeout)
            - _OPERATION_CLEANUP_RESERVE_SECONDS
        )
        try:
            return self._measure_impl(
                slot,
                api,
                operation_deadline,
            )
        except Exception:
            self.sequence_active = False
            self.last_status = (
                "Measurement interrupted - safety shutdown requested"
            )
            self._best_effort_safe_state(api)
            raise

    def _measure_impl(
        self,
        slot: int,
        api: ModuleAPI,
        operation_deadline: float,
    ) -> tuple[Mapping[str, Any], list[float]]:
        """实现当前逻辑槽位的正常测量路径，产生一行 DAT 与 rawdata。"""

        settings = self._require_ready()
        channels = self._enabled_channels(settings)
        if slot < 1:
            raise ModuleError(
                "Delta module received an invalid logical slot",
                "K6221_LOGICAL_SLOT_INVALID",
            )
        scheduled = f"ch{slot}"
        if scheduled not in channels:
            raise ModuleError(
                f"{scheduled.upper()} is not enabled for this sequence",
                "K6221_LOGICAL_SLOT_DISABLED",
                scheduled,
            )
        for channel in (scheduled,):
            api.sleep(0)
            selected = self._channel_settings(
                settings,
                channel,
            )
            if settings["mode"] == MODE_INDEPENDENT:
                self._enter_safe_source_state(api)
                self._switch_channel(
                    channel,
                    settings,
                    api,
                )
                self._configure_delta(selected, api)
                self._arm_delta(api)
            else:
                self._verify_armed(api)
                self._verify_zero_current(api)
                self._switch_channel(
                    channel,
                    settings,
                    api,
                )

            raw_values, issues, status_code = (
                self._trigger_and_read(
                    selected,
                    api,
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
                api.warn(
                    "K6221_READING_WARNING",
                    f"{channel.upper()} Delta readings are invalid: "
                    + "; ".join(issues),
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
                api.warn("K6221_READING_WARNING", None, channel)
                self.last_resistance = mean
                self.last_stddev = stddev
                self.last_status = "Normal"
            self.last_current = current
            self.active_channel = channel.upper()
            api.status(self._status())
            api.sleep(0)
            return row, raw_values

    @property
    def slots(self) -> tuple[int, ...]:
        settings = self._require_ready()
        return tuple(
            int(channel[2:])
            for channel in self._enabled_channels(settings)
        )

    def _run_end(
        self,
        reason: str,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """对 completed/stopped/error 使用相同的严格安全收尾。"""

        self.sequence_active = False
        self.armed = False
        try:
            self._enter_safe_state(api)
        finally:
            # 即使严格安全确认抛错，内部状态也不能继续显示 Running/Armed。
            self.sequence_active = False
            self.armed = False
            self.active_channel = ""
        self.last_status = f"Sequence {reason} - output off"
        status = self._status()
        api.status(status)
        return status

    def close(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """Disable/退出时尽力安全关闭，再释放两个 VISA 会话。"""

        self.sequence_active = False
        self.armed = False
        failure: ModuleError | None = None
        try:
            self._enter_safe_state(api)
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
        api.status(status)
        if failure is not None:
            raise failure
        return status

    def _read_status(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """只读刷新 Armed 与输出状态，不隐式 Apply 或操作切换器。"""

        if self.transport_6221 is not None:
            armed = self._query_6221(
                keithley_6221.ARM_QUERY,
                api,
            )
            self.armed = self._parse_switch(
                armed,
                keithley_6221.ARM_QUERY,
            )
            output = self._parse_switch(
                self._query_6221(keithley_6221.OUTPUT_QUERY, api),
                keithley_6221.OUTPUT_QUERY,
            )
            self.last_status = (
                "Armed" if self.armed else "Connected"
            ) + (" / Output on" if output else " / Output off")
        status = self._status()
        api.status(status)
        return status

    def _action(
        self,
        action: str,
        payload: Mapping[str, Any],
        api: ModuleAPI,
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
            api.warn("K6221_RESOURCE_DISCOVERY_FAILED", None)
            status = self._status(
                available_resources=resources,
            )
            api.status(status)
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
                    api.timeout
                ),
            )
            self._test_connections(settings, api)
            status = self._status()
            api.status(status)
            return status
        if action == "safe_off":
            self._enter_safe_state(api)
            self.last_status = "Output off / all routes open"
            status = self._status()
            api.status(status)
            return status
        raise ModuleError(
            f"Unsupported action: {action}",
            "UNSUPPORTED_ACTION",
            action,
        )

    def on_event(
        self,
        event: str,
        data: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        if event == "run_start":
            return self._run_start(api)
        if event == "run_end":
            return self._run_end(str(data.get("reason", "error")), api)
        if event == "status":
            return self._read_status(api)
        if event == "action":
            payload = data.get("payload", {})
            if not isinstance(payload, Mapping):
                raise ModuleError(
                    "Action payload must be a mapping",
                    "K6221_INVALID_ACTION",
                )
            return self._action(str(data.get("name", "")), payload, api)
        return {}

    def _test_connections(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        """临时连接并识别仪表，不改变当前 Apply/Armed 状态。"""

        test_6221: keithley_6221.Transport | None = None
        test_switcher: keithley_6221.Transport | None = None
        try:
            test_6221 = self._transport_factory(
                str(settings["resource_6221"]),
                float(settings["io_timeout_seconds"]),
            )
            identity_6221 = test_6221.query(keithley_6221.IDENTIFY).strip()
            self._validate_6221_identity(identity_6221)
            self.identity_6221 = identity_6221
            switcher_type = str(settings["switcher_type"])
            if switcher_type != SWITCHER_NONE:
                test_switcher = self._transport_factory(
                    str(settings["resource_switcher"]),
                    float(settings["io_timeout_seconds"]),
                )
                identity_switcher = test_switcher.query(
                    self._switcher_identify_command(switcher_type)
                ).strip()
                self._validate_switcher_identity(switcher_type, identity_switcher)
                self.identity_switcher = identity_switcher
            api.warn("K6221_CONNECTION_TEST_FAILED", None)
            self.last_status = "Connection test passed"
        except Exception as exc:
            raise ModuleWarning(
                "Connection test failed: "
                f"{type(exc).__name__}: {exc}",
                "K6221_CONNECTION_TEST_FAILED",
            ) from exc
        finally:
            for transport in (test_switcher, test_6221):
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass

    def _connect_6221(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        self._close_transport_6221()
        try:
            self.transport_6221 = self._transport_factory(
                str(settings["resource_6221"]),
                float(settings["io_timeout_seconds"]),
            )
            identity = self.transport_6221.query(keithley_6221.IDENTIFY).strip()
            self._validate_6221_identity(identity)
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
        api.sleep(0)

    def _connect_switcher(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        """按明确类型连接切换器；失败直接终止 Apply，不做自动降级。"""

        self._close_transport_switcher()
        switcher_type = str(settings["switcher_type"])
        resource = str(settings["resource_switcher"])
        try:
            self.transport_switcher = self._transport_factory(
                resource,
                float(settings["io_timeout_seconds"]),
            )
            identity = self.transport_switcher.query(
                self._switcher_identify_command(switcher_type)
            ).strip()
            self._validate_switcher_identity(switcher_type, identity)
        except ModuleError:
            self._close_transport_switcher()
            raise
        except Exception as exc:
            self._close_transport_switcher()
            raise ModuleError(
                f"Keithley {switcher_type} could not be connected for Apply: "
                f"{type(exc).__name__}: {exc}",
                "K6221_SWITCHER_CONNECTION_FAILED",
                resource,
            ) from exc
        self.identity_switcher = identity
        api.sleep(0)

    @staticmethod
    def _validate_6221_identity(identity: str) -> None:
        if not keithley_6221.validate_identity(identity):
            raise ModuleError(
                f"Expected Keithley 6221, received {identity!r}",
                "K6221_IDENTITY_MISMATCH",
                identity,
            )

    @staticmethod
    def _switcher_identify_command(switcher_type: str) -> str:
        return (
            keithley_7001.IDENTIFY
            if switcher_type == SWITCHER_7001
            else keithley_3706a.IDENTIFY
        )

    @staticmethod
    def _validate_switcher_identity(switcher_type: str, identity: str) -> None:
        valid = (
            keithley_7001.validate_identity(identity)
            if switcher_type == SWITCHER_7001
            else keithley_3706a.validate_identity(identity)
        )
        if not valid:
            raise ModuleError(
                f"Expected Keithley {switcher_type}, received {identity!r}",
                "K6221_SWITCHER_IDENTITY_MISMATCH",
                identity,
            )

    def _verify_2182a(
        self,
        api: ModuleAPI,
    ) -> None:
        present = self._query_6221(
            keithley_6221.NANOVOLTMETER_PRESENT_QUERY,
            api,
        )
        if not self._parse_switch(
            present,
            keithley_6221.NANOVOLTMETER_PRESENT_QUERY,
        ):
            raise ModuleError(
                "6221 does not report a compatible 2182A on "
                "its RS-232 connection",
                "K6221_2182A_NOT_PRESENT",
            )
        identity = self._serial_query(
            keithley_2182a.IDENTIFY,
            api,
        )
        if not keithley_2182a.validate_identity(identity):
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
        api: ModuleAPI,
    ) -> None:
        """写入并逐项回读一套 Delta/2182A 设置；不 ARM。"""

        high = float(settings["high_current"])
        low = float(settings["low_current"])
        compliance = float(settings["compliance"])
        delay = float(settings["delta_delay"])
        count = int(settings["count"])

        # 明确使用预数学电压，软件再按有效反转电流换算电阻。
        for command in keithley_6221.configuration_commands(settings):
            self._write_6221(command, api)

        self._configure_2182a(settings, api)
        self._expect_float(
            keithley_6221.COMPLIANCE_QUERY,
            compliance,
            api,
        )
        self._expect_float(
            keithley_6221.HIGH_CURRENT_QUERY,
            high,
            api,
        )
        self._expect_float(
            keithley_6221.LOW_CURRENT_QUERY,
            low,
            api,
        )
        self._expect_float(
            keithley_6221.DELAY_QUERY,
            delay,
            api,
        )
        self._expect_integer(
            keithley_6221.COUNT_QUERY,
            count,
            api,
        )
        for query in (
            keithley_6221.COLD_SWITCH_QUERY,
            keithley_6221.COMPLIANCE_ABORT_QUERY,
        ):
            if not self._parse_switch(
                self._query_6221(query, api),
                query,
            ):
                raise ModuleError(
                    f"{query} did not read back ON",
                    "K6221_SETTINGS_VERIFY_FAILED",
                    query,
                )
        self._raise_if_instrument_error(api)

    def _configure_2182a(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        voltage_range = str(settings["voltage_range"])
        ranges = {
            key: value
            for key, _label, value in VOLTAGE_RANGES
        }
        for command in keithley_2182a.range_commands(voltage_range, ranges):
            self._serial_write(command, api)
        if voltage_range == "auto":
            self._expect_serial_switch(
                keithley_2182a.RANGE_AUTO_QUERY,
                True,
                api,
            )
        else:
            value = ranges[voltage_range]
            assert value is not None
            self._expect_serial_switch(
                keithley_2182a.RANGE_AUTO_QUERY,
                False,
                api,
            )
            actual = self._parse_float(
                self._serial_query(
                    keithley_2182a.RANGE_QUERY,
                    api,
                ),
                keithley_2182a.RANGE_QUERY,
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
                    keithley_2182a.RANGE_SETTING,
                )

        nplc = int(settings["nplc"])
        self._serial_write(
            keithley_2182a.nplc_command(nplc),
            api,
        )
        actual_nplc = self._parse_float(
            self._serial_query(keithley_2182a.NPLC_QUERY, api),
            keithley_2182a.NPLC_QUERY,
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
                keithley_2182a.NPLC_SETTING,
            )

        analog = bool(settings["analog_filter_enabled"])
        digital = bool(
            settings["digital_filter_enabled"]
        )
        for command in keithley_2182a.filter_commands(settings):
            self._serial_write(command, api)
        self._expect_serial_switch(
            keithley_2182a.ANALOG_FILTER_QUERY,
            analog,
            api,
        )
        self._expect_serial_switch(
            keithley_2182a.DIGITAL_FILTER_QUERY,
            digital,
            api,
        )

    def _arm_delta(
        self,
        api: ModuleAPI,
    ) -> None:
        """发送 ARM，等待用户指定的 3 秒，再查询确认。"""

        self._write_6221(keithley_6221.ARM, api)
        self.armed = False
        self.last_status = "Arming"
        api.status(self._status())
        self._waiter(api, ARM_SETTLE_SECONDS)
        self._verify_armed(api)
        self._raise_if_instrument_error(api)
        self.last_status = "Armed - waiting for software trigger"
        api.status(self._status())

    def _verify_armed(
        self,
        api: ModuleAPI,
    ) -> None:
        reply = self._query_6221(
            keithley_6221.ARM_QUERY,
            api,
        )
        self.armed = self._parse_switch(
            reply,
            keithley_6221.ARM_QUERY,
        )
        if not self.armed:
            raise ModuleError(
                "6221 did not enter Delta Armed state",
                "K6221_ARM_FAILED",
            )

    def _trigger_and_read(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
        operation_deadline: float,
    ) -> tuple[
        tuple[float, ...],
        tuple[str, ...],
        int,
    ]:
        """软件触发有限 Delta 采集并读取预数学电压缓冲。"""

        self._verify_armed(api)
        self._write_6221(keithley_6221.TRIGGER, api)
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
            keithley_6221.COMPLETE_QUERY,
            api,
            timeout_seconds=completion_timeout,
        )
        if completion.strip() not in {"1", "+1"}:
            raise ModuleError(
                f"6221 returned unexpected completion state "
                f"{completion!r}",
                "K6221_MEASUREMENT_NOT_COMPLETE",
            )
        trace = self._query_6221(
            keithley_6221.TRACE_QUERY,
            api,
        )
        self._raise_if_instrument_error(api)
        self._verify_armed(api)
        self._verify_zero_current(api)
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

        values, issues, invalid_trace, over_range = keithley_2182a.parse_trace(
            reply,
            expected_count,
        )
        return (
            values,
            issues,
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
        api: ModuleAPI,
    ) -> None:
        if self.switcher_type == SWITCHER_NONE:
            if channel != "ch1":
                raise ModuleError(
                    "Switcher type is None; only CH1 may be measured",
                    "K6221_SWITCHER_UNAVAILABLE",
                    channel,
                )
            self.active_channel = "CH1"
            return
        if self.transport_switcher is None:
            raise ModuleError(
                f"{self.switcher_type} transport is not connected",
                "K6221_SWITCHER_NOT_CONNECTED",
            )
        routing = self.routing_table.for_switcher(self.switcher_type)
        target = routing.channels[channel]
        # 两种切换器都严格执行 break-before-make，并在触发电流前读回整机状态。
        if self.switcher_type == SWITCHER_7001:
            self._write_switcher(keithley_7001.OPEN_ALL, api)
            self._expect_7001_states(
                self._query_switcher(
                    keithley_7001.open_query(routing.all_routes),
                    api,
                ),
                len(routing.all_routes),
                expected=True,
                command=keithley_7001.open_query(routing.all_routes),
            )
            self._write_switcher(keithley_7001.close_command(target), api)
            self._expect_7001_states(
                self._query_switcher(keithley_7001.close_query(target), api),
                len(target),
                expected=True,
                command=keithley_7001.close_query(target),
            )
        else:
            self._write_switcher(keithley_3706a.OPEN_ALL, api)
            self._expect_3706a_routes(
                self._query_switcher(keithley_3706a.CLOSED_QUERY, api),
                frozenset(),
                keithley_3706a.OPEN_ALL,
            )
            self._write_switcher(keithley_3706a.close_command(target), api)
            self._expect_3706a_routes(
                self._query_switcher(keithley_3706a.CLOSED_QUERY, api),
                frozenset(target),
                keithley_3706a.close_command(target),
            )
        self.active_channel = channel.upper()
        api.status(self._status())
        self._waiter(
            api,
            float(settings["switch_settle_seconds"]),
        )

    @staticmethod
    def _expect_7001_states(
        reply: str,
        count: int,
        *,
        expected: bool,
        command: str,
    ) -> None:
        try:
            states = keithley_7001.parse_route_states(reply, count)
        except ValueError as exc:
            raise ModuleError(
                f"{command} returned invalid route states {reply!r}",
                "K6221_SWITCHER_VERIFY_FAILED",
                command,
            ) from exc
        if any(state is not expected for state in states):
            raise ModuleError(
                f"{command} did not confirm every route as {expected}",
                "K6221_SWITCHER_VERIFY_FAILED",
                command,
            )

    @staticmethod
    def _expect_3706a_routes(
        reply: str,
        expected: frozenset[str],
        command: str,
    ) -> None:
        try:
            actual = keithley_3706a.parse_closed_routes(reply)
        except ValueError as exc:
            raise ModuleError(
                f"{command} returned invalid closed routes {reply!r}",
                "K6221_SWITCHER_VERIFY_FAILED",
                command,
            ) from exc
        if actual != expected:
            raise ModuleError(
                f"{command} left {sorted(actual)!r} closed; expected "
                f"{sorted(expected)!r}",
                "K6221_SWITCHER_VERIFY_FAILED",
                command,
            )

    def _enter_safe_state(
        self,
        api: ModuleAPI,
    ) -> None:
        """严格 Abort/Clear/输出关闭并打开全部路由。"""

        # Enable 后尚未 Apply 时没有打开任何仪表会话，也没有由本 worker 产生的
        # 输出或路由需要确认。此路径允许用户直接 Disable，而不会把“未连接”误报
        # 成“安全关闭失败”。
        if (
            self.transport_6221 is None
            and self.transport_switcher is None
        ):
            self.armed = False
            self.active_channel = ""
            return
        self._enter_safe_source_state(api)
        if self.switcher_type != SWITCHER_NONE:
            if self.transport_switcher is None:
                raise ModuleError(
                    f"Cannot confirm {self.switcher_type} routes are open because "
                    "the switcher is not connected",
                    "K6221_SAFE_STATE_FAILED",
                    self.switcher_type,
                )
            routing = self.routing_table.for_switcher(self.switcher_type)
            if self.switcher_type == SWITCHER_7001:
                self._write_switcher(keithley_7001.OPEN_ALL, api)
                command = keithley_7001.open_query(routing.all_routes)
                self._expect_7001_states(
                    self._query_switcher(command, api),
                    len(routing.all_routes),
                    expected=True,
                    command=command,
                )
            else:
                self._write_switcher(keithley_3706a.OPEN_ALL, api)
                self._expect_3706a_routes(
                    self._query_switcher(keithley_3706a.CLOSED_QUERY, api),
                    frozenset(),
                    keithley_3706a.OPEN_ALL,
                )
        self.active_channel = ""

    def _enter_safe_source_state(
        self,
        api: ModuleAPI,
    ) -> None:
        if self.transport_6221 is None:
            self.armed = False
            return
        self._write_6221(keithley_6221.ABORT, api)
        self._write_6221(keithley_6221.CLEAR, api)
        output = self._parse_switch(
            self._query_6221(keithley_6221.OUTPUT_QUERY, api),
            keithley_6221.OUTPUT_QUERY,
        )
        current = self._parse_float(
            self._query_6221(keithley_6221.CURRENT_QUERY, api),
            keithley_6221.CURRENT_QUERY,
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
        api: ModuleAPI,
    ) -> None:
        current = self._parse_float(
            self._query_6221(
                keithley_6221.CURRENT_QUERY,
                api,
            ),
            keithley_6221.CURRENT_QUERY,
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
        api: ModuleAPI,
    ) -> None:
        """异常路径不掩盖原始错误，但尽可能执行安全命令。"""

        if self.transport_6221 is not None:
            for command in (
                keithley_6221.ABORT,
                keithley_6221.CLEAR,
            ):
                try:
                    self.transport_6221.write(command)
                except Exception:
                    pass
        if (
            self.switcher_type != SWITCHER_NONE
            and self.transport_switcher is not None
        ):
            try:
                self.transport_switcher.write(
                    keithley_7001.OPEN_ALL
                    if self.switcher_type == SWITCHER_7001
                    else keithley_3706a.OPEN_ALL
                )
            except Exception:
                pass
        self.armed = False
        self.active_channel = ""
        try:
            api.status(self._status())
        except Exception:
            pass

    def _write_6221(
        self,
        command: str,
        api: ModuleAPI,
    ) -> None:
        transport = self.transport_6221
        if transport is None:
            raise ModuleError(
                "6221 is not connected",
                "K6221_NOT_CONNECTED",
            )
        api.sleep(0)
        try:
            transport.write(command)
        except Exception as exc:
            raise ModuleError(
                f"6221 command failed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "K6221_COMMUNICATION_FAILED",
                command,
            ) from exc
        api.sleep(0)

    def _query_6221(
        self,
        command: str,
        api: ModuleAPI,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        transport = self.transport_6221
        if transport is None:
            raise ModuleError(
                "6221 is not connected",
                "K6221_NOT_CONNECTED",
            )
        api.sleep(0)
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
        api.sleep(0)
        return str(result).strip()

    def _write_switcher(
        self,
        command: str,
        api: ModuleAPI,
    ) -> None:
        transport = self.transport_switcher
        if transport is None:
            raise ModuleError(
                f"{self.switcher_type} is not connected",
                "K6221_SWITCHER_NOT_CONNECTED",
            )
        api.sleep(0)
        try:
            transport.write(command)
        except Exception as exc:
            raise ModuleError(
                f"{self.switcher_type} command failed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "K6221_SWITCHER_COMMUNICATION_FAILED",
                command,
            ) from exc
        api.sleep(0)

    def _query_switcher(
        self,
        command: str,
        api: ModuleAPI,
    ) -> str:
        transport = self.transport_switcher
        if transport is None:
            raise ModuleError(
                f"{self.switcher_type} is not connected",
                "K6221_SWITCHER_NOT_CONNECTED",
            )
        api.sleep(0)
        try:
            result = transport.query(command)
        except Exception as exc:
            raise ModuleError(
                f"{self.switcher_type} query failed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "K6221_SWITCHER_COMMUNICATION_FAILED",
                command,
            ) from exc
        api.sleep(0)
        return str(result).strip()

    def _serial_write(
        self,
        command: str,
        api: ModuleAPI,
    ) -> None:
        self._write_6221(
            keithley_6221.serial_send(command),
            api,
        )

    def _serial_query(
        self,
        command: str,
        api: ModuleAPI,
    ) -> str:
        self._serial_write(command, api)
        return self._query_6221(
            keithley_6221.SERIAL_ENTER_QUERY,
            api,
        )

    def _raise_if_instrument_error(
        self,
        api: ModuleAPI,
    ) -> None:
        reply = self._query_6221(keithley_6221.ERROR_QUERY, api)
        try:
            error_code = keithley_6221.parse_error_code(reply)
        except ValueError as exc:
            raise ModuleError(
                f"6221 returned invalid error status {reply!r}",
                "K6221_INVALID_REPLY",
                keithley_6221.ERROR_QUERY,
            ) from exc
        if error_code != 0:
            raise ModuleError(
                f"6221 reported an instrument error: {reply}",
                "K6221_INSTRUMENT_ERROR",
                reply,
            )

    def _expect_float(
        self,
        query: str,
        expected: float,
        api: ModuleAPI,
    ) -> None:
        actual = self._parse_float(
            self._query_6221(query, api),
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
        api: ModuleAPI,
    ) -> None:
        actual = self._parse_float(
            self._query_6221(query, api),
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
        api: ModuleAPI,
    ) -> None:
        actual = self._parse_switch(
            self._serial_query(query, api),
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
        try:
            return keithley_6221.parse_switch(reply)
        except ValueError as exc:
            raise ModuleError(
                f"{command} returned invalid state {reply!r}",
                "K6221_INVALID_REPLY",
                command,
            ) from exc

    @staticmethod
    def _parse_float(
        reply: str,
        command: str,
    ) -> float:
        try:
            return keithley_6221.parse_number(reply)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{command} returned invalid number {reply!r}",
                "K6221_INVALID_REPLY",
                command,
            ) from exc

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
        if self.switcher_type == SWITCHER_NONE:
            if not bool(settings["channels"]["ch1"]["enabled"]):
                raise ModuleError(
                    "Enable CH1 when Switcher is None",
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
            self.switcher_type != SWITCHER_NONE
            and self.transport_switcher is None
        ):
            raise ModuleError(
                f"{self.switcher_type} transport is not connected",
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
            "Switcher Type": self.switcher_type,
            "Switcher": (
                "None - CH1 only"
                if self.switcher_type == SWITCHER_NONE
                else (self.identity_switcher or "Not connected")
            ),
            "Armed": self.armed,
            "Sequence Active": self.sequence_active,
            "Active Channel": self.active_channel or "-",
            "Routing Config": str(
                self.routing_table.source_path
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

    def _close_transport_switcher(self) -> None:
        if self.transport_switcher is not None:
            try:
                self.transport_switcher.close()
            finally:
                self.transport_switcher = None
                self.identity_switcher = ""

    def _close_transports(self) -> None:
        first_error: Exception | None = None
        for closer in (
            self._close_transport_switcher,
            self._close_transport_6221,
        ):
            try:
                closer()
            except Exception as exc:
                first_error = first_error or exc
        # close 方法异常时也必须丢弃本地引用；不能让后续 UI 误以为还能复用一个
        # 已经部分关闭、状态未知的 VISA 会话。
        self.transport_switcher = None
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
    ) -> dict[str, Any]:
        """合并默认值并在 worker 内重做全部边界验证。"""

        if not isinstance(raw, Mapping):
            raise ModuleError(
                "Module settings must be a table",
                "K6221_INVALID_SETTINGS",
            )
        defaults = default_settings()
        result: dict[str, Any] = {}
        switcher_type = str(
            raw.get("switcher_type", defaults["switcher_type"])
        ).strip().casefold()
        allowed_switchers = {key for key, _label in SWITCHER_TYPES}
        if switcher_type not in allowed_switchers:
            raise ModuleError(
                "switcher_type must be none, 7001, or 3706a",
                "K6221_INVALID_SETTINGS",
                "switcher_type",
            )
        result["switcher_type"] = switcher_type
        for key in (
            "resource_6221",
            "resource_switcher",
        ):
            value = str(
                raw.get(key, defaults[key])
            ).strip()
            if key == "resource_switcher" and switcher_type == SWITCHER_NONE:
                value = ""
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
        if switcher_type != SWITCHER_NONE and not result["resource_switcher"]:
            raise ModuleError(
                "Select the switcher GPIB resource",
                "K6221_INVALID_SETTINGS",
                "resource_switcher",
            )
        if (
            switcher_type != SWITCHER_NONE
            and result["resource_switcher"]
            and result["resource_6221"].casefold()
            == result["resource_switcher"].casefold()
        ):
            raise ModuleError(
                "Keithley 6221 and switcher must use different VISA resources",
                "K6221_INVALID_SETTINGS",
                "resource_switcher",
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
            enabled = [
                key
                for key, channel in result["channels"].items()
                if channel["enabled"]
            ]
            if switcher_type == SWITCHER_NONE and not result["channels"]["ch1"]["enabled"]:
                raise ModuleError(
                    "Enable CH1 when Switcher is None",
                    "K6221_INVALID_SETTINGS",
                    "channels.ch1.enabled",
                )
            if switcher_type == SWITCHER_NONE and any(
                result["channels"][f"ch{index}"]["enabled"]
                for index in range(2, 5)
            ):
                raise ModuleError(
                    "Disable CH2-CH4 when Switcher is None",
                    "K6221_INVALID_SETTINGS",
                    "channels",
                )
            if switcher_type == SWITCHER_NONE:
                enabled = ["ch1"]
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
        # ARM 位于 run_start，不能再次计入 Measure；独立模式则每个通道都在
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


Module = Keithley6221DeltaBackend

__all__ = [
    "Keithley6221DeltaBackend",
    "Module",
]
