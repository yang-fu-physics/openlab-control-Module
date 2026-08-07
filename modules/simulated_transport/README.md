# Simulated Transport

无硬件的四槽位示例。`slots = 4`，因此核心调用 `measure(slot, api)` 四次，
分别写 R1–R4 四个稀疏行。

模块自定义 `StatusCode`：0 正常；1 表示模拟电阻超过 `warning_threshold_ohm`。状态 1
同时报告去重 Warning，并让当前 R 值留空。该语义属于本模块，不是核心统一规则。
