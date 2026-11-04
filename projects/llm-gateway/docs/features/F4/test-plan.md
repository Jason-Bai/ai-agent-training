# F4：Structured Output 与 Responses 适配——测试方案

> 状态：验收通过。用户确认的完整十进制保真已实现；最终功能回归与原生 Claude Code 第1轮独立审查均通过，实际证据与审查处置分别记录。
>
> 对应 [已分段确认的设计](design.md) 和 [调用方完整规则](../../response-schema.md)。设计确认、测试确认、技术探测与实际验收分别记录；本文件不是通过报告。

## 1. 范围与通过标准

### 1.1 基线与证明目标

基线为已验收 F3/F3-O1；原证据入口见 [F3](../F3/test-plan.md)、[F3-O1](../F3/optimizations/O1/test-plan.md)。历史 606 离线/4 真实通过不替代 F4 回归，也不能作为迁移后 Responses 已可用的证明。

本项证明统一 Schema 请求、受控模板、能力配置、两条出站协议、完整预算、严格 JSON/Schema 验收和失败收口。保护既有文本、探测、配置/凭据隔离、无隐式重试和客户端生命周期；不验证业务事实、授权、模型语义安全、恢复、流式、取消、持久化或多实例。

| 层级 | 实际测试对象与替换边界 | 不能证明 |
| --- | --- | --- |
| 逻辑单元 | 真实 Schema 预检/校验器、规范化、模板、模型能力、专用计数策略 | HTTP 与外部服务可用 |
| MockProvider / ASGI | 真实 Gateway、校验组件、Trace、HTTP 错误边界；只替换 Provider 输出 | 真实 SDK 参数转换或模型遵守 Schema |
| 协议替身 | 真实 DeepSeekProvider/httpx2 和 OpenAICompatibleProvider/AsyncOpenAI，经回环服务接收原始请求、注入上游响应 | 云端能力和真实 Key 有效 |
| 实际进程 | 单 worker 临时服务与真实 HTTP；注入隔离配置，默认 Mock | 真实供应商可用或多实例安全 |
| 显式真实 | 临时本地 FastAPI → 两个真实适配器 → 同一 DeepSeek 服务 | 双供应商容灾、全部 Schema 组合或生产可靠性 |

通过条件：全部必测用例/参数化变体和相关回归通过；前置拒绝零 Provider/零 Attempt；已调用失败一次 failed Attempt；成功须完成所有验收；默认测试不读真实凭据、不访问外网；六次真实接入有实际回执；实现导读和相关文档同步；Claude Code 完整独立审查无 P0–P2。未满足任何必验层次不得称整项验收通过。

### 1.2 公共断言与固定输入

所有 /v1/llm 成功/失败检查四字段信封，X-Request-ID 存在且只在响应头；成功数据字段按模式精确匹配，错误 data=null。按请求 ID 关联请求、Attempt 与 Mock 记录，不按并发列表下标猜测对应关系。404/405 等非模型入口沿用原契约，不凭空要求调用 Trace。

表中 零调用表示 Provider 调用数=0、Attempt 数=0、请求 failed；一次失败 表示调用数=1、Attempt failed、请求 failed；一次成功 表示调用数=1、Attempt completed、HTTP 正常完成后请求 completed。这些结果标签描述副作用。启动拒绝用 C0 表示客户端构造数=0、无模型调用、无请求 Trace。

基础请求使用 general-primary、context_answer@1.2.0、context=`订单 A123 的商品为充电器，数量为 1，类别为配件。资料未提供备注。`；User=`提取订单号、商品信息和备注；备注未知时使用 null。`，输出 128/超时 30。基础 Schema 与固定 Mock 回答如下：

```json
{
  "type":"object",
  "$defs":{
    "Product":{
      "type":"object",
      "properties":{
        "name":{"type":"string","description":"商品名称"},
        "quantity":{"type":"integer","minimum":1},
        "kind":{"type":"string","enum":["配件","其他"]}
      },
      "required":["name","quantity","kind"],
      "additionalProperties":false
    }
  },
  "properties":{
    "order_id":{"type":"string"},
    "product":{"$ref":"#/$defs/Product"},
    "note":{"anyOf":[{"type":"string"},{"type":"null"}]}
  },
  "required":["order_id","product","note"],
  "additionalProperties":false
}
```

```json
{"order_id":"A123","product":{"name":"充电器","quantity":1,"kind":"配件"},"note":null}
```

纯逻辑用例可缩减此 Schema 以隔离单个约束；真实/关键协议用例使用完整基础 Schema。Mock 通过显式脚本提供候选文本，不实现任意 Schema 的数据生成器。凡测试解析/校验，使用真实 validation 组件，不能将其替换为固定成功。

## 2. 用例

