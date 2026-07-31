# OpenLab Measurement Module 统一开发规范

> - 文档状态：当前仓库的规范性开发基线
> - 规范版本：1.1
> - 适用接口：OpenLab Control Measurement Module API 1.1
> - 基线日期：2026-07-31
> - 参考实现：`lakeshore_372a`、`lr700`、`keithley_6221_2182a_delta`、
>   `keithley_6221_2182a_delta_3706a`、`keithley_2400`、
>   `keithley_6517b`、`keithley_2614b`

本文件用于统一后续 Measurement Module 的目录、清单、生命周期、数据、安全、界面、
依赖、测试和发布方式。它不替代仪表手册，也不证明模块已经通过真实仪表验证。

文中的“必须/不得”是仓库准入要求，“应/不应”是默认要求，只有在模块 README 中写明
理由并补充相应测试后才可偏离，“可以”表示受支持的设计选择。OpenLab Control 的
`measurement/api.py`、`frontend_api.py`、manifest 和 IPC 校验器是可执行接口边界，
本文件是模块仓库的开发与发布政策。二者冲突时必须暂停发布，把冲突当作缺陷处理并在
同一次变更中同步，不能让任一方被静默忽略。

## 1. 边界与既定行为

Measurement Module 表示“一套完整测量方案”，可以拥有一个或多个测量仪表、切换器和
内部触发链。它与温度、磁场 Device Plugin 的边界如下：

- 模块可以读取核心提供的温度、磁场和 Monitor 快照，但不得设置温度或磁场。
- 一个物理测量仪表只能由一个模块拥有。不得同时把它交给另一个模块或 Device Plugin。
- Frontend 只负责界面；全部 VISA、串口、SDK 和仪表状态都属于独立 backend 进程。
- 所有模块每次启动默认 Disabled。Enable 打开模块窗口并调用 `initialize`，但不会
  自动 Apply 已保存或随 SEQ 导入的设置。
- `Apply Settings` 只由核心放在 Settings 页。模块窗口不得再提供第二个 Apply 按钮。
- 模块窗口不能由用户直接关闭；Disable 成功后由核心隐藏。
- 一个 `T Measure` 会先按所有扫描模块的逻辑槽位并集展开；每个逻辑通道槽位对应一行
  DAT。同一槽位中的 Enabled 模块并行执行，同一模块内部的生命周期调用串行执行。
- 每次 `measure(context)` 调用必须且只能产生一行。扫描模块由核心按槽位分别调用，
  不得在一次调用中自行循环发出多行。
- Warning 表示仍可安全继续且数据语义明确；核心继续 SEQ 并按
  `source + code + context` 去重。Error 中止 SEQ。
- 测量数据问题只影响当前数据行并报告 Warning，不中止 SEQ。只有通信、进程、协议
  状态、设置状态、路由或安全状态等系统问题才触发 Error。
- Stop 结束本次模块运行状态，但不会 Disable 模块。温度和磁场保持由核心及其
  Device Plugin 负责；模块只处理自身输出、切换器和资源。

## 2. 现有硬件模块形成的参考模型

| 模块 | 仪表拓扑 | 配置方式 | SEQ 前准备 | DAT 行模型 | 模块安全状态 |
| --- | --- | --- | --- | --- | --- |
| Lake Shore 372A | 单台电阻桥及其输入扫描器 | `aligned_slots`；R1-R4 各自保存输入、激励和量程 | 确认所有相关输入分流 | 每个启用逻辑槽位一行；只填写该槽位的 R/Phase/Current，加整数 StatusCode | 所有相关输入 excitation shunted，并读回确认 |
| LR-700 + LR-720-16 | 电阻桥加十六路复用器 | `aligned_slots`；R1-R4 保存各自参数，但仪表参数是全局的 | 确认最低可验证激励 | 每个启用逻辑槽位一行；只填写该槽位的 R/X，加整数 StatusCode | `20 uV × 5% = 1 uV` 最低激励，并读回确认 |
| Keithley 6221 + 2182A + 可选 7001 | 电流源、纳伏表和切换器组成的多仪表触发链 | `aligned_slots`；可选共享持续 Armed 或每通道重新 ARM | 共享模式 ARM 后可中断等待 3 s 并确认 Armed | 每个启用逻辑槽位一行；Channel + Resistance/Current/StdDev/SampleCount/StatusCode | Abort Delta、输出关闭、必要时打开路由，并查询确认 |
| Keithley 6221 + 2182A + 可选 3706A | 与上一项相同，但切换器使用区分大小写的 TSP | 与 7001 版本相同 | 与 7001 版本相同 | 与 7001 版本相同，并保持独立 module ID/rawdata 文件 | Abort Delta、输出关闭、`channel.open("allslots")`，并用整机 closed-channel list 精确确认 |
| Keithley 2400 | 单通道 SMU | `once_per_slot`；恒流/恒压和 2-wire/4-wire 可选 | 确认配置读回一致且输出为 OFF | 每个逻辑槽位重复测一次；Resistance/Voltage/Current/StatusCode | 默认每行前关闭；可选跨成功行保持输出；所有结束路径强制 OFF |
| Keithley 6517B | 内置 V-source + electrometer | `once_per_slot`；固定恒压、两线 FVMI | standby、zero check ON，并确认 METER-CONNECT | 每个逻辑槽位重复测一次；Resistance/Voltage/Current/StatusCode | 默认每行后 standby + zero check；可选跨成功行保持输出；结束路径恢复安全状态 |
| Keithley 2614B | 双通道 SMU A/B | `once_per_slot`；一次调用读取全部 Enabled SMU 通道 | 确认 A/B 均 OFF 和全部 Enabled 通道配置 | 每个逻辑槽位一行宽表；R1/V1/I1/StatusCode1 + R2/V2/I2/StatusCode2 | 默认每行后 A/B OFF；可选跨成功行保持输出；结束路径全部 OFF |

