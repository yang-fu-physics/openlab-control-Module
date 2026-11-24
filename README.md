# OpenLab Control — Measurement Modules（测量模块仓库）

本仓库维护 **OpenLab Control** 配套的 **Measurement Module** 插件，用于执行电输运与物性测量。主框架负责进程隔离、SEQ 调度、Pause/Stop、多模块并行与 DAT 写入；每个模块专注管好自己的仪表协议、通道逻辑、数字状态码与输出安全收尾。

> 如果你在找温控仪、磁体电源或压缩机开关，请前往 [openlab-control-Device](https://github.com/yang-fu-physics/openlab-control-Device)。

## 当前包含的模块

| 目录 ID | 适用仪表与功能 | 实机验证状态 |
| --- | --- | --- |
| `keithley_6221_2182a_delta` | Keithley 6221 + 2182A Delta 电阻测量（可选 7001 / 3706A 切换器） | **已实机验证（2026-11-05）**（仅限已测接线与配置） |
| `keithley_6221_ac` | Keithley 6221 独立正弦 AC 电流源（扫流、扫频、Phase Marker） | Beta（仅仿真/协议测试） |
| `oe1022d_lockin` | OE1022D 双通道锁相放大器（外参考/单机 Sine Out、1f/2f、软件 Auto Range） | Beta（已有 USB 空载写后回读及部分实机验证，仍按 Beta 对待） |
| `keithley_2400` | Keithley 2400 两线/四线恒流或恒压电阻测量 | Beta（仅仿真/协议测试） |
| `keithley_2614b` | Keithley 2614B 双通道 SMU A/B 电阻测量 | Beta（仅仿真/协议测试） |
| `keithley_6517b` | Keithley 6517B 高阻测量（内置电压源 FVMI） | Beta（仅仿真/协议测试） |
| `lakeshore_372a` | Lake Shore 372 / 372A 交流电阻电桥（1–16 通道扫描） | Beta（仅仿真/协议测试） |
| `lr700` | Linear Research LR-700 + LR-720-16 交流电桥（通用／独立固定配置、按电流上限生成非标 `T=4` Trace、固定基准 ×10） | Beta（已有早期命令读写测试；本次重构与 ×10 仅离线验证，尚未接样品复测） |
| `simulated_transport` | 无需硬件的四通道模拟电阻模块 | 教学与仿真测试用 |

> 接真机前请先阅读对应模块目录中的 `README.md`，并在假负载上完成低风险验证。软件测试不能替代硬件联锁、限流/限压和人工急停。

## 离线安装（复制目录即可）

1. 将需要的模块完整目录复制到主程序的 `OpenLabControl/modules/<模块ID>/`。
2. 确保已通过 **Instrument Scanner** 扫描并保留了该仪表的 Measurement VISA 资源（写入 `configs/visa.resources.toml`）。
3. 重启 OpenLab Control，在工具栏 **Modules** 中勾选启用（所有模块启动默认 Disabled）。
4. 打开模块窗口的 **Settings** 页选择资源 ID 与参数，核对无误后点击 **Apply Settings** 下发。

> 依赖说明：`PySide6`、`PyVISA`、`QtAwesome`、`packaging` 和 `typing_extensions` 由主框架统一锁定提供，各模块不再重复安装依赖。

## 模块组织方式与最小骨架

目录名即模块 ID，入口固定为 `backend.py` 里的 `Module` 类（**无需继承框架基类**）：

```text
modules/my_meter/
├─ module.toml        # 仅需 name 和 version
├─ backend.py         # 生命周期、流程编排、超时、告警与安全收尾
├─ my_meter.py        # 底层 VISA/串口命令与响应解析（一台仪表一个文件）
└─ frontend.py        # 可选：PySide6 设置与状态界面
```

```python
from labcontrol.measurement.visa import VisaSession
from labcontrol.module_api import ModuleAPI
from . import my_meter


class Module:
    columns = {"Resistance": "Ohm", "StatusCode": ""}
    display_columns = ("Resistance",)  # 可选：主窗口左侧实时监视卡显示列

    def open(self, api: ModuleAPI):
        self.instrument = None
        api.resources()  # 从核心读取扫描器确认的资源表，不自行全盘扫描 VISA

    def configure(self, settings, api: ModuleAPI):
        address = api.resource_address(str(settings["resource"]))
        self.instrument = VisaSession(address, 3.0)
        # 此处须核对 *IDN?、写入安全设置并回读确认

    def measure(self, slot: int, api: ModuleAPI):
        api.checkpoint()
        return {
            "Resistance": my_meter.parse_number(self.instrument.query(my_meter.READ)),
            "StatusCode": 0,
        }

    def close(self, api: ModuleAPI):
        if self.instrument is not None:
            self.instrument.write(my_meter.OUTPUT_OFF)
            self.instrument.close()
            self.instrument = None
```

### 核心开发约定

- **协议与编排分离**：每台物理仪表单独放一个同名 `.py` 文件（多仪表模块也只保留一个 `backend.py`）。仪表文件不调用 `ModuleAPI`，`backend.py` 不直接拼 SCPI/TSP 字符串。
- **VISA 资源与共享会话**：统一使用 `labcontrol.measurement.visa.VisaSession`（只关本句柄，不关进程共享 ResourceManager）；设置键用 `resource`、`resource_*` 或 `*_resource` 保存稳定资源 ID，Apply 时由核心按实际地址检查跨模块占用。
- **输出收尾原则**：会打开输出的模块必须处理 `on_event("run_end", data, api)`（`reason` 为 `completed` / `stopped` / `error`），默认一律关闭输出；确需跨 SEQ 保持连续偏置（如栅压）时，须提供默认勾选的“SEQ 结束关闭输出”选项，取消后仅在读回确认一致时保持，且 Disable / 重新 Apply / 退出 / 异常时仍须关闭。
- **通道槽位与数据**：声明 `slots` 的模块由核心按槽位并集逐行调用，未测列直接省略，异常点写整数状态码并 `api.warn()`，需要原始序列时返回 `(row, raw_values)`；通讯或安全状态不明时抛 `ModuleError` 停机。
- **上游驱动参考**：编写新驱动时优先对照 [PyMeasure](https://pymeasure.readthedocs.io/en/latest/api/instruments/index.html) 或 [QCoDeS](https://microsoft.github.io/Qcodes/drivers_api/index.html) 核对命令与固件差异（如 2400/6517B/6221 参考 PyMeasure，2614B/372 参考 QCoDeS，LR-700/OE1022D 按厂商手册维护），移植命令时保留出处，不引入整套第三方框架依赖。
- **参数精度与 UI**：可编辑等待/超时和常规 NPLC 保留 1 位小数（Delta PLC、372A 整秒参数保留整数）；SI 电流/电压在缩短会丢精度时保留原文本；前端可使用核心的 `PrecisionDoubleSpinBox` 保持与核心一致。

## 运行测试

```powershell
<OpenLabControl>\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

> 说明：`tests/test_instrument_file_layout.py` 为函数式布局与协议隔离检查（不会自动被 `unittest discover` 发现），提交硬件模块改动时请额外运行该文件确认无违例。

真实硬件模块须覆盖：身份/量程/限流/互锁校验、命令顺序、写超时不重放、超量程/compliance/乱码与状态码、Pause/Stop、三种 `run_end`、重复 `close`、资源释放以及导入设置不自动 Apply。
