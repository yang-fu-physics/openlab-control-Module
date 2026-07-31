# Keithley 6517B High Resistance

这是使用 **Keithley 6517B** 内置 V-source，以 force-voltage / measure-current
（FVMI）方式测量两线高电阻的 Beta Measurement Module。模块读取实际 V-source 和电流，
使用 `R = V / I` 计算电阻。

尚未用真实 6517B、高压测试夹具、联锁、GPIB 控制器和样品验证，因此版本保持
`0.1.0b1`。自动化 fake-instrument 测试不能替代高压安全认证。

## 接线和固定 METER-CONNECT 条件

按 6517B 手册的 “Force voltage measure current - basic connections”：

- DUT 一端连接 `V SOURCE OUT HI`；
- DUT 另一端连接 `INPUT` triax 中心导体；
- `V-source LO` 必须通过 `METER-CONNECT` 在仪表内部连接到 `Ammeter LO`。

本模块不会把该连接做成可选设置。Apply Settings 固定发送
`SOUR:VOLT:MCON ON`，随后查询 `SOUR:VOLT:MCON?`；每次 Measure 打开 V-source
之前还会再次查询。任何读回不是 ON 的情况都是 Error，输出不会打开。

模块还固定发送 `SOUR:CURR:RLIM:STAT OFF` 并读回确认。该选项若为 ON，会在
`V SOURCE OUT HI` 串入 1 MΩ 保护电阻，直接用 V/I 会把它计入结果，不能代表 DUT
本身；因此它不作为可选设置保留前面板旧状态。

手册同时规定 V-source/Electrometer LO 相对机壳的最大共模电压为 500 V peak，并要求
危险电压使用安全屏蔽和联锁测试夹具。1000 V 档不等于可以省略联锁、屏蔽或额定电缆。
模块不提供绕过硬件联锁的命令；若仪表不能进入 operate，输出读回失败并中止 SEQ。

## 设置和硬件限制

- V-source range：100 V 或 1000 V；
- source voltage：不超过所选档位；
- hardware voltage limit：由 6517B 自己执行，必须不小于源值绝对值；
- current range：由 6517B 自动量程；
- NPLC 和 source settle 可配置。

手册给出的内置 V-source 普通电流限制为：100 V 档约 10 mA，1000 V 档约 1 mA。
模块读取 `SOUR:CURR:LIM:STAT?` 判断 current compliance。模块没有另加未经真实样品
验证的软件电压上限；这里的 voltage limit 是仪表自身的硬件配置。

## 生命周期和安全状态

- **Enable**：只加载设置和发现 GPIB，不连接。
- **Apply Settings**：连接、核对 6517B 型号，进入 standby，打开 zero check，配置并
  读回 V-source limit、METER-CONNECT、current measurement 和数据格式。
- **Measure**：本模块声明 `measurement_mode = "once_per_slot"`，所以核心在每个逻辑
  通道行都重新调用一次；再次确认全部配置和 METER-CONNECT，关闭 zero check，进入
  operate，等待并读取。
- **Stop / Error / completed**：standby + zero check ON，模块仍保持 Enabled。
- **Disable / 应用退出**：确认上述安全状态后释放 VISA session。

通信或设置错误、METER-CONNECT 不确定、standby/zero-check 无法确认都属于框架 Error。
单次读数 overrange、current compliance 或无法计算电阻属于 Warning，SEQ 继续。

Settings 中的 `Return to standby + zero check after each DAT row` 默认勾选：每行完成后
回到 standby 并确认 zero check ON。取消勾选后，V-source operate 和 zero check OFF 会在
成功行之间以及 SEQ Pause 期间保持；每个逻辑通道行仍重新读取一次。Stop、Error、
completed、Disable、通信异常和应用退出不受该选项影响，始终恢复 standby + zero check
ON。若同时启用四槽位扫描模块，一条 `T Measure` 因此会产生四个独立 6517B 读数；没有
扫描模块时只产生一个。

## DAT 状态码

| 状态码 | 含义 |
| --- | --- |
| `0` | 正常 |
| `1` | 读数 overflow/underflow、非有限或仪表哨兵值 |
| `2` | V-source current compliance |
| `3` | zero-check/out-of-limit/reference 等非正常状态，或电流为零 |

`StatusCode != 0` 时 `Resistance`、`Voltage`、`Current` 全部留空。正常行同时保存实际
V-source 电压和测得电流；DAT 中不写状态文字。

## 外部依赖

PyVISA 由主框架统一提供。Windows 还必须安装 NI-VISA、Keysight IO Libraries 等可用
VISA implementation；厂商 VISA 驱动不能由模块的 Python 依赖安装功能替代。
