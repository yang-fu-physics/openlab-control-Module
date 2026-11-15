# Keithley 6221 AC Current Source

独立 AC 源模块，适用于 OE1022D、SR830 等外参考锁相仪。模块只连接 6221；没有 SR830
驱动，也不应与 Delta 模块同时占用同一台 6221。Enable/载入不写仪表；Apply 校验身份并
确认关断，不开启激励。填写样品 RMS 电流上限、**最大步进电流**、**步进间隔**、电压
compliance、频率和物理接线对应的 Phase Marker 输出线。

```text
T Module Scan "keithley_6221_ac" "scan_current" {"points":["100u","200u","300u","0"],"settle_seconds":3.0}
T     Measure
T End Scan
```

也可加入 `Scan Frequency`。参数支持 Hz、mHz 和 kHz（例如 `137.77`、`1kHz`），
每点改变频率后等待，再执行子指令。如果已经设了非零电流，扫频保持该幅值和 Marker，
不关断或重新 ARM；如果输出本来是关的，就只更新后续扫流会使用的频率。要以固定
电流扫频，外层放只有一个点的 `Scan Current`，内层放 `Scan Frequency` 和 `Measure`。
扫频上限还要考虑读数仪表的参考和谐波范围，例如 OE1022D 的 (2f) 不得超过 102 kHz。

- 首个非零点以最小正弦幅值启动。接下来保持输出和固定量程，只逐级改变幅值；
  软件相邻设定点之差不超过配置的步进，达到测量点后再等 SEQ 参数里的稳定时间。
- 负点只将共同参考 Marker 切至 180°，不是实际波形的反相。跨符号先降到最小幅值，
  切 Marker 再升；不会在大电流处直接切相位。明确扫描到零时，降到最小幅值再关闭，
  因此零点没有来自 6221 的参考脉冲。
- 正常结束先逐步降幅再关断。通讯失败、操作中止或无法读回时优先关断并确认；
  不会为了继续扫描而自动重启失效的波形。Disable 也确认输出关闭。
- `Measure` 不重新设置电流，只读回源状态，记录带符号的 RMS 设定、仪表读回的 RMS
  幅值、频率、Marker 和 compliance。V/I 留给后处理。

依据 [6221 Reference Manual，第 7 章波形命令](https://download.tek.com/manual/622x-901-01%28B-Jun2005%29%28Ref%29.pdf#page=233)。
SCPI 命令和范围也对照了 [PyMeasure 的 6221 驱动](https://github.com/pymeasure/pymeasure/blob/master/pymeasure/instruments/keithley/keithley6221.py)，
没有新增 PyMeasure 运行时依赖。
当前只通过模拟测试：软件不在非零点之间发送关闭或重启命令，不等于已验证仪表输出无瞬态。
**首次请先接耐受已知的假负载，用示波器确认调幅过程和零点关断，再接样品。**