每行按“构造指定输入或上游响应 → 调用真实被测组件/入口 → 断言响应、参数、副作用和 Trace”执行。表中分号分隔的输入分别参数化；不能只测一个代表值便声称覆盖整行。边界夹具用生成函数精确构造，包含临界值和临界值+1。

### 2.1 请求、Schema 与资源

| 编号 | 对应规则 / 准备与输入 | 预期 | 参数/副作用与 Trace | 层级 |
| --- | --- | --- | --- | --- |
| F4-S01 | 省略 Schema，旧模板；以及新模板文本模式 | 普通成功，data 精确 text/usage | 一次成功；无 Schema 指纹、无 JSON API 约束 | 单元+ASGI |
| F4-S02 | Schema 显式 null、字符串、数字、数组、true/false | 422 invalid_request | 零调用；不渲染或发模型请求 | ASGI |
| F4-S03 | 根 {}；根数组/标量 type；根 $ref；根 anyOf | 400 invalid_response_schema | 零调用 | 单元+ASGI |
| F4-S04 | 基础 Schema，嵌套对象与数组，各基本类型 | 合法 Schema | 一次成功；输出 parsed 与输入 JSON 值一致 | 单元+ASGI |
| F4-S05 | 每个支持关键字的合法及错误取值类型；type 数组；布尔 Schema | 非法变体 invalid_response_schema | 零调用；不将 bool 当数值限制 | 单元+ASGI |
| F4-S06 | oneOf/allOf/not、if/then/else、pattern/format、prefixItems、未知关键字 | invalid_response_schema | 零调用；不静默移除 | 单元+ASGI |
| F4-S07 | property 名为 format/unknown；description/default/enum 内含似关键字的数据 | 不因数据名误拒绝 | 校验位置与数据位置分离 | 单元 |
| F4-S08 | $schema 省略、正确 URI、旧草案/错误类型/其他 URI；$id/$anchor | 仅省略/正确 URI 通过 | 错误 零调用 | 单元+ASGI |
| F4-S09 | 本地 $defs 引用、引用重用、~0/~1 转义、指向其他有效 Schema 位置 | 正确解析，不展开传输 | 一次成功；发送仍有 $ref | 单元+协议 |
| F4-S10 | 远程/文件/锚点引用、缺失目标、指向数据或布尔值 | invalid_response_schema | 零调用；网络/文件引用解析计数均 0 | 单元+ASGI |
| F4-S11 | 自引用、多节点间接循环、无循环重复引用 | 循环拒绝，重用允许 | 拒绝 零调用；不能用全局 visited 错拒绝重用 | 单元 |
| F4-S12 | $ref 同级 title/description/default；同级 type/minimum 等附加约束 | 注解允许，附加约束拒绝 | 错误 零调用 | 单元 |
| F4-S13 | anyOf 恰好一非空+一 null；分支反序；非空引用 | 通过，null 与非空值按各自规则验收 | 无 union 类型强制转换 | 单元+ASGI |
| F4-S14 | anyOf 三分支、没有 null、两个 null、一般联合、非法分支 | invalid_response_schema | 零调用 | 单元 |
| F4-S15 | 未使用定义含未知关键字、非法引用、循环或结构超深 | invalid_response_schema | 零调用；不能只遍历已引用定义 | 单元+ASGI |
| F4-S16 | 用 description 长度构造规范化 UTF-8 16,384/16,385 字节；含中文 | 前者通过，后者 invalid_response_schema | 错误 零调用；按字节不是字符或入站排版 | 单元+ASGI |
| F4-S17 | 有效节点的 default 容器构造原始深度 32/33 | 前者通过，后者拒绝 | 错误 零调用；包含注解容器但不误计 Schema 节点 | 单元 |
| F4-S18 | 嵌套对象、数组、可空和跨引用组合构造结构深度 8/9 | 前者通过，后者拒绝 | 错误 零调用；标量及可空包装不加层 | 单元 |
| F4-S19 | 独立定义和属性构造原始节点 256/257 | 前者通过，后者拒绝 | 错误 零调用；properties 字典自身不额外算节点 | 单元 |
| F4-S20 | 小定义反复引用构造展开节点 256/257；宽而浅的展开增长 | 前者通过，后者拒绝；有限遍历 | 错误 零调用；不分配巨型展开文档 | 单元 |
| F4-S21 | 完整规则中的 Address 节点例子 | 原始 5 / 展开 7 | 不双计引用包装；无全局去重漏算 | 单元 |
| F4-S22 | 标准 Pydantic 模型导出：嵌套、可空、默认注解；递归/format/复杂联合 | 支持子集通过，其余拒绝 | 库来源不豁免预检 | 单元 |

### 2.2 模板、配置、规范化与预算