这些实现说明下列差异是允许的：

- 单仪表、多路复用仪表和多仪表编排都可以由一个模块实现。
- 通道设置可以是真正独立，也可以只是扫描计划，测量时再写入全局仪表参数。
- DAT 可以使用“稀疏宽表”“按通道标准行”或“一次调用汇总多个内部通道的宽表”，但
  必须在开发开始时选定并保持固定。
- 安全状态不一定等于零输出；无法真正关闭输出的仪表必须定义并确认最低风险状态。
- 可选附件在 Enable 阶段可以有明确的降级模式；运行中状态不确定时不得静默降级。

这些模块是协议和架构模式的参考，不自动等于对本规范全部条款的永久合规证明。规范或
模块变化后必须重新运行合规检查，并在模块 README 记录暂时偏离的条款、理由和测试。

## 3. 标准目录

每个可独立复制安装的模块必须位于 `modules/<module_id>/`：

```text
modules/<module_id>/
├─ module.toml             必须：清单、版本、入口、固定 DAT 列
├─ backend.py              必须：仪表通信和生命周期
├─ frontend.py             必须：Settings/Status 页面
├─ constants.py            应有：协议枚举、限值、安全默认值
├─ README.md               必须：接线、设置、数据、安全和验证状态
├─ quantities.py           可选：单位或 SI 数值解析
├─ routing.py              可选：隐藏路由配置的读取和校验
├─ routing.toml            可选：现场基本不变且不适合放入 UI 的硬件拓扑
├─ requirements.lock       仅额外第三方依赖需要
└─ wheels/                 仅额外第三方依赖需要

tests/
└─ test_<module_id>.py     必须：模块独立测试
```

`__init__.py` 可以存在，但不得在 import 时连接仪表、启动线程、创建 Qt 窗口或修改
全局状态。仪表协议映射只能维护一份；前后端和测试应复用 `constants.py`，不得各自复制
一套量程或状态位表。

不得提交以下内容：

- `module_data`、`plugin_runtime`、`plugin_state`、缓存和采集 DAT；
- QQ/HTTP token、密码、私钥或其他凭据；
- 带有秘密信息的仪表地址；
- 未在清单中使用的旧后端、重复协议表、临时脚本和构建产物。

## 4. `module.toml` 规范

### 4.1 基本模板

下面的 core/PyVISA 范围只表示本文基线时的示例。创建模块时必须按实际使用的 API 和
当时核心提供的版本重新确定，不能无检查复制。模块不使用 PyVISA 时应删除该依赖。

```toml
id = "example_bridge"
name = "Example Bridge"
version = "0.1.0b1"
api_version = "1.1"
core_requires = ">=0.11.5,<0.12"
frontend = "frontend:ExampleBridgeFrontend"
backend = "backend:ExampleBridgeBackend"
backend_type = "python"
measurement_mode = "once_per_slot"
dependencies = [
    "PyVISA>=1.16.2,<1.17",
]

[[columns]]
name = "Channel"

[[columns]]
name = "Resistance"
unit = "Ohm"

[[columns]]
name = "StatusCode"
```

### 4.2 强制规则

- `id` 必须匹配 `[a-z][a-z0-9_]*`，并在首次发布后保持不变。
- `version` 必须是合法 PEP 440 版本；未经过真实仪表验证时使用 beta。
- `api_version` 当前必须是 `"1.1"`。
- 正式模块必须显式声明 `measurement_mode = "once_per_slot"` 或 `"aligned_slots"`。
  为了让尚未迁移的第三方模块仍可被检查，缺少该字段时核心会显示 Warning，并按
  `once_per_slot` 处理；该兼容兜底不代表模块符合本仓库发布规范。
- `core_requires` 必须覆盖实际使用的最早核心版本，不得为了方便写成无限制范围。
  使用 `raw_values` 的模块当前至少需要提供该 API 的核心版本。
- `frontend` 和 `backend` 必须是 `文件名:类名`，不能包含路径；对应 `.py` 文件必须存在。
- `backend_type` 当前只能是 `"python"`。
- `dependencies` 必须是合法 PEP 508 requirement，不允许 URL。
- 至少声明一列。列名必须唯一、非空、单行且不含逗号。
- 列在 Run 开始前固定；运行中不得增加、删除或重命名动态列。
- 列名一经发布即视为数据接口。应使用稳定的 ASCII 名称，单位放在 `unit`，不要把
  单位重复写进 `name`。
- 模块只声明自己的列。核心写盘时自动加 `<module_id>.` 前缀。

任何源码或配置文件变化都会改变模块指纹。现场修改 `routing.toml` 后应重启或刷新并
重新确认信任，不能假设旧信任自动覆盖新内容。

## 5. 依赖与离线安装

当前核心统一提供并锁定以下公共依赖。此表是便于阅读的基线快照，不是第二个版本来源；
真正来源是 OpenLab Control 的 `FRAMEWORK_DEPENDENCY_VERSIONS`，核心依赖升级时必须由
自动检查或同一次变更同步本表和所有 manifest：

| 依赖 | 当前核心版本 |
| --- | --- |
| PySide6 | 6.11.1 |
| QtAwesome | 1.4.2 |
| packaging | 26.2 |
| PyVISA | 1.16.2 |
| typing_extensions | 4.16.0 |

