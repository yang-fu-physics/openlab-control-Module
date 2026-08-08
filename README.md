# OpenLab Control Measurement Modules

这是 Measurement Module 的共享仓库。框架负责加载、进程隔离、Pause/Stop、操作总
timeout、跨模块并行和 DAT 写入；模块负责仪表协议、通道、状态码和安全动作。

## 最小硬件模块

```text
modules/my_meter/
├─ module.toml
├─ backend.py
└─ my_meter.py
```

```toml
name = "My Meter"
version = "0.1.0"
```

```python
from labcontrol.module_api import ModuleAPI
from . import my_meter


class Module:
    columns = {"Resistance": "Ohm", "StatusCode": ""}

    def open(self, api: ModuleAPI):
        self.instrument = my_meter.PyVisaTransport("GPIB0::1::INSTR", 3.0)

    def measure(self, slot: int, api: ModuleAPI):
        api.sleep(0)
        return {
            "Resistance": my_meter.parse_number(
                self.instrument.query(my_meter.READ)
            ),
            "StatusCode": 0,
        }

    def close(self, api: ModuleAPI):
        self.instrument.write(my_meter.OUTPUT_OFF)
        self.instrument.close()
```

不继承框架基类。目录名就是 ID，入口固定为 `backend:Module`。`columns` 是有序的
`{列名: 单位}`。

`backend.py` 只保留框架生命周期和流程编排：`open/configure/measure/close`、SEQ
事件、通道循环、总超时预算、重试决策、Warning/Error 和安全清理。每个物理仪表使用
一个同名 Python 文件，例如 `my_meter.py`；其中集中放置 VISA/串口适配器、命令常量、
绝对设置命令构造、身份判断和响应解析。多仪表模块仍只保留一个 `backend.py`，并按
物理仪表分别建立 `keithley_6221.py`、`keithley_2182a.py` 等文件。

仪表文件不调用 `ModuleAPI`，也不决定重试、报警级别或 SEQ 是否终止。`backend.py`
不直接拼接 SCPI/TSP 文本；它只调用仪表文件导出的命令和解析函数。这样更换某个仪表
协议时不会同时改动框架生命周期。纯仿真模块没有物理仪表，可只保留
`module.toml + backend.py`。

除此以外，作者可自由增加界面、配置或辅助文件；不需要继承框架基类、Mixin，也不
要求更深的固定目录层次。

## 接口

必需：

- `open(api)`：Enable 时建立安全初始状态；不接收保存设置。
- `measure(slot, api)`：返回一行 Mapping。
- `close(api)`：Disable/退出时幂等地进入安全状态并释放资源。

可选：

- `configure(settings, api)`：用户明确 Apply 时调用。
- `on_event(event, data, api)`：统一处理 `run_start`、`run_end`、`status`、`action`。
- `slots`：正整数、正整数序列或动态 property。

`run_end` 的 `data["reason"]` 为 `completed`、`stopped` 或 `error`。`action` 的 data 为
`{"name": str, "payload": dict}`。

## 槽位、行与 rawdata

- `slots = 4` 等价于 `(1, 2, 3, 4)`。
- 核心取所有模块槽位并集，每个槽位写一行；同槽位的不同模块并行。
- 未声明 `slots` 的模块跟随每个槽位；所有模块都未声明时只有槽位 1。
- 模块内部不循环发多行；当前槽位就是 `measure(slot, api)` 的 `slot`。
- 未测量列省略；不要写文字占位。
- 需要原始序列时返回 `(row, raw_values)`，否则直接返回 row。

模块不直接写 DAT。数据异常时省略无效测量值，写模块自定义的数值状态码并调用
`api.warn(...)`；通信、协议或安全状态无法确认时抛 `ModuleError` 或其他异常。

## ModuleAPI

- `api.sleep(seconds)`：Pause 不计时、Stop 可打断；`sleep(0)` 为检查点。
- `api.devices()`：最新温度、磁场和 Monitor 快照副本。
- `api.warn(code, message, key="")`：报告 Warning；`message=None` 解除。
- `api.status(mapping)`：更新状态页。
- `api.timeout`：核心给本次调用的总时限。

每个 VISA/串口/厂商 SDK 调用必须自行设置有限 I/O timeout，并为输出关闭预留时间。

## 可选界面

`frontend.py` 中的 `Frontend` 是普通 QWidget，只需 `load(settings)` 和 `dump()`：

```python
from PySide6.QtWidgets import QWidget


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api

    def load(self, settings): ...
    def dump(self): return {}
```

可选提供 `status_widget`、`show_status(mapping)`，并用 `api.action(...)` / `api.refresh()`
向后端发请求。前端不连接仪表、不写文件、不控制温场，也不需要注册设置变化信号或
Run 状态钩子；这些由核心处理。

## 依赖

PySide6、PyVISA、QtAwesome、packaging 和 typing_extensions 使用主框架版本，模块不得
重复声明。只有额外库才写入 `dependencies`，并携带完整本地 wheel 与带 SHA-256 的
精确 `requirements.lock`。安装不访问网络。

## 安全与协议测试

真实模块必须保留：

- 身份、地址、量程、限流/限压、互锁与关键设置读回；
- 仪表命令顺序、响应解析、写 timeout 不重放；
- 正常、超量程、compliance、损坏响应和模块状态码；
- Pause/Stop、三种 run_end、重复 close、异常清理和资源释放；
- 槽位、空列、rawdata、有限数值和并行模块；
- 设置保存/SEQ 导入不自动 Apply。

当前硬件模块均未完成真机验证，仍按 Beta 对待。软件测试不能替代硬件保护、互锁和
人工急停。
