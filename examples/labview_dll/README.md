# LabVIEW DLL Measurement Module 模板

要求 OpenLab Control `0.20.1` 或更新版本。

仪表已有经过实际使用的 LabVIEW 驱动或测量 VI 时，优先使用本模板复用原实现，而不是重新
用 Python 翻译整套底层指令。复用不会替代安全边界：量程与输出限制、有限超时、写后回读
和真机低风险验证仍然必须完成。

这个目录是开发模板，不会出现在 Modules Manager。使用时复制整个目录到
`modules/<新 ID>/`，修改 `module.toml`，再把编译好的 64 位 `driver.dll` 和它自己的依赖
文件放在 `backend.py` 旁边。

```text
modules/<id>/
├─ module.toml
├─ backend.py
├─ labview_dll.py
├─ driver.dll
└─ driver 的其他依赖文件
```

DLL 在模块工作进程启动时加载一次；同一模块的调用保持串行，不会并发访问同一 LabVIEW
状态。不同 Enabled 模块仍由框架并行运行。每个框架操作只跨 DLL 边界一次，避免把一条
完整测量拆成大量高延迟的小调用。

`OLC_Describe` 必须只返回常量说明，不能连接仪表、启动后台循环或改变输出，否则模块会
卡在 Initializing。`OLC_Open` 可把 VISA session 等句柄保存在 LabVIEW functional global
中，后续 `OLC_Invoke` 复用同一份状态，`OLC_Close` 最后清除它。

模板未提供 Settings 界面。DLL 使用固定默认设置时可以直接工作；需要选择资源或输入参数
时，按模块开发教程增加 `frontend.py`，其设置会在用户按 Apply Settings 后原样传给 DLL。

## 固定导出接口

DLL 必须以 C (`cdecl`) 调用约定导出 `OLC_Describe`、`OLC_Open`、`OLC_Invoke` 和
`OLC_Close`。四者必须与 `openlab_labview_abi.h` 使用完全相同的签名：

```c
int32_t Function(
    const uint8_t *request_json,
    int32_t request_length,
    uint8_t *response_json,
    int32_t response_capacity,
    int32_t *response_length);
```

输入和输出都是 UTF-8 JSON 的 U8 数组，不是 LabVIEW String Handle。Python 提供 1 MiB
输出缓冲区；DLL 不得越界，必须填写实际 `response_length`，也不需要结尾 `\0`。

返回值 `0` 表示 ABI 调用完成，并且输出必须是下面的 JSON 信封。非零返回值只用于坏
指针、容量不足等无法生成 JSON 的 ABI 故障；仪表超时和测量异常必须使用信封中的
`warning` 或 `error`。

```json
{
  "severity": "ok",
  "code": "",
  "message": "",
  "context": "",
  "result": {}
}
```

`severity` 只能是 `ok`、`warning` 或 `error`。Warning 终止本次模块调用但允许 SEQ 继续，
Error 中止 SEQ。把 LabVIEW error cluster 映射到稳定的 `code`、可读的 `message` 和具体
通道/操作 `context`，不要把错误文字写入 DAT 数值列。

## LabVIEW 端编译

1. 为四个导出函数各建一个很薄的顶层 VI；真实测量 VI 保持在它们下面。
2. 顶层 VI 使用 U8 数组处理 JSON，并用 I32 表示长度、容量、返回值。
3. 在 Application Builder 中建立 64 位 Shared Library，导出名称严格使用上面的四个名称，
   调用约定选择 C (`cdecl`)。
4. 对数组参数选择数据指针形式。构建后逐项比较生成头文件和
   `openlab_labview_abi.h`；类型、顺序、位宽、指针层级或调用约定不同就不能加载。
5. OpenLab Control、DLL、依赖 DLL 和 LabVIEW Run-Time Engine 必须具有相同位数；运行时
   版本必须能加载构建该 DLL 的 LabVIEW 版本。

## JSON 操作

`OLC_Describe` 收到 `{}`，必须返回静态 DAT 元数据：