模块必须接受核心提供的同一版本，不能携带私有副本覆盖它。模块可在 `dependencies`
声明兼容范围，例如 PyVISA；若范围不接受核心版本，模块应在导入源码前被拒绝。
PySide6 等接口固有依赖可以直接使用；只有确实依赖特定版本特性时才需要额外声明范围。

只有核心没有提供的第三方包才属于“额外依赖”，并且必须：

1. 在 `module.toml` 声明全部直接依赖；
2. 在 `requirements.lock` 固定所有传递依赖的精确 `==` 版本和 SHA-256；
3. 携带目标 Windows/Python 架构的全部 wheel；
4. 使用核心的离线 `Install Dependencies`，不得回退到网络；
5. 在干净、断网环境验证安装和完整生命周期。

NI-VISA、Keysight VISA 等厂商 VISA implementation 是系统驱动，不是 Python wheel。
模块 README 必须将其列为外部前置条件，但不能伪装成可由模块安装的依赖。

## 6. Backend 状态与生命周期

Backend 应显式区分以下状态：

- `desired_settings`：界面或 SEQ 导入的期望值；
- `applied_settings`：已完整写入并读回确认的设置；
- transport/driver 句柄：当前实际连接；
- `sequence_active`、`armed`、当前通道等运行态；
- 最近一次只读状态和测量摘要。

`applied_settings` 表示用户确认的完整方案已经通过后端验证，并且仪表已经建立了该
方案要求的安全基线。它不一定表示所有逐通道扫描参数此刻同时存在于仪表中：对于只有
一套全局参数的扫描仪表，逐通道值可以在 Measure 切换通道后再写入。无论哪种模型，
`applied_settings` 都只能在整个 Apply 成功后一次性更新；Apply 中途失败时必须保留
“未应用”语义，不能让部分写入看起来像成功。

### 6.1 生命周期表

| 阶段 | 必须执行 | 不得执行 |
| --- | --- | --- |
| import / `__init__` | 只建立内存状态和可注入 transport factory | 连接仪表、枚举 VISA、创建窗口、启动后台线程 |
| `initialize(settings, context)` | 规范化 desired settings、发现资源、返回初始状态 | 自动 Apply 保存设置、打开非零输出、改变路由或量程 |
| `apply_settings(settings, context)` | 后端完整验证方案、连接和识别仪表、建立并确认安全基线；立即生效的设置必须写入并读回，扫描计划可以延迟到 Measure | 信任前端校验、打开工作激励后留在样品上、失败后仍记录 applied |
| `begin_sequence(context)` | 确认已 Apply、建立本 Run 临时状态；准备时间可 Pause/Stop | 依赖第一次 Measure 才发现基本设置无效 |
| `measure(context)` | 只测 `context.measurement_step.logical_slot` 对应的一次测量单元；逐通道参数在使用前写入并读回；用返回 Mapping 或一次 `emit_row` 产生恰好一行固定 Schema 数据 | 自行循环多个槽位、直接写 DAT、重入同一模块、返回未声明列 |
| `end_sequence(reason, context)` | 对 completed/stopped/error 都处理本模块危险输出，清除运行态 | 自动 Disable 或假装未确认的安全动作成功 |
| `abort(context)` | 幂等地进入安全状态并释放全部资源；即使安全动作失败也清理本机引用 | 把 worker 退出等同于仪表已安全 |
| `read_status(context)` | 只读实际状态；无连接时明确报告 Unknown/Disconnected | 隐式连接、Apply、切换通道或打开输出 |
| `manual_action(action, payload, context)` | 仅处理 Idle 手动操作，使用显式 payload | 写实验 DAT、偷偷保存/Apply 整组设置 |

Enable 阶段允许为了确认“可选附件是否存在”而建立临时只读连接，但必须满足：

- 只执行身份或无副作用状态查询；
- 用有限超时；
- 无论成功失败都关闭临时句柄；
- 缺失后的降级方式固定并向用户 Warning；
- 降级状态在 Disable/再次 Enable 前不悄悄改变；
- 如果附件在 Apply 或 SEQ 运行中变得不确定，按设计报告 Error，不得继续在未知路由
  或未知输出上测量。

除上述明确的临时只读探测外，Enable 不应仅因加载了保存设置就连接主测量仪表。若某个
模块确实必须在 Enable 保持只读主连接，必须在 README 说明原因、保证不发送设置或输出
命令，并测试 Enable、Disable 和应用退出的全部资源释放路径。

### 6.2 逻辑槽位与测量模式

每个模块必须在 manifest 选择一种调度模式：

- `once_per_slot`：模块没有需要与其他扫描模块对齐的外部扫描通道。核心在本次
  `T Measure` 的每个逻辑槽位调用一次。2400、6517B，以及一次调用同时读取 A/B 的
  2614B 都属于此类。如果本次 Run 没有任何 `aligned_slots` 模块，唯一逻辑槽位为 1，
  因而这些模块只调用一次。
- `aligned_slots`：模块拥有需要跨模块对齐的扫描槽位。`begin_sequence` 成功后，核心
  调用一次 `measurement_slots(context)`；模块返回本次 Run 启用的唯一正整数槽位。
  这些列表在 Run 内冻结，模块只能在重新 Apply 并开始下一次 Run 后改变它们。

核心取所有 `aligned_slots` 列表的并集并按数值升序执行。例如 A 模块启用 `[1, 3, 4]`，
B 模块启用 `[1, 2, 4]`，本次 `T Measure` 仍写 4 行：第 2 行只有 B 的扫描结果，第 3 行
只有 A 的扫描结果；`once_per_slot` 模块四行都会重新测量。这里“一轮”只表示一个逻辑
通道槽位，不表示整个 `T Measure`。