| 编号 | 对应规则 / 准备与输入 | 预期 | 参数/副作用与 Trace | 层级 |
| --- | --- | --- | --- | --- |
| F4-P01 | 旧三份模板携带合法 Schema | 400 unsupported_capability | 零调用；旧资产内容/指纹不改 | 单元+ASGI |
| F4-P02 | 新模板三种输出路径 | System 完整正文相同，只有两条消息、无 Few-shot | Chat User 含完整 Schema；native User 仅 json；text 为 text | 单元+协议 |
| F4-P03 | 覆盖 output_contract/message；缺 context、额外变量、空白/超长 context | 原变量错误分类 | 零调用；不允许嵌入的假标签改变实际受控值 | 单元+ASGI |
| F4-P04 | context 10,000/10,001；最终 User 含协议总长 20,000/20,001 | 上限允许，超限原有错误 | 错误 零调用；Schema 独立满足 16 KiB 也不能豁免 | 单元+ASGI |
| F4-P05 | 原消息/资料/Schema description 含修改 System、切换接入、授予权限文字 | 仍作为数据传递，System/实际模式不变 | Mock/协议参数断言，不声称证明真实模型抗注入 | 单元+协议 |
| F4-C01 | 四种受控 adapter/protocol 组合及各允许 mode | 配置成功 | 不额外构造客户端或调用模型 | 单元 |
| F4-C02 | 缺 api_protocol/mode；未知值；adapter/protocol/mode 不兼容 | 启动 ConfigurationError | C0；诊断不含配置值/凭据 | 单元 |
| F4-C03 | mode=none 请求 Schema；合法模式但普通请求 | 前者 unsupported_capability；后者文本成功 | 前者 零调用，后者 一次成功 | ASGI |
| F4-C04 | 计数策略不支持本模型/模式约束；未知策略 | 启动拒绝 | C0；不偷偷按纯消息计数 | 单元 |
| F4-N01 | Schema 对象键递归重排、输入缩进不同 | 规范化字节/Schema 指纹/实际约束一致 | 重复相同请求指纹稳定 | 单元+ASGI |
| F4-N02 | 改 enum/required 数组顺序、description 内空白、default 或业务约束 | 保留变化，指纹变化 | 不擅自按语义排序数组或移除注解 | 单元 |
| F4-N03 | Schema 含非有限数；default 不是应用数据的转换要求 | 非有限拒绝；不填默认、不改原结构 | 不将 int/string/bool 相互转换 | 单元 |
| F4-N04 | native 下相同 messages、不同 Schema；Chat 下不同 Schema | native 消息指纹可同，Schema 指纹不同；Chat 两者均变 | 不把消息指纹冒充整个请求指纹 | 单元+ASGI |
| F4-N05 | 原始 HTTP 中超精度 minimum/const、小数/指数、5000位整数；精确匹配与不匹配输出 | 匹配成功且 parsed 为JSON数字；不匹配502 Schema错；不因浮点舍入成功 | 同一Decimal传到Schema/指纹/User/计数/真实SDK字节；无全局精度设置变更 | 单元+ASGI+协议 |
| F4-B01 | 完整新模板与 Schema；中文、描述、enum、引用、Few-shot 文本回归 | Chat Schema 一次；native 额外约束完整计入 | 对独立参考编码黄金比对，禁止只用同实现自证 | 单元 |
| F4-B02 | 窗口=输入+输出+余量；窗口少 1；输出/超时恰好上限和超限 | 边界允许；超限原有错误 | 错误 零调用；Provider 参数不被降低 | 单元+ASGI |
| F4-B03 | native 不带 Schema 的文本/探测；Chat 带 Schema | 原文本编码不变；没有 Schema 信封或重复计数 | counter_version 对应实际规则；真实 Usage 不来自预检 | 单元 |
| F4-B04 | 计数器抛错；Schema 已合法但未知模型；多个阶段同时错误 | 依固定顺序错误收口，不产生部分消息指纹 | 零调用；已完成步骤元信息保留、未知保持 None | ASGI |
| F4-B05 | 非法 Schema+未知模板；有效 Schema+未知模板/模型；变量错+模型错 | Schema → 模板变量/能力 → 模型 → 装配/预算顺序 | 零调用；与无 Schema 的 F3 顺序分别断言 | ASGI |

### 2.3 协议与返回验收

