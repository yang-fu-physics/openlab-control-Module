# Keithley 6221 + 2182A Delta

版本 `0.2.0b1`。本模块用 Keithley 6221 电流源和通过其 RS-232/Trigger Link
连接的 2182A 纳伏表执行 Delta 测量，并可明确选择以下通道路由方式：

- `None`：不连接切换器，只允许 CH1；
- `Keithley 7001`：通过第二个 GPIB 地址控制 7001；
- `Keithley 3706A`：通过第二个 GPIB 地址控制 3706/3706A。

模块不会自动探测或在连接失败时静默降级。选择 7001 或 3706A 后，Apply 必须识别
对应型号，否则立即报告 Error。选择 None 时完全不打开切换器 VISA 会话。

2182A 不使用独立 VISA 地址。使用前应在仪表面板上把 2182A RS-232 设置为
19.2 kbaud、XON/XOFF 和 CR 终止，并让 6221 串口设置一致，同时连接 RS-232 与
Trigger Link。模块会验证 6221 报告 2182A 存在，并通过串口转发检查 2182A 身份；
不会擅自修改这些面板通信设置。

## 两种运行模式

- Shared configuration / stay Armed：`run_start` 写入公共配置、发送 ARM、等待至少
  3 秒并读回确认。整个 SEQ 保持 Armed，每个逻辑槽位只切换路由并软件触发一次。
- Independent configuration / re-arm each channel：每次切换前 Abort/Clear 并确认
  零电流和输出关闭；切换后写入该通道完整配置，再 ARM、等待 3 秒、确认并触发。

动态 `slots` 让核心为每个 Enabled 逻辑通道调用一次 `measure(slot, api)`，因此
CH1-CH4 各占一行，并与其他四槽位扫描模块按槽位对齐。DAT 列为通道号、电阻、有效
反转电流、电阻标准差、有效样本数和模块状态码。2182A 原始电压序列逐行写入
`rawdata`，与正式 DAT 行一一对应。Delta count 最大 32,768，以保证 IPC 帧有界。

## 路由配置

物理接线只保存在 `routing.toml`，界面不显示具体触点。文件同时包含：

- `[switchers.7001.channels]`：7001 完整 Channel List 地址；
- `[switchers.3706a.channels]`：3706A 四位全局通道号。

安装的卡型、槽位或接线变化时，修改对应分区并重启程序。模块不接受旧的单
`[channels]` 格式，也不猜测卡型。每次触发前执行 break-before-make：先打开全部
触点并严格读回，再闭合目标四路并要求整机闭合状态精确匹配。运行中的任何路由通信
或读回错误都会终止 SEQ；写结果不确定时不会自动重发。

## 数据状态与安全边界

`StatusCode` 只写数值：

- `0`：正常；
- `1`：有限 2182A 电压超出支持量程；
- `2`：预留给经过真机确认的 6221 compliance 事件；
- `3`：Delta trace 无效、非有限或样本数不完整。

状态非零时 `Resistance` 和 `StdDev` 留空。数据质量问题调用去重 Warning 并继续
SEQ；通信、身份、配置读回、路由、触发状态或安全输出无法确认时报告 Error 并终止。

Apply 前后、Stop、Error、Disable 和正常 SEQ 结束均执行 Abort/Clear、确认输出关闭
及零电流，并在存在切换器时打开全部触点。模块没有单通道 timeout，也没有 DUT 软件
电流/compliance 上限；长 `*OPC?` 使用核心 Measure 总预算并预留安全清理时间。仍会
按 6221 手册拒绝仪表自身不支持的命令范围，但这不等于样品安全认证。

本版本尚未用真实仪表、开关卡、DUT、线缆和 GPIB 控制器验证，仍按 Beta 使用。

## 代码边界

- `backend.py`：生命周期、槽位调度、超时、报警与安全清理；
- `keithley_6221.py`：6221 VISA 与 Delta SCPI；
- `keithley_2182a.py`：2182A 串口命令和 trace 解析；
- `keithley_7001.py`：7001 路由协议；
- `keithley_3706a.py`：3706A TSP 路由协议；
- `frontend.py`：设置与状态界面；
- `routing.py` / `routing.toml`：隐藏物理路由的读取和校验。