同一槽位的参与模块并行完成后，核心把它们合入该槽位对应的一行。CH1–CH4 永远是四行，
不会合成一行。未启用当前槽位的扫描模块只留下空列。Stop 若发生在槽位执行中，核心不写
该槽位的半成品行；已经完成的前面槽位保持在 DAT 中。

## 7. 设置模型与校验

### 7.1 默认值

`default_settings()` 每次必须返回全新的可变对象，不能让多个窗口共享同一字典。默认值
必须满足：

- 需要操作员选择通信资源时，默认资源为空；固定 SDK 或自动发现模块可以没有资源字段；
- 多通道模块至少提供一个合理的逻辑通道供编辑，通常只默认 Enabled CH1/R1；无通道
  模块不需要虚构 Channel；
- 主动输出模块默认零输出；不能真正关闭的仪表使用最低风险值；
- 若设计决定提供模块软件安全上限，默认值取保守值并在 README 标记“待真实样品
  确认”；明确不提供这类上限的模块不得保留隐藏或失效字段，并必须说明只校验仪表合法
  命令范围、真实样品边界由前面板设置/硬件互锁和人工流程负责；
- 时间和计数不会超过核心默认操作时限；
- 默认设置本身不代表已经 Apply。

### 7.2 后端是最终裁决者

Frontend 校验只用于改善操作体验。Backend 在 Apply 前必须独立验证：

- Mapping、bool、int、float 和字符串的准确类型；
- 所有数值有限，拒绝 NaN/Infinity；
- 正数、非负数、枚举、计数和仪表硬件范围；
- 模块具有通道或路由时，验证通道号、物理输入和路由唯一性；提供通道 Enable 开关时，
  还要验证至少一个通道 Enabled；
- 模块具有主动设置时，验证激励与量程、滤波与等待、模式与通道配置等交叉约束；
- 仪表绝对合法范围；若模块设计声明软件安全上限，还要验证其本身及所有通道不能绕过；
- 最坏测量时长不超过 `context.operation_timeout_seconds`。

布尔值不能因 Python 中 `bool` 是 `int` 的子类而误通过整数校验。所有设置进入内部状态前
应正规化并深拷贝，不能继续引用来自 IPC 的原字典。

### 7.3 保存设置和 SEQ 导入

`load_settings()` 只把保存值或 SEQ companion 中的模块设置装入界面。它不得触发
Enable、连接或 Apply。加载不兼容的旧设置时，应尽量原样显示并标记问题；不得在没有
提示的情况下把安全关键值改成另一个值。最终由 Apply 明确拒绝无效组合。

如果现场拓扑在设备确定后基本不变，可以放在独立 TOML 中并要求重启，例如 7001
路由；操作员经常调整的电流、量程、等待、计数和通信口必须属于普通模块设置。

### 7.4 Settings 的可保存类型

Frontend `settings()`、默认设置和 SEQ companion 中的模块设置必须能同时通过 TOML 和
JSON 往返。允许的值只有：

- 非空、无换行的字符串键和嵌套 Mapping；
- bool；
- int；
- 有限 float；
- str；
- 由上述标量组成的 list/tuple。

不得包含 `None`、NaN、Infinity、bytes、日期对象、set、Qt 对象、driver 对象或自定义
类实例。设置缺字段时，backend 应按兼容策略补入明确默认值；未知或已废弃字段不得绕过
新的安全校验。整个请求仍受 1 MiB IPC frame 上限约束，不能把波形或大型校准表塞入
普通 Settings。

## 8. 通信、重试与资源释放

- 所有仪表 I/O 必须有正数、有限的 driver timeout。
- 普通 driver timeout 应显著小于框架 operation timeout，使 Stop/Error 后仍有清理
  时间。对手册定义的长采集完成查询，可以使用本次生命周期调用的剩余总预算，但必须
  固定预留清理时间，并在连接前用 count/delay/通道数证明总时长可容纳；不要求因此再
  暴露一个用户可配置的“单通道超时”。
- 每次关键写入后必须查询读回，确认实际状态，而不是只确认 `write()` 没抛异常。
- 身份查询必须验证准确型号，不能仅以“收到任意字符串”为连接成功。
- 资源发现失败可以 Warning 并允许手工输入地址；Apply 连接失败必须 Error。
- 资源下拉框应可编辑，保留手工输入的 VISA resource。
- `close()` 必须放在 `finally` 或等价清理路径中。即使 close 抛错，也要清空本地句柄，
  避免后续误用半关闭会话。
- Backend 应通过小型 transport protocol 和 factory 注入通信层，便于使用 fake
  transport 覆盖所有协议路径。

自动重试必须按命令语义决定：

- 可以重试：无副作用查询、确认没有执行写入后的重连和再次查询。
- 不得自动重放：结果不确定的写入、切换路由、ARM、Trigger、打开输出。
- 如果写操作可能已到达仪表但响应丢失，应先进入安全状态或查询实际状态，不能直接再写
  一遍。
- 对“路由是否闭合”“输出是否为零”无法确认时必须 Error。

## 9. Pause、Stop 和总超时

模块不能依靠核心强杀进程来保证仪表安全。必须主动提供协作取消点：

```python
context.checkpoint()
context.interruptible_sleep(settle_seconds)
```

- pause、dwell、settle、ARM 等实验等待必须使用 `interruptible_sleep()`，不得用长时间
  `time.sleep()`。
- 长循环在通道切换前后、触发前、每批读取之间调用 `checkpoint()`。
- 捕获 `ModuleOperationCancelled` 时只可做即时 best-effort 安全动作，然后必须继续
  抛出；不得转换成 Warning 或 Error。