| 编号 | 对应规则 / 准备与输入 | 预期 | 参数/副作用与 Trace | 层级 |
| --- | --- | --- | --- | --- |
| F4-W01 | 真实 DeepSeekProvider 指向回环协议服务，文本与 Schema 两种请求 | 仅 Schema 有 response_format=json_object，路径 chat/completions | System 不追加，参数/次数准确为 1 | 协议 |
| F4-W02 | 真实 SDK Responses 指向回环服务，文本/Schema 请求 | input 完整；原生 name=gateway_response；同一规范化 Schema；无 Chat 路径/字段 | 非思考、store/stream=false、预算与超时准确；不重复 instructions | 协议 |
| F4-W03 | 401/402/429/5xx、超时、连接失败、重定向、无效外层 HTTP JSON | 延续 F3 分类；外层坏 JSON 为 provider_invalid_response | 一次失败；收到请求不超过一次；无 SDK/传输重试 | 协议+ASGI |
| F4-W04 | Responses completed +合法 assistant output_text；多段文本 | 顺序原样拼接、无额外分隔；取得严格 Usage | 一次成功；保留原始状态，不把 reasoning 当正文 | 协议+ASGI |
| F4-W05 | completed 但空/空白正文、错误 role/内容类型、缺关键字段、工具调用或矛盾状态 | provider_invalid_response | 一次失败；不能仅依据 output_text/HTTP 200 成功 | 协议+ASGI |
| F4-W06 | incomplete/max_output_tokens 与 Chat length；正文为坏 JSON或碰巧合法 JSON | output_truncated 优先，不进入 JSON/Schema 验收 | 一次失败；保留合法 Usage/模型身份 | 协议+ASGI |
| F4-W07 | 明确 content_filter、refusal（包括伴随普通文本） | 分别 provider_content_filtered / provider_refused | 一次失败；不变成 invalid_json 或成功 | 协议+ASGI |
| F4-W08 | Responses failed +已知/未知合法 error；未知/in_progress 状态、缺原因或矛盾字段 | 合法失败按受控映射/通用 provider_error；非法状态 provider_invalid_response | 一次失败；不泄露 error.message | 协议+ASGI |
| F4-W09 | Usage 正确、缺失/null、布尔/字符串/负数/错误总和 | 正确保留、未知为 null、错误拒绝 | 与后续解析失败分开；不保留未验证 Usage | 单元+协议 |
| F4-V01 | 基础 Schema+固定合法回答，含外围空白 | 200 text/parsed/usage；text 原样保留 | 一次成功；不通过重新序列化替换 text | 单元+ASGI |
| F4-V02 | 缺括号、尾逗号、代码围栏、前后说明、连续两个 JSON 值 | 502 invalid_json | 一次失败；不提取、不修复、不重试 | 单元+ASGI |
| F4-V03 | 重复键（含嵌套）、NaN、Infinity/-Infinity、非有限解析数值 | 502 invalid_json | 一次失败；不接受 Python JSON 宽松扩展 | 单元+ASGI |
| F4-V04 | 合法 JSON 根为 []、字符串、整数、true、null | 502 schema_validation_failed | 一次失败；不混为语法错误 | 单元+ASGI |
| F4-V05 | 少必填字段；多字段且 additionalProperties=false；嵌套字段错类型 | schema_validation_failed | 一次失败；不删多余字段、不类型转换 | 单元+ASGI |
| F4-V06 | additionalProperties 省略/true；未知字段；required 与 nullable/default 四种组合 | 严格遵守调用方语义 | 合法 一次成功、不合法 一次失败；不自动收紧/补值 | 单元+ASGI |
| F4-V07 | 字符串长度、数组数量、数值开闭边界、enum/const 的满足/不满足值 | 每个约束两侧分别成功/Schema 错误 | bool 不是 integer；字符串数字不转换 | 单元 |
| F4-V08 | schema 合法但与实际事实不符的候选，例如另一订单号但仅声明 string | 协议可以通过，业务仍需 Agent 校验 | 不暗中添加 A123 专属规则；不执行动作 | 单元 |
| F4-V09 | 验收器/编排出现未预期程序异常 | 500 internal_error | 一次失败；不泄露正文/异常，不误写完成 | ASGI |

### 2.4 Trace、隔离、回归与文档