```json
{
  "severity": "ok",
  "code": "",
  "message": "",
  "context": "",
  "result": {
    "abi_version": 1,
    "kind": "measurement_module",
    "columns": {"Resistance": "Ohm", "StatusCode": ""},
    "display_columns": ["Resistance"],
    "slots": 1,
    "operations": ["configure", "measure", "event", "sequence_command", "manual_function"],
    "manual_functions": [
      {
        "id": "diagnostic_read",
        "label": "Diagnostic Read",
        "description": "Run one existing LabVIEW diagnostic VI.",
        "inputs": [
          {"name": "mode", "label": "Mode", "type": "choice", "default": "Current", "choices": ["Current", "Voltage"]},
          {"name": "range", "label": "Range", "type": "choice", "default": "Auto", "choices": ["Auto", "Low", "High"]},
          {"name": "level", "label": "Level", "type": "float", "default": 0.001, "minimum": 0.0, "maximum": 1.0, "unit": "A", "decimals": 9},
          {"name": "samples", "label": "Samples", "type": "int", "default": 10, "minimum": 1, "maximum": 1000}
        ],
        "outputs": [
          {"name": "value", "label": "Value", "type": "float", "unit": "V", "decimals": 9},
          {"name": "status", "label": "Status", "type": "int"}
        ]
      }
    ]
  }
}
```

`columns`、`display_columns` 和正整数 `slots` 在模块 Enable 时读取，运行中不能改变。
`manual_functions` 是可选项，直接保存在 DLL 静态说明中，不需要另写 TOML。模块 Enable
成功后，OpenLab 在主菜单 `Modules` 下自动生成窗口；同一函数可以同时打开多个窗口。
函数只在 SEQ 空闲时运行，结果显示并写入事件日志，不写 DAT。

`OLC_Open` 收到核心确认过的 Measurement 资源表和本次操作总超时。它适合初始化 DLL
内部状态；如果用户还没有 Apply Settings，不要猜测应连接哪台仪表：

```json
{
  "resources": {
    "meter_1": {
      "id": "meter_1",
      "address": "GPIB0::24::INSTR",
      "identity": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,...",
      "purpose": "measurement"
    }
  },
  "operation_timeout_seconds": 120.0
}
```

`OLC_Invoke` 使用统一请求：

```json
{"operation": "measure", "parameters": {"slot": 1, "operation_timeout_seconds": 120.0}}
```

支持的模板操作及其 `result`：

- `configure`：参数中有 `settings`、`resources` 和总超时；结果是要显示的状态字典。
- `measure`：结果必须有 `row` 对象；可选 `status` 对象和只含数值的 `rawdata` 数组。
- `event`：参数中有 `name`、`data` 和总超时；结果是状态字典。`run_end` 的安全关闭输出
  也在这里完成。
- `sequence_command`：普通指令和扫描的每个点都使用这个操作；结果是状态字典，不写 DAT。
- `manual_function`：参数包含 `function_id`、对应的 `parameters` 和总超时；结果字段必须与
  `manual_functions.outputs` 完全一致。
- `OLC_Open`、`OLC_Close`：结果是状态字典，可以为空。

模板后端附带两个可直接出现在 Sequence Command Bar 中的示例：

- `Set Value`：普通指令，DLL 收到一次 `set_value` 和 `{"value": ...}`。
- `Scan Value`：扫描指令。核心依次取 `points`，为每个点增加当前 `value` 后调用 DLL；
  只有该点调用成功才运行扫描块内的 Measure 或其他子命令。

例如 `Scan Value` 的第二个点会进入 DLL：

```json
{
  "operation": "sequence_command",
  "parameters": {
    "command_id": "scan_value",
    "parameters": {
      "points": [0.0, 1.0],
      "settle_seconds": 0.0,
      "value": 1.0
    },
    "operation_timeout_seconds": 120.0
  }
}
```

开发真实模块时，直接修改 `backend.py` 顶部的 `sequence_commands`：替换 ID、显示名称、
参数、单位和上下限。扫描的 `points_field` 必须指向 list 参数，`point_parameter` 是核心为
当前点增加的参数名，不能和已有字段同名。参数窗口限制只帮助输入；DLL 在改变真实输出前
仍必须执行仪表安全检查和写后回读。

一次正常测量结果例如：

```json
{
  "severity": "ok",
  "code": "",
  "message": "",
  "context": "",
  "result": {
    "row": {"Resistance": 12.5, "StatusCode": 0},
    "rawdata": [0.00124, 0.00126],
    "status": {"Last Resistance": "12.5 Ohm"}
  }
}
```

未测量或异常通道不要写字符串，省略对应数值列；状态码由模块自行定义为数值。

框架只能在进入和离开一次 DLL 调用时检查 Pause/Stop，不能中断已经进入 DLL 的阻塞 VI。
因此 DLL 内部每次仪表 I/O 必须设置有限超时，长等待应留在 Python/ModuleAPI，或把操作
拆到能返回 Python 的安全检查点。核心总超时到达后可能终止进程，不能保证卡死的 DLL 会
执行 `OLC_Close`；真实输出仍需仪表本机保护和硬件联锁。