- 驱动内部阻塞不能被 checkpoint 打断，因此 I/O timeout 必须有限。
- `begin_sequence` 中的准备同样必须可 Stop；例如 3 s ARM 等待不能成为不可取消黑箱。

`context.operation_timeout_seconds` 是 initialize、Apply、begin_sequence、Measure、
end_sequence 等每一次生命周期调用各自的总上限，不是整个 SEQ 的累计上限。Apply
必须在连接仪表前验证未来每一种调用分别能在上限内完成。某项只发生在
`begin_sequence` 时，不应错误地与 Measure 的时长相加后判定 Measure 超时。

每种调用的最坏时长估算应按实际归属至少考虑：

- 当前生命周期调用内实际测量的内部通道数；`aligned_slots` 每次只处理一个逻辑槽位，
  不得把本次 `T Measure` 的全部槽位时间错误累加成单次调用时长；
- 通道切换、pause、dwell、filter settle；
- 该调用内的 ARM 等待和触发次数；
- 采样 count 及单次采样上限；
- 允许的只读重试和重连；
- 两次系统快照及必要的安全收尾余量。

任一生命周期调用的估算超过 `context.operation_timeout_seconds` 时必须在连接仪表前
拒绝设置。可以保守预留调度与清理余量，但不能把互相独立调用的全部时长机械相加，也
不能通过增大 driver timeout 隐藏设计错误。

## 10. 数据行、系统快照与 rawdata

### 10.1 固定 Schema 下的行模型

模块必须在固定 manifest Schema 下选择并记录适合自己的行模型。常用模型包括但不限于：

1. **单行/汇总行**：适用于没有扫描槽位概念，或一次调用汇总固定内部通道的模块。
   可以直接从 `measure()` 返回一个 Mapping，也可以调用一次 `emit_row()`。2614B
   一次调用读取 A/B 并写宽表，属于这种 `once_per_slot` 模型。
2. **稀疏宽表**：适用于 R1-R4 等固定语义槽位和已有分析流程。每个通道一行，只填写
   当前槽位的 `R1/X1` 等测量列，其他槽位留空；当一行只表示一个通道时，应共用一个
   整数 `StatusCode` 列，不应为每个槽位重复声明状态列。372A 和 LR-700 使用此模型。
3. **按通道标准行**：适用于各通道结果字段完全相同的新模块。每行填写 `Channel` 和
   公共测量列。Keithley Delta 使用此模型。

除非必须兼容既有数据，新的同构扫描模块应优先使用按通道标准行，避免通道数扩大时
列数成倍增加。未属于当前逻辑槽位的清单列应省略或写 `None`；同一模块内必须保持一致。

每个 `emit_row` 的值只能是 JSON 可表示的 `str/int/float/bool/None`，浮点数必须有限。
不得发送未在清单声明的列。模块必须先完成该行的全部读取、换算和状态判断，再发送该行；
发送后不能回头修改已经写盘的数据。

每次 `measure()` 调用必须恰好产生一行：返回一个非空 Mapping，或者调用一次
`context.emit_row()` 并返回 `None`。两者同时使用、调用两次 `emit_row()` 或成功返回却
没有行，都会成为 Error。多通道扫描由核心通过多次 `measure()` 调用展开，而不是允许
一次调用输出多行。所有请求、事件、状态和最终返回值都受 1 MiB IPC frame 上限约束；
32,768 只是 raw 数值数量上限，不替代总字节限制。

一次 `T Measure` 最终写入的 DAT 行数等于本次逻辑槽位并集的大小；没有
`aligned_slots` 模块时为 1。核心在每个槽位等待全部参与模块完成，把结果合到该槽位的
同一行，因此仍严格保持“每个通道一行”。

### 10.2 多次采样

如果一个通道包含多次读数，README 和测试必须明确：

- 正式值使用单点、中位数或全部有效样本平均；
- 标准差使用样本标准差还是总体标准差；
- 异常样本是否剔除，以及最少有效样本数；
- 单个异常是 Warning 行、整个通道 Error，还是终止 SEQ；
- 电压、电流、电阻等换算公式和符号约定。

状态列表示对应正式结果的数据质量。一个 DAT 行只有一个测量结果组时必须声明
`StatusCode`；一次调用汇总多个独立内部通道时，可以为每组声明 `StatusCode1`、
`StatusCode2` 等编号列。每个实际测量的结果组都必须提供非负整数状态；Disabled 内部
通道的整组列（包括编号状态列）留空。`0` 在所有模块中固定表示正常；其他数值由模块
根据仪表能力自行定义，不存在框架统一的 `ERROR` 数值。模块必须在 README 和测试中
给出完整映射及多种故障同时出现时的优先级。状态码不得写 `NORMAL`、`ERROR`、
`OVER_RANGE` 等文本。框架 Warning/Error 表示“运行是否继续”，与 DAT 状态码不能互相替代。

某结果组的状态码非零表示该组没有可信的正式测量结果：对应电阻、电压、相位、标准差
等主结果列必须省略，使 DAT writer 写为空；稀疏表中其他未测通道也必须为空。同一宽表
行中的另一内部通道若正常，可以保留自己的结果。通道编号、温场快照、设定电流、样本数
和 rawdata 等诊断信息只有在含义仍然可信且 README 已说明时才可保留。若测量值本身仍然
有效、只是需要提醒操作员，应保持对应状态码为 0 并单独调用 `context.warning()`，不得
一边写非零状态码一边保留看似有效的正式结果。

### 10.3 系统快照

`context.system` 是本次生命周期调用开始时的快照。长时间测量需要新的温度/磁场时间点时，
必须调用 `context.sample_system()`。同一逻辑槽位的多个模块并行完成后，核心为最终合并
行采集一次系统状态，避免同一 DAT 行包含互相矛盾的核心时间点。