| 编号 | 对应规则 / 场景 | 预期 | 参数/副作用与 Trace | 层级 |
| --- | --- | --- | --- | --- |
| F4-T01 | 上游 completed 但非法 JSON / Schema；各前置失败；正常完成 | 上游状态与平台验收分开记录 | 分别 一次失败/零调用/一次成功；请求与 Attempt 各收口一次 | ASGI |
| F4-T02 | 输出失败前已取得合法 model/Usage；前置尚未生成消息/计数 | 已确认元数据保留，未知不补造 | schema_fingerprint/模式/计数版本按实际步骤记录 | ASGI |
| F4-T03 | 输入、Schema description、回答、上游 error 含敏感哨兵 | HTTP 错误/Trace/证据无哨兵、Key、正文、堆栈 | 仅公开成功 text/parsed 属于正常返回；不误将其当内部留存 | ASGI+协议 |
| F4-T04 | 并发的两个不同 Schema/模式请求，受控交错返回 | Schema、参数、指纹和结果按 request_id 隔离 | 每个独立 一次成功/一次失败；不变异共享 messages/Schema | ASGI |
| F4-R01 | 现有 F1/F1-O1/F2/F3/F3-O1 非 live 场景 | 相关回归全部通过 | 只迁移必要配置/协议夹具，不删除原断言来变绿 | 全离线 |
| F4-R02 | 旧三份 Prompt、2/2/6 条消息与内容黄金 | 原内容、指纹、长度/变量规则不变 | 旧 Schema 请求只是新增能力拒绝分支 | 单元+协议 |
| F4-R03 | 两路径普通文本、provider health、healthz | 文本 data 不加 parsed；探测固定16/10且只返回 latency_ms；healthz零调用 | Probe Trace 用途/Usage不变 | ASGI+协议 |
| F4-R04 | 默认两 Mock、注入客户端、自有客户端、部分构造/关闭失败 | 继续正确构造/复用/关闭且不误关借用资源 | 无额外网络或隐式重试 | 单元 |
| F4-R05 | 默认全部测试；真实凭据存在但未 --run-live | live 默认跳过，离线不读真实 Key、不出网 | 测试不改用户 .env/gateway.yaml/credentials.yaml | 全离线 |
| F4-R06 | Uvicorn 单 worker、临时配置/端口实际 HTTP | 文本成功、结构化成功、Schema拒绝与输出失败契约一致 | 按ID检查Trace；服务停止/端口/子进程清理 | 实际进程 |
| F4-D01 | README/接口/规则/设计/测试/进度/AGENTS 的链接与状态 | 无断链、冲突参数、把设计写成已实现 | 当前不新增 capability 路由；示例可 JSON 解析 | 文档检查 |

## 3. 执行计划、证据与结论

### 3.1 隔离环境与 TDD 顺序

在项目目录 `projects/llm-gateway`，使用 uv 与项目锁文件。现有 Python 下运行全量离线，关键新验证器、计数和适配器场景另验证 Python 3.12 最低版本兼容。已添加并锁定 jsonschema 4.26.0，不增加十进制第三方依赖。

顺序：Schema 预检与严格输出验收 → 配置/Prompt/计数与统一 Provider 契约 → Chat/Responses 真实适配器的离线协议转换 → Gateway/HTTP/Trace → 全量回归与实际进程 → 六次显式真实 → 文档/代码导读 → Claude Code。每个切片先有目标失败的 Red，再最小实现，同一用例 Green；导入缺失、拼写错误不能作为有效 Red。

新增测试按现有 unit/integration/protocol/smoke 分层组织；实际文件映射见下节。下列为执行命令，运行结果另行归档：

```bash
uv sync --locked
uv run python -m pytest tests/unit tests/integration -q
uv run python -m pytest tests/protocol -q
uv run python -m pytest tests/smoke/test_http_server.py -q
uv run python -m pytest -q
```

逻辑/HTTP 测试使用临时 ModelRegistry/PromptRegistry/Settings 和脚本 MockProvider，不读取真实注册配置或密钥。协议替身只占用回环端口，外部连接由测试边界禁止；只替换外部服务响应，不替换真实 SDK 调用/请求转换、计数器或验收器。每个夹具 finally 清理服务、连接、端口和任务；并发故障用事件屏障控制顺序，不依赖睡眠凑时间。

### 3.2 正式真实接入：六类场景

真实测试沿用现有 `tests/live/test_real_providers.py` 的显式启用方式，保留原四类场景并增加两条结构化场景，总共六个参数化用例，不另外重复执行四例。每例经临时 FastAPI 实际 HTTP 和真实 Provider，不把测试目标 Mock 掉。注册表由测试显式构建，运行配置保持 Mock 不影响该验证。

| 编号 | 接入 / 路径 | 输入与预算 | 必须证明 |
| --- | --- | --- | --- |
| F4-L01 | general-primary / DeepSeek Chat | 旧 context_answer 文本，64 Token/30秒 | text/usage，无 JSON 约束，真实 Usage/完成元信息 |
| F4-L02 | general-backup / SDK Responses | 旧 context_answer 文本，64 Token/30秒 | Responses 迁移后普通文本仍可用，data不加parsed |
| F4-L03 | general-primary / DeepSeek Chat | 1.2.0+基础 Schema，128 Token/30秒 | 实际 JSON mode，真实输出通过本地 Schema 验收 |
| F4-L04 | general-backup / SDK Responses | 同一模板、资料、基础 Schema，128 Token/30秒 | 原生 Schema 约束及本地验收，不在 User 重复 Schema |
| F4-L05 | primary health | 无请求体，16 Token/10秒 | 真实探测成功、latency_ms、用途为 provider_probe |
| F4-L06 | backup health | 无请求体，16 Token/10秒 | Responses 迁移后的真实探测兼容 |

