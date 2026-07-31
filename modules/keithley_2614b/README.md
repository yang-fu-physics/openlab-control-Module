# Keithley 2614B Dual-Channel Resistance

这是面向 **Keithley Model 2614B** 两个独立 SMU 通道（SMU A / SMU B）的 Beta
Measurement Module。每个通道可独立选择：

- 恒流源，读取电压和电流，计算 `R = V / I`；
- 恒压源，读取电压和电流，计算 `R = V / I`；
- 2-wire local sense 或 4-wire remote sense；
- 独立 source level、compliance 和 NPLC。

尚未用真实 2614B、GPIB 控制器、双通道接线和联锁测试夹具验证，因此版本保持
`0.1.0b1`。自动化测试验证 TSP 状态机和异常清理，不是硬件安全认证。

## 测量顺序

本模块声明 `measurement_mode = "once_per_slot"`。每个逻辑槽位调用都会一次完成全部
Enabled SMU 通道，并写一个宽表行；A/B 不是核心用来对齐其他扫描模块的逻辑槽位。
若同时启用了四槽位扫描模块，一条 `T Measure` 会调用 2614B 四次，每次都重新读取
Enabled 的 A/B；没有扫描模块时只调用一次。

每次调用按以下顺序执行：

1. 确认 A/B 输出均为 OFF，Enabled 通道配置与读回一致；
2. 依次打开 Enabled 通道，因此 settle 阶段所有 Enabled DUT 同时保持偏置；
3. 共同等待一次 `settle_seconds`；
4. 按 A、B 顺序分别读取 V、I 和 `source.compliance`；
5. 关闭 A/B 并逐个读回确认 OFF；
6. 将 A/B 结果合在当前逻辑槽位的一个宽表行中；Disabled 通道的整组列留空。

读取 V 和 I 使用同一条 TSP 请求中的连续测量函数。两个 DUT 在读取期间都保持偏置，
但 A/B 的 ADC 转换并非严格同时发生。

## 输出边界与联锁

2614B 用户手册给出的能力包括约 ±202 V、±1.515 A、每通道 30.603 W。模块按手册
maximum limits 检查组合：

- 源电流绝对值超过 100 mA 时，voltage limit 不得超过 20 V；
- 源电压绝对值超过 20 V 时，current limit 不得超过 100 mA；
- 其他设置仍不能超过仪表绝对命令范围。

200 V source range 只有物理 interlock 有效时才能打开。模块没有联锁绕过设置，也不会
模拟联锁信号；若输出读回不是 ON，Measure 立即 Error 并关闭两个通道。危险电压必须
使用额定接线、安全屏蔽、接地和闭合的联锁测试夹具。

模块不另加未经真实样品验证的软件安全上限。这里的 current/voltage limit 是 2614B
硬件 compliance，真实样品允许的边界仍需操作员在实验流程中确定。

## 生命周期

- **Enable**：只加载设置和发现 GPIB，不连接。
- **Apply Settings**：连接并验证型号，先关闭 A/B，设置 `OUTPUT_HIGH_Z`，再配置和
  读回两个 Enabled 通道；结束保持全部输出 OFF。
- **Measure**：默认只在采样事务内输出；取消逐行关闭选项时可跨成功行保持 Enabled
  输出。Stop、通信异常或任一通道失败会直接请求 A/B OFF。
- **completed / stopped / error**：严格读回 A/B 都是 OFF，连接保持。
- **Disable / 退出**：确认 A/B OFF 后关闭 VISA session。

Settings 中的 `Turn SMU A/B outputs off after each DAT row` 默认勾选。取消勾选后，
Enabled 输出会在成功行之间以及 SEQ Pause 期间保持活动；每个逻辑槽位仍重新读取 A/B。
Stop、Error、completed、Disable、通信异常和应用退出始终请求两个输出 OFF。

## DAT 列和状态码

一个 DAT 行包含两组固定列：

| SMU A | SMU B |
| --- | --- |
| `R1`, `Voltage1`, `Current1`, `StatusCode1` | `R2`, `Voltage2`, `Current2`, `StatusCode2` |

Disabled 通道的四列全部为空。Enabled 通道的状态码分别定义如下：

- `0`：正常；
- `1`：非有限、仪表哨兵或计算 overrange；
- `2`：该通道 `source.compliance` 为 true；
- `3`：电流为零或响应不能形成有效电阻。

`StatusCode1/2 != 0` 时只保留该通道的状态码；对应 `R`、`Voltage`、`Current` 留空。
一个通道的数据异常只产生去重 Warning，不中止另一个通道或 SEQ；通信、配置、输出与
安全状态异常属于 Error。

## 外部依赖

PyVISA 由 OpenLab Control 主框架统一提供。Windows 仍需预先安装 NI-VISA、Keysight
IO Libraries 等 VISA implementation；厂商驱动不是可由模块安装器代替的 Python wheel。