只有确实需要“两次温度/磁场平均”等派生结果时，模块才声明自己的
`TemperatureAverage`/`FieldAverage` 列。模块必须验证角色、单位、时间新鲜度并统一换算，
不能把同一份旧快照当作两次读取。

### 10.4 原始数值序列

需要保留每行原始仪表序列时使用：

```python
context.emit_row(
    formal_values,
    raw_values=raw_numeric_sequence,
)
```

强制限制如下：

- 最多 32,768 个数值；
- 只能包含有限 int/float，不能含 bool、文本、时间戳、通道名或对象；
- 一次 `emit_row` 的 rawdata 行与该模块在当前逻辑槽位的正式结果一一对应；多个模块
  合并到同一 DAT 行时，各自仍写入独立的模块 rawdata sidecar 行；
- 模块不得自行创建或追加 rawdata/DAT 文件；
- 文件名、路径摘要、重建和写盘顺序全部由核心管理；
- 完全不使用 rawdata 的模块不要发送空序列；已经承诺“每个正式行都有 rawdata 对应行”
  的模块在本行没有有效数值时应传空序列，保留空行占位和行号对应。

## 11. Warning、Error、状态和日志

事件代码应使用稳定模块前缀，例如：

```text
LS372_RESOURCE_DISCOVERY_FAILED
LR700_SAFE_STATE_FAILED
K6221_SWITCHER_UNAVAILABLE
```

- `code` 表示故障种类，`context` 表示稳定对象，如 `r1`、`input 3`、资源地址。
- 不得把时间、当前读数或不断变化的异常文本放入 `context`，否则无法去重。
- 相同问题恢复后必须调用与原 `code/context` 完全一致的 `resolve_warning()`。
- Warning 只用于“仍能安全继续且数据含义明确”的路径。
- **系统问题必须 Error 并中止 SEQ**：包括 worker/IPC 故障、通信重试耗尽、身份不符、
  协议状态无法建立、设置读回不一致、未知路由、触发状态不确定和无法确认安全状态。
- **测量数据问题只 Warning，不中止 SEQ**：包括单点非数字/NaN/Infinity、超量程、
  compliance、样本数不符或单通道统计失败。模块必须丢弃不能写入的值，输出明确的
  模块自有整数状态码，使对应结果组与所有未测通道保持为空，并在能够确认仪表和路由
  仍安全时继续下一通道。
- 如果数据异常已经扩展到无法判断报文边界、当前通道、路由、输出或仪表状态，它就不再
  是单纯数据问题，必须升级为系统 Error。
- Stop 产生的协作取消不是 Error。
- 仪表 overload/compliance 若仍能产生明确状态行，可以记录 StatusCode 并 Warning；具体
  继续策略必须写入 README 和测试。

当前逻辑槽位出现数据问题时，应调用 `context.warning()`，发送一行带非零状态码且正式
测量值为空的结果，再正常返回；核心随后调度下一个槽位。直接抛出 `ModuleWarning` 会
结束本次 backend 方法并令当前模块在该槽位留空，适合整个槽位无法形成状态行但仪表、
路由和安全状态仍明确的情况。

`update_status()`/生命周期返回值只能包含小型 JSON 数据，不得放 driver 对象、bytes、
大数组或秘密。状态页应明确区分 Desired、Applied、Connected、Armed、Safe
unconfirmed 等状态，不能只显示笼统的 “Initialized”。

模块不直接写运行日志。核心负责事件日志、设备状态日志、运行快照和 DAT；模块通过
稳定状态与事件接口提供信息。

## 12. Frontend 规范

Frontend 必须继承 `ModuleFrontend` 并实现需要的以下方法：

```python
create_settings_page(parent)
create_status_page(parent)
settings()
load_settings(settings)
update_status(status)
set_sequence_running(running)
```

界面要求：

- Settings 为默认页，Status 只显示实际状态和只读动作。
- 控件变化时只发一次 `settingsChanged`，不发仪表命令。
- `settings()` 必须完整返回当前期望设置；`load_settings()` 必须可 round-trip。
- Test Connection 等需要未保存界面值的动作，应在 payload 中显式传入当前设置。
- `set_sequence_running(True)` 必须禁用所有会请求 backend I/O 的按钮，包括资源刷新、
  Test Connection、手动安全动作和手动 Status Refresh。只改变本地显示、不访问 backend
  的纯界面重绘可以保留。
- Frontend 不得创建 VISA/串口连接、写 DAT 或启动测量线程。
- 所有 signal 只连接一次；所有 QObject/QWidget 都有明确 parent，窗口重建后旧对象可
  回收。
- 页面使用布局和可滚动区域，必须在 1080p、4K 和不同缩放比例下可操作；不得依赖固定
  大尺寸。
- 应提供合理 `sizeHint()`，但不能让首开窗口超过可用屏幕。
- 必须兼容核心的滚轮策略：未展开的下拉框和数字输入不因页面滚动而改变值；下拉列表
  已展开时允许滚轮翻页。
- SI 输入可以支持 `1m`、`100u`、`1p` 等形式，但解析器必须统一、拒绝歧义和非有限值，
  并避免失焦后显示无意义的过长小数。
- 前端禁用不兼容选项只是提示；Backend 仍必须执行相同的最终约束。

## 13. 安全状态规范

每个模块 README 和 backend 必须给出一条可验证的“模块安全状态”，至少回答：