每例仅一次 Provider 请求，SDK/传输均零重试，无 fallback。首轮最多六次、最大输出预算合计 416 Token；失败停止作结论，不能循环调用直到成功。需要复测时记录原因和范围，不把后续调用冒充首轮。真实 Key 由维护者本地配置 `config/credentials.yaml` 的 deepseek-main-key，不在聊天/日志中索取或打印。

显式启用后缺 Key/前置条件、网络失败或能力不符都应失败，不能静默跳过或换成 Mock。检查两条路径的实际出站约束需协议回执与真实SDK方法/路由一致；云端不可见的内部解码实现不作推断。

```bash
uv run python -m pytest tests/live/test_real_providers.py --run-live -x -q -o junit_family=xunit1 --junitxml=/tmp/llm-gateway-f4-live-results.xml
```

报告逐例记录 request_id、adapter/protocol、受控原始状态、实际模型、Schema/消息指纹、计数规则版本、输入本地计数、实际 Usage、差值与耗时；不保留真实 messages/Schema/输出/Key。六个固定样本以预检输入计数与实际 input_tokens 一致为预期；非零差值先失败并定位，不提高余量、放宽断言或自动重试来掩盖。即使六例一致也不宣称所有输入精确一致。

### 3.3 已有技术探测

以下为本轮文档前用户授权的三次独立 Responses 探测，已执行，**不是 F4 Gateway 验收**。直接 SDK，model=deepseek-flash、effort=none、stream/store=false、max_output_tokens=64、SDK重试0；未经过新 Gateway/模板/规范化排序/最终 Schema name。

| 样本 | 响应 ID | 实际 input/output/total | Schema 本体参考计数 | HTTP / 状态 |
| --- | --- | --- | --- | --- |
| 无 Schema | 2eb16a00-087f-41a7-8380-c6473eda6ac6 | 45 / 25 / 70 | 消息计数 45 | 200 / completed |
| 平面 Schema | 613deffb-9e8b-437a-adc8-c7489a2c4bd8 | 105 / 15 / 120 | 105 | 200 / completed |
| 嵌套、引用、可空 | 3dba2f45-ee8d-4f0e-81d0-65a010a09409 | 189 / 29 / 218 | 189 | 200 / completed |

两个 Schema 样本若把整个 type/name/schema 信封代入参考编码，各多算 20 Token。结果只支持当前样本采用 Schema 本体参考编码，不能证明所有 Responses 服务或所有内容相同。

原始 results.json 从临时目录原样归档为 [schema-probe-evidence.json](schema-probe-evidence.json)，SHA-256=`1e4cd576158bd2e9b29b62ef750281193fc6892c266bb2d3e81c954d70c97202`。来源为 `/tmp/llm-gateway-f4-schema-probe-path` 指向目录中的 results.json；原 integrity.json 记载当时 91 个被检查项目文件未变。临时脚本为 `/tmp/f4-native-schema-probe.py`；项目只归档元数据，不复制临时脚本或虚构当时没有保存的完整 HTTP 包。

### 3.4 当前证据与独立审查状态

| 项目 | 当前状态 |
| --- | --- |
| F4 关键 Red / Green | 各切片及用户确认的十进制边界已闭环，历史失败见3.6、最终结果见3.8–3.9 |
| F4 全量离线、协议与实际进程回归 | 最终836 passed/6 live skipped，包括全部协议与4个实际进程场景 |
| Python 3.12 关键兼容验证 | 最终233 passed，弃用警告视为错误 |
| 六次正式真实接入 | 最终单轮6 passed；加此前失败/修复/编码器版本验证共19次调用，各阶段分别归档 |
| implementation-summary.md | 已按实际代码建立，随同代码接受独立审查 |
| Claude Code F4 独立代码审查 | 第1轮APPROVED，P0–P2为0；两项P3观察已核实处置，见3.9 |
| 整项验收 | 验收通过；依据最终功能验证与完整独审，不依据历史探测或文档确认 |

执行时同步留存命令、原始 Red/Green、结果和实际层级；不要事后用新运行补造旧证据。真实回执仅保存安全元数据。使用既有 Claude Code 原生 CLI 只读审查完整代码、测试、配置与导读；按项目 AGENTS 最多三轮，P0–P2 阻断，修复后先原场景复测再审，第三轮仍阻断交用户重审需求/技术方案。工具未正常完成则待独立审查，不能以启动或读文件冒充通过。

### 3.5 模板回看与本次文档核验

