# Keithley 2400 Resistance

这是面向 **Keithley Model 2400** 的 Beta Measurement Module。模块支持：

- 恒流源、测量电压并计算 `R = V / I`；
- 恒压源、测量电流并计算 `R = V / I`；
- 2-wire local sense；
- 4-wire remote sense。

尚未用真实 2400、GPIB 控制器和样品验证，因此版本保持 `0.1.0b1`。自动化测试只验证
协议状态机、异常路径和资源释放，不是仪表或接线的安全认证。

## 接线

2-wire 使用 `INPUT/OUTPUT HI` 与 `INPUT/OUTPUT LO`。4-wire 还必须把
`4-WIRE SENSE HI/LO` 接到 DUT 对应端。模块通过 `:SYST:RSEN OFF/ON` 选择两线或
四线；手册明确警告，4-wire 状态下断开 sense lead 可能使源端升高输出以补偿压降。

Model 2400 的绝对命令能力约为 ±210 V、±1.05 A，连续工作边界为 22 W。模块只验证
仪表合法命令范围，不提供未经真实样品验证的软件功率上限。实际接线、允许功耗、保险、
屏蔽和人员防护必须在仪表及实验流程中完成。

## 生命周期

- **Enable**：只加载 desired settings 并发现 GPIB 地址，不连接、不 Apply、不打开输出。
- **Apply Settings**：连接并核对 `*IDN?`，先关闭输出，再写入并读回源模式、源值、
  compliance、NPLC 和 sense mode；固定启用 concurrent V/I measurement，并移除可能
  遗留的其他测量函数；结束时再次确认 `OUTP? = 0`。
- **Measure**：本模块声明 `measurement_mode = "once_per_slot"`，所以核心在每个逻辑
  通道行都重新调用一次；确认设置未被前面板改变，打开输出，等待 settle，并读取
  电压/电流和 compliance。
- **Stop / Error / completed**：关闭并确认输出，但模块保持 Enabled 和连接状态。
- **Disable / 应用退出**：关闭并确认输出后释放 VISA session。

任何无法确认输出已经关闭、设置读回不一致、通信中断或型号不匹配都属于框架 Error，
会中止 SEQ。单次读数超量程、compliance 或无法计算电阻属于数据 Warning，写入状态行并
继续后续 SEQ。

## DAT 列和状态码

每次逻辑槽位调用写一行。若同时启用了四槽位扫描模块，一条 `T Measure` 会调用 2400
四次，并把四次新读数分别写入 CH1-CH4 对应的四行；没有扫描模块时只调用一次。

Settings 中的 `Turn output off after each DAT row` 默认勾选：每行发送前确认输出 OFF，
采样后关闭并读回。取消勾选后，输出会在成功行之间以及 SEQ Pause 期间保持活动，以免
每个通道行反复开关；下一行仍重新采样。无论该选项如何，Stop、Error、completed、
Disable、通信异常或应用退出都必须请求并确认输出 OFF。

DAT 列：

| 列 | 含义 |
| --- | --- |
| `Resistance` | `Voltage / Current`，单位 Ω |
| `Voltage` | 2400 返回的实际电压，单位 V |
| `Current` | 2400 返回的实际电流，单位 A |
| `StatusCode` | 非负整数数据状态 |

状态码：

- `0`：正常；
- `1`：仪表超量程哨兵值或非有限结果；
- `2`：当前源模式对应的 compliance 已触发；
- `3`：电流为零或返回格式无法形成有效电阻。

当 `StatusCode != 0` 时，`Resistance`、`Voltage`、`Current` 全部留空，避免异常读数被
误当成正式数据。数据问题同时以去重 Warning 报告；下一次正常读数会解除该 Warning。

## 外部依赖

PyVISA 由 OpenLab Control 主框架统一提供。Windows 仍需预先安装可用的 VISA
implementation（例如 NI-VISA 或 Keysight IO Libraries）；厂商驱动不是 Python wheel，
不能由模块离线依赖安装器代替。