1. 电源/激励/输出应是什么状态？
2. 切换器应闭合、保持还是全部打开？
3. Armed/Trigger 状态应如何退出？
4. 哪条查询用于确认？
5. 仪表没有真正 Off 时最低风险状态是什么？
6. 通信已经断开时软件还能确认什么，用户应到哪里人工检查？

统一安全顺序为：

```text
Enable：不改变危险输出
Apply：验证完整方案 → 先安全 → 写入应立即生效的设置并读回 → 再次安全
Begin：确认安全/已 Apply → 建立运行态
Measure：每个测量单元独立读取并形成一行；延迟扫描设置在使用前写入并读回
协作 Stop/系统 Error：立即 best-effort 安全 → worker 可用时由 end_sequence 严格确认
数据 Warning：写明确状态行；按已配置的行边界输出策略处理 → 继续下一个测量单元
Disable/退出：worker 可用时由 abort 严格确认 → 无条件释放本机资源
worker 超时/崩溃/IPC 断开：不得宣称已安全 → 标记 Safety Unconfirmed 并要求人工确认
```

主动输出模块可以提供 `output_off_between_measurements` 一类选项，但必须满足：默认值为
安全的逐行关闭；取消后只允许在 SEQ 内两个已经完整收束的槽位之间保持有意输出，并且
每行结束仍查询实际输出状态。数据 Warning 若路由、输出和协议状态仍明确，也按该选择
保持或关闭；SEQ Pause 不隐式改变输出，所以取消逐行关闭时 Pause 期间同样保持。Stop、
系统 Error、completed、Disable 和应用退出不受该选项影响，必须立即请求并确认模块安全
状态。Frontend 必须明确提示这些行为，Backend 必须再次验证布尔类型，README 和测试
必须覆盖默认与保持两条路径。

对于电流源加切换器，通常必须先确认输出为零/Off，再切换路由；路由确认后才能 ARM 或
Trigger。任何可能绕过模块已声明的全局电流、compliance、功率上限或仪表绝对合法
范围的每通道设置都必须由 Backend 拒绝。若模块明确不提供额外软件上限，不得暗中保留
一个只存在于旧设置或后端默认值中的限制。

软件测试不能替代硬件互锁、仪表前面板限值、人工急停和非关键负载上的首次验证。

## 14. 代码和注释

- 使用类型标注，公共生命周期和安全关键 helper 必须有中文 docstring。
- 中文注释重点解释“为什么”：手册依据、单位、状态位、安全顺序、为何不重试、异常时
  如何释放；不要逐行翻译代码。
- SCPI/GPIB 命令附近应注明对应仪表和命令语义；离散协议索引不得靠 UI 列表位置推导。
- 协议常量、单位换算、路由校验和状态位解析应拆成可独立测试的小函数。
- 不使用无界线程、守护线程、后台无限轮询或全局可变单例。
- 不吞掉普通异常。只有 best-effort cleanup 可以收集异常，但最终必须报告“安全未确认”。
- 不因三个模块存在相似代码就立即建立复杂公共驱动层。只有语义、错误模型和测试都一致
  的稳定逻辑才适合合并；仪表协议和安全动作通常应留在模块内。

## 15. 自动测试准入矩阵

每个正式模块至少覆盖以下项目：

### 15.1 清单与设置

- 使用核心 manifest loader 验证 ID、版本、API/core、入口、依赖和全部固定列；
- 默认设置是安全的新对象；
- Settings 保存、TOML 序列化、Frontend round-trip 和 SEQ 导入；
- Settings 对 `None`、非有限数、bytes 和自定义对象失败关闭，并受 IPC 大小限制；
- 缺字段、错误类型、边界、NaN/Infinity、重复物理通道和交叉约束；
- 声明了软件限值的主动输出模块必须测试单通道无法绕过；未声明软件限值的模块必须测试
  旧限值字段被兼容忽略且不会重新出现在 Frontend/Backend round-trip 中；

### 15.2 生命周期与协议

- `initialize` 不 Apply、不写仪表、不打开输出；允许的临时探测只能做无副作用查询；
- 允许的只读附件探测会关闭临时 transport；
- Apply 的身份验证、完整方案校验、安全基线、应立即生效设置的写入/读回和安全收尾；
- 延迟扫描设置在 Measure 使用前写入并读回；
- Apply 任一步失败时不保留半应用状态；
- 多通道模块覆盖一个、全部支持数量、Disabled 通道和固定顺序；
- `once_per_slot` 模块覆盖无扫描模块时的一次调用，以及与多槽位扫描模块并用时每行
  重新调用；内部多通道汇总模块覆盖一个和全部 Enabled 内部通道；
- `begin_sequence → measure × N → end_sequence`；
- completed、stopped、error 三种 end reason；
- Disable before Apply、重复 abort、close 异常和资源释放。

### 15.3 Pause、Stop、超时和异常

- 在 switch/pause/dwell/filter/ARM/trigger/read 等每个等待点 Stop；
- Pause 不消耗实验等待时间，恢复后继续；
- 主动输出模块若允许跨成功行保持输出，测试 SEQ Pause 期间的明确保持行为；无论该
  选项如何，Stop/Error/completed/Disable 都必须恢复并确认模块安全状态；
- 每个生命周期调用的最坏时长分别校验，超限在连接前拒绝；
- I/O timeout、worker timeout 和断开；
- 可重试查询成功、查询重试耗尽；
- 不确定写入/路由/Trigger 不重放；
- 安全动作失败必须保留 Error，而不是只关闭 worker。

### 15.4 数据和事件

- manifest 显式声明正确的 `measurement_mode`；缺失时发现结果有 Warning 且核心按
  `once_per_slot` 兜底；