已用两个模板组织目标/边界、唯一职责、全量 Prompt、具体契约、逐例参数/副作用/Trace 和分层证据。设计模板补充外部 API 官方依据与验证边界，避免把参考编码当作已验证真实请求；测试模板已覆盖显式真实、原始证据和共同断言，本次无需改动。此项仅检查文档完整性，后续实际遗漏再闭环改进，不提前声称模板改进提高了代码质量。

2026-11-01 本次文档核验：6 个 JSON 代码块、2 个 YAML 代码块解析通过；120 个本地文件链接存在，代码围栏成对。比对写入前后文件摘要：仅修改 Markdown 并原样归档探测元数据，源码、测试、依赖、默认运行配置、资源及 F1–F3 历史 Feature 工件未改；探测归档摘要与原结果文件一致。检查回执保存在 `/tmp/llm-gateway-f4-docs-check.json`。这不是功能测试、Schema 运行校验或 Claude Code 独立审查。

### 3.6 开发中实际执行记录（未验收）

- 基线：`uv run python -m pytest -q`，606 passed / 4 skipped，46.66s；最初原始输出在工具回执，本地未另保存完整基线 stdout，不补造历史文件。
- Schema HTTP Red：原 Schema={} 422 与目标 400 不符；Schema 图资源、深度、数值精确验收均有后续 Red/Green。当前模块单元测试已覆盖 84 个场景，后续补充验证仍继续。
- 平台 Red 16 failed/10 passed → 平台/原模板 Green 70 passed；完整生成约束 Red 5 failed → 计数/旧黄金 Green 38 passed；Gateway/Schema 首轮组合 Green 116 passed。
- 协议适配器：实际 SDK/HTTP 回环，100 passed，26.81s；没有隐式重试，10秒探测总预算与持续读超时断言保留。
- 第一轮全离线：771 passed / 4 skipped，49.57s；该结果早于后续测试补充和新发现的入站数值问题，不冒充最终回归。
- 实际进程：smoke 4 passed，2.01s；含临时单 worker 的结构化成功、预检零调用、输出失败与落盘测试快照（不是产品 Trace 查询 API）。
- Python 3.12 隔离环境关键集：170 passed，3.47s；同样不是最终新增场景全量结论。
- 真实首轮：L01/L02 通过；L03 计数差值 +22，2 passed/1 failed，按 -x 停止。原始回执见 live-first-results.xml。
- 实际字段与固定参考编码对照确认漏计 JSON mode 格式标志：仅 messages=572，参考 type=object 为593，参考 type=json_object 为594，与实际594相同。新增有效 Red（572!=594）→相关集成/黄金计数71 passed；不加固定22、不提高安全余量、不更改Prompt。
- 真实修复复测与余下场景：仅选择 structured/probe，L03–L06 共4 passed/2 deselected，3.66s。与此前通过的 L01/L02 合计六类场景成功，累计7次调用（含首轮失败），没有把复测冒充首轮。

| 场景 | 本地输入 / 实际输入 | 差值 | 实际输出 / total | 说明 |
| --- | --- | --- | --- | --- |
| L01 文本主 | 358 / 358 | 0 | 13 / 371 | 首轮 |
| L02 文本备 | 515 / 515 | 0 | 9 / 524 | 首轮 |
| L03 Chat结构化 | 594 / 594 | 0 | 27 / 621 | 修复复测；首轮572/594失败独立保留 |
| L04 Responses结构化 | 615 / 615 | 0 | 27 / 642 | 修复后余下场景 |
| L05 主探测 | 237 / 237 | 0 | 1 / 238 | 固定16/10 |
| L06 备探测 | 237 / 237 | 0 | 1 / 238 | 固定16/10 |

本阶段曾返回需求节点：入站 Schema minimum/const 的超精度字面量被默认解析舍入，新增两个正确 HTTP Red。用户后续明确选择完整十进制保真，以下3.7记录实现与复测；本段首轮结果保留，不改写为最终版本结论。

### 3.7 用户确认十进制保真后的阶段执行

- 再次运行原两个入站HTTP Red，2 failed；Schema有限Decimal最初拒绝的单元Red为6 failed/3 passed，修复后对应独立集106 passed。
- 共享json_codec被HTTP、Schema、完整计数和SDK复用；严格JSON、精确小数/指数、JSON数字序列化、旧JSON格式兼容性均有检查。5000位整数最初触发Python默认4300位限制，新增正确Red，再采用Decimal回退保留，不改变全局安全限制。
- SDK资源helper默认encoder不能表示Decimal，改用同一个真实SDK公开post('/responses',content=精确JSON字节)；原body=bytes的弃用警告已纠正，不过滤警告。精度专项205 passed，5.01s，启用 `-W error::DeprecationWarning`。
- 最终默认全量：834 passed/6 live skipped，53.88s。包括真正HTTP协议、默认离线边界、单worker实际进程及原功能回归；不是只执行精度子集。
- 最终Python3.12隔离环境关键集：231 passed，5.35s，同样启用弃用警告视为错误；锁文件安装43个包，未改项目主环境。
- 最终版本六例真实接入：6 passed，6.62s；[最新原始回执](live-final-results.xml)。两条文本、两条结构化和两条探测均单次调用，六例预检与实际输入计数差值均0。此前7次保留历史，累计13次本功能调用，不包含历史三次技术探测。
- 此阶段尚未发起Claude Code；完整第1轮结论与处置见3.9。

### 3.8 响应编码边界与提交独审的版本

精度链路完成后复核到一个实际失败：根对象允许额外字段时，600层额外数组已被JSON解析及Schema验收接受，但递归编码器在HTTP响应阶段返回500。新增编码器/HTTP两个正确Red，改为显式栈编码，相关183例Green；未擅自收紧additionalProperties或Schema层数。值、字节和数字保真策略不变。

该修改后全量离线836 passed/6 live skipped，52.63s；Python3.12关键233 passed，5.15s（弃用警告视为错误）。随后只调整新增F4两个测试文件为显式静态注册表，避免读取开发者运行YAML，原相同场景25 passed；未改产品源码或放松断言。更新后的真实六例6 passed，6.74s，回执见 [live-release-results.xml](live-release-results.xml)。此前13次调用保留，累计19次本功能调用，最新六例计数差值均0。

模板闭环补充：数字误验收来自HTTP解析与SDK序列化边界，测试模板增加实际值/类型/出站字节断言；不把Decimal、固定限制或供应商名字写成所有Feature要求。新增代码导读已按实际函数组织并纳入独审，最终通过前不改验收状态。

### 3.9 Claude Code 独立审查与处置

原生CLI完整第1轮已完成：`CLAUDE_CODE_INLINE_TOOLS=false claude --print --tools Read --allowedTools Read --no-session-persistence --output-format stream-json --verbose`，沿用已有连接与模型配置，无模型参数覆盖。退出码0、is_error=false、stop_reason=end_turn、subtype=success，713.217秒。清单56/56文件和2份指定安全证据均实际Read，无工具读取错误；66份源码/测试/默认配置摘要在源码审查前后相同。原始报告见 [review.md](review.md)，原始流/结果的路径与SHA-256、实际读取清单及命令回执见 [evidence.json](evidence.json)。

结论为 APPROVED，P0=0/P1=0/P2=0/P3=2。审查者仅Read，所有测试结论来自执行者回执，没有把审查者静态分析写成其实际运行。

| 观察 | 核实与处置 |
| --- | --- |
| P3-1 建议改变JSON Pointer百分号解码顺序 | 不采纳。按 [RFC6901 §3/§6](https://www.rfc-editor.org/rfc/rfc6901.html#section-6)，URI片段解码后按JSON Pointer解释；token内字面斜杠使用~1，不用%2F代替。原文将这两层混淆。已构造同时含嵌套路径与斜杠键名的Schema，现实现与标准Draft202012Validator结果一致：a%2Fproperties%2Fb指向嵌套integer，a~1properties~1b指向字面键的string。实际只读脚本输出保存在evidence的pointer-disposition记录，无源码或测试修改，不因错误建议改变正确行为。 |
| P3-2 原F3测试版本断言迁移 | 记录，无需修改。计数规则扩展已确认，版本断言迁移必要；原黄金编码/计数仍逐项比较，fixtures与23份历史Feature验收工件摘要未变。当前测试源码的必要迁移不等于改写历史验收报告。 |

两项均无需修改受审代码，未启动第二轮。独审后源/测试/默认配置66份摘要仍一致，4份固定资源、23份历史工件及本地.env/credentials摘要未改。所有变更后的必测场景均已执行，最终全量836 passed/6 live默认跳过、最低Python3.12关键233 passed、最新真实六例6 passed（差值均0），本项验收通过。真实/协议/Mock证据边界继续保留，不宣称生产或通用计数一致性。

实际用例映射：S/V/N资源与语义主要在 tests/unit/test_f4_validation.py；精确编解码在 test_f4_json_codec.py；C/P/B分别在 test_f4_contracts.py、test_f4_prompts.py、test_f4_budget.py；HTTP错误顺序、终态、预算、数字和并发在 tests/integration/test_f4_gateway.py 与最初 schema_red.py；W在 tests/protocol/test_f4_providers.py 并迁移保留三个F3协议文件；R执行原全量与 tests/smoke/test_http_server.py；L为现有live文件扩展为总六例。原始命令、失败/通过输出、耗时见 evidence.json。