- `aligned_slots` 返回值、槽位并集、每槽位一行、Disabled 槽位空列，以及
  `once_per_slot` 在每个槽位重复测量；
- 单次调用无行、多行或“emit 后又 return”均被拒绝；
- 单位换算、符号、平均和标准差定义；
- Warning code/context 稳定、恢复时 resolve；
- 系统 Error 终止；坏点、超量程、样本数等数据 Warning 写模块自有整数状态码、保持
  当前测量结果为空后继续下一测量单元；
- 未声明列、复杂类型、NaN/Infinity 和超大 IPC 被拒绝；
- 使用 rawdata 时验证一行一对应、必要的空占位、32,768 上限和异常样本策略；
- 采样前后系统快照确实是不同的新鲜时间点。

### 15.5 Frontend 与安装

- 资源下拉、手工地址、Refresh/Test payload；
- 运行时所有 backend I/O 按钮禁用，包括资源和 Status Refresh；
- 不兼容选项的禁用和 Backend 再验证；
- 1080p/4K、缩放、滚动、窗口重建、重复 Enable/Disable 和 signal 数量；
- 公共依赖无需安装，额外依赖可完全离线安装；
- 目录被修改后需要重新信任。

单模块测试通过后，必须再运行：

1. 模块仓库全部测试；
2. 核心完整测试；
3. Python compile check；
4. 依赖一致性检查；
5. Windows 源码版和打包版的手动冒烟测试。

## 16. 真实仪表验证顺序

未连接真实仪表的模块必须保持 beta，并在 README 明确说明。首轮硬件验证按风险逐级进行：

1. 不接敏感样品，只枚举资源和读取 `*IDN?`；
2. 验证面板通信模式、线缆、终止符和触发链；
3. 用非关键负载验证安全状态及其查询回读；
4. 对主动输出模块，使用最小激励验证一个测量单元的 Apply/Measure；
5. 对多通道/路由模块，验证切换前后的输出和物理路由；
6. 分别在 settle、ARM、trigger、read 中执行 Pause/Stop；
7. 断开 GPIB、关闭附件或制造超量程，核对 Warning/Error；
8. 验证 Disable、应用退出和进程强制回收后的仪表实际状态；
9. 多通道模块运行全部支持的通道数量；当前四个硬件模块为四通道；
10. 长时间运行，并对照仪表本机记录、DAT、rawdata 和状态日志。

记录仪表型号、固件、卡槽、接线、VISA implementation 和验证日期。只有完成计划内的
真实仪表验证并关闭安全问题后，才可从 beta 提升为稳定模块。

## 17. 版本、兼容与发布

- 模块任何准备分发的源码、清单、隐藏配置或依赖变化都必须提升 manifest `version`。
- 只修复实现且不改变设置/DAT 接口时提升 patch 或对应 prerelease 序号。
- 新增向后兼容设置或列通常提升 minor；删除/重命名 DAT 列、改变单位或改变同名列语义是
  breaking change，必须提升 major，或发布新的 module ID 并提供迁移说明。
- Backend 的设置规范化必须兼容仍受支持的旧设置：缺少的新字段补入安全默认值；旧字段
  的迁移必须显式且有测试。第一次需要不兼容设置结构时，应加入模块自己的
  `settings_schema_version` 和逐版本迁移，不能靠静默猜测。
- 提高 `core_requires` 时必须指出使用了哪个新 API，并测试最低和当前核心版本。
- 公共依赖快照必须与核心 source of truth 对照；额外依赖必须完成离线 wheel 测试。
- README 必须记录模块版本、支持的核心范围、DAT Schema、设置迁移、已测试仪表/固件和
  尚未完成的真实硬件项目。
- 未完成真实仪表验证时保持 beta；提升 stable 前逐项保存第 16 节的验证证据。
- 发布前必须生成一份合规结果：通过项、N/A 项、带理由和测试的偏离项。不得只把现有
  模块称为“参考实现”而不检查其当前版本。
- 分发包不得包含本地地址、凭据、DAT、缓存、`module_data` 或 `plugin_runtime`。

## 18. 新模块开工清单

开始编码前先写清以下设计决定：

- [ ] 完整仪表拓扑、每个通信口和唯一所有者；
- [ ] 仪表面板及接线前置条件；
- [ ] 模块可验证的安全状态与确认查询；
- [ ] Enable 是否需要只读探测可选附件，以及缺失后的固定降级方式；
- [ ] 显式选择 `once_per_slot` 或 `aligned_slots`；若为后者，定义逻辑槽位编号、
  Enabled 槽位和 `measurement_slots()`；
- [ ] 是否有内部通道；若有，通道数量、独立配置、宽表汇总或与逻辑槽位对齐方式；
- [ ] 单行/汇总、稀疏宽表或按通道标准行；
- [ ] 是否需要 rawdata，以及正式值、平均、标准差和异常样本规则；
- [ ] 哪些属于数据 Warning，哪些属于系统 Error；
- [ ] 每条命令是否可安全重试；
- [ ] Pause/Stop 检查点和每个生命周期调用的最坏操作时长；
- [ ] Warning、Error 和单组 `StatusCode` 或宽表 `StatusCodeN` 的稳定代码及 README 映射；
- [ ] 主动输出是否提供额外软件安全上限（若否，记录原因）、仪表绝对合法范围和人工
  应急步骤；
- [ ] Settings 类型、旧版本迁移和 DAT Schema 兼容策略；
- [ ] 单元测试 fake transport 能否观察每条写入、查询和 close；
- [ ] README 中的真实硬件验证状态。

完成这些决定后，再建立 manifest、constants、backend、frontend 和 tests。协议实现应先
通过 fake transport 的安全与异常测试，再连接真实仪表。
