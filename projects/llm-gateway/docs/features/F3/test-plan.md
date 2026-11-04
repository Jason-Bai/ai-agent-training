# F3：模型配置、统一 Provider 与真实接入——测试方案

> 状态：验收通过（2026-11-01）。570个离线测试、4条真实必验及Claude Code第2轮补审通过；默认测试仍跳过live，显式真实结果单独记录。

## 1. 范围与通过标准

### 1.1 基线与刻意变化

基线为 [F2 验收记录](../F2/test-plan.md) 的历史 223 个用例及用户验证通过；历史通过不是本轮运行结果。原 F1/F2 工件不改写。

必须保护原模板正文/变量/字符限制/消息顺序与指纹、统一错误信封、请求 ID、零调用拒绝、异常和 ASGI 中断不误记成功、配置注入与应用隔离。F3 明确改变下列预期，迁移测试时逐项记录原场景与新断言，不把这类变更计为删除失败测试：

| 原行为 | F3 预期 | 迁移验证 |
| --- | --- | --- |
| 普通 data 含 model/actual_model/attempts | 精确只含 text/usage | 两条路径完整 JSON 断言，同时检查内部元信息仍在 |
| .env 全局 LLM_GATEWAY_PROVIDER | YAML 按模型绑定 Provider；.env 不存密钥 | 旧键迁移提示、新默认 Mock、真实引用严格校验 |
| 仅 general-primary | 两个稳定逻辑名 | 主备显式调用，主失败不自动调用备用 |
| 请求不接收预算字段 | 增加两个可省略正整数字段 | 默认、覆盖、类型、边界和出站参数 |
| 只有普通 POST 建模型 Trace | 主动探测也建用途与尝试记录 | 校验前 ID、失败阶段、GET/404/405 无调用记录 |
| Provider 内部接口只有逻辑名与消息 | 统一模型参数/预算/元信息与分类异常 | 原调用次数、消息断言保留，按新接口调整 fixture |

### 1.2 分层和验收门槛

| 层次 | 证明范围 | 无法证明 |
| --- | --- | --- |
| 单元 | 配置、预算、计数黄金、Usage、分类规则 | HTTP、远程服务 |
| Mock 服务/进程内接口 | 真实 Gateway 编排与响应/Trace/并发；Mock 不替代被测预算或路由 | 真实适配器协议与外部可用性 |
| 适配器协议 | 真实两适配器及实际 SDK；替身 HTTP 服务观察完整出站请求、返回受控响应 | 真实账户与供应商可用性 |
| 实际进程 HTTP | 单 worker 工厂、默认文件配置、运行目录无关、退出清理 | 真实供应商 |
| 真实 Provider | 两条接入各一次普通调用和一次主动探测，真实网络/模型/Usage/计数差异 | 生产可靠性、所有输入计数一致、极限窗口能力 |

全部必测参数化变体、刻意变化迁移和其他基线回归通过，副作用/Trace 对应，四条真实用例通过，Claude Code 独立审查无 P0–P2 才能宣布 F3 验收。Mock、协议替身和真实回执分别记录；真实用例跳过或缺前置条件不代表通过。

范围外：F4 Schema、F5 SSE、F6 主动取消、F7 重试/加预算/fallback、数据库、Router、全局限流与管理 UI；本项只保留原有中断传播回归，不新增取消功能。

## 2. 用例

### 2.1 确定输入、替身边界与共同断言

普通请求基准：model=general-primary；prompt=basic_text@1.0.0；messages 仅一条 user，content=`Explain TTFT`。分别省略/显式提供 max_output_tokens=1024、timeout_seconds=30；另参数化 general-backup。context 请求复用 F2 静态黄金：问题“订单 A123 的商品是什么？”，context“调用方提供的资料：订单 A123 的商品为充电器。”，分别选择 context_answer 1.0.0/1.1.0，完整消息 2/6 条。

协议替身的成功原始响应固定如下；除指定变更字段外全部保持不变，避免一个错误被另一个错误遮住：

```json
{
  "id": "provider-test-response",
  "object": "chat.completion",
  "created": 0,
  "model": "deepseek-flash",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "TEST_REPLY"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
}
```

普通成功预期为精确四字段信封，data 精确 `{text: "TEST_REPLY", usage: {input_tokens:120, output_tokens:30, total_tokens:150}}`；Mock 默认正文为 MOCK_REPLY、Usage=null、内部 actual_model=null。探测成功 data 精确仅 latency_ms，非负数；不强制回复文字逐字 OK。

每个 HTTP 用例共同检查真实状态码、success/code/data/message、唯一非空 X-Request-ID，ID 不进入正文。每个拒绝用例检查调用次数和尝试数，不靠状态码推断；错误不包含 PRIVATE_INPUT_MARKER、PRIVATE_ERROR_MARKER、FAKE_CREDENTIAL_MARKER、堆栈、消息全文或任意用户键名回显。

每个 Provider 协议用例对两种真实适配器执行，不替换 Adapter.complete、SDK 方法或其解析器。替换边界为本地 HTTP 服务/受控传输，只使用假密钥；观察端点、请求模型、完整 messages、max_tokens、thinking.type=disabled、调用次数和资源释放。可共享断言，不能用 SDK 路径复用直接 HTTP 的通信实现冒充两条独立路径。

计数黄金在实施准备完成资源核验后、写计数实现前，由固定官方资源独立编码确定：保存输入消息、编码字节/特殊标记、Token ID/数量及资源 revision/hash。测试期望不调用被测计数器生成；不能仅用“调用同一函数两次相等”作为黄金。预算边界使用独立黄金计数 N，注入合法小窗口配置（例如默认输出 16、平台上限 32、安全余量 8），分别构造 N+16+8 与 N+16+8−1，避免真实 1M 窗口被 F2 字符限制挡住而用例无法触达。

### 2.2 配置与资源（C 组）

| 编号 | 设计规则、准备与输入 | 预期响应/错误 | 调用/其他副作用 | Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| F3-C01 | 3.1/3.2 默认两 Mock 的完整 YAML，无凭据文件 | 应用启动；两逻辑名可用 | 启动 0 模型调用；两个实例由配置构造 | 请求时 adapter=mock | 单元+HTTP |
| F3-C02 | 真实示例两条 Provider，假凭据同一引用 | 注册正确，主备分别绑定目标实现 | 工厂构造不连网；每次只选指定 Provider | ID/adapter/配置模型准确 | 单元+协议 |
| F3-C03 | gateway 文件缺失、语法错误、重复 provider/model/字段键 | 启动失败 | 客户端工厂和模型调用均 0 | 无请求 ID/Trace | 单元+进程 |
| F3-C04 | root/provider/model/credentials 的未知字段、错类型、空白标识、空集合 | 严格拒绝，不做隐式转换 | 0 模型调用 | 无请求记录 | 单元 |
| F3-C05 | 未知 Provider 引用、真实凭据引用缺失/空白、未知 adapter | 启动失败，安全定位项 | 不创建模型尝试，不打印值 | 无请求记录 | 单元 |
| F3-C06 | 全部正整数字段分别置 0/−1/true/"1024"/1.5/null；默认预算大于平台、平台大于供应商；默认超时大于上限；默认输出+余量无输入空间 | 启动失败 | 0 调用；合法等边界另测 | 无请求记录 | 单元 |
| F3-C07 | model/Provider 容器、深层配置被修改；调用方原字典被修改；两个应用注入不同注册表 | 冻结/复制隔离，另一应用不变 | 无共享可变选择状态 | ID 与模型归属无串线 | 单元+HTTP |
| F3-C08 | ENV 三个合法值与非法值；进程环境/.env/默认优先级；旧 LLM_GATEWAY_PROVIDER 存在 | ENV 合法且不切 Provider；旧键明确迁移错误 | 无真实 .env 修改或秘密环境回退 | 不记录设置值全文 | 单元+进程 |
| F3-C09 | 正常目录与外部 cwd；显式配置/注册表/Provider 注入；并存 gateway.test.yaml/gateway.production.yaml | 路径按项目根，仅加载 gateway.yaml；测试显式依赖不读用户文件 | 默认无网络；不导入时创建应用 | 应用独立 | 单元+进程 |
| F3-C10 | tokenizer/编码资源缺失、摘要不符、不支持 token_counter、与模型/模式不匹配 | 启动失败，不回退字符换算 | 工厂/生成调用 0；无请求下载 | 无请求记录 | 单元 |
| F3-C11 | 工厂构造第二客户端失败；正常关闭；注入 Provider 的 fixture 结束 | 工厂拥有的客户端准确释放；注入对象由 fixture 管 | 关闭一次，无泄漏/重复关闭 | 不伪造请求 | 单元+进程 |
| F3-C12 | 启动、GET /healthz；模拟远端凭据无效但本地字段合法 | 启动与 GET 不探测远端；GET 只表示存活 | 模型调用 0 | 不新增模型请求/尝试 | HTTP+协议 |

### 2.3 输入、预算与确定顺序（B 组）

| 编号 | 设计规则、准备与输入 | 预期响应/错误 | 调用/其他副作用 | Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| F3-B01 | 省略两预算字段；显式默认值；显式 1 与平台上限 4096/120 | 成功；默认 1024/30，原文不变 | 各 1；参数准确，消息黄金/指纹与 F2 相同 | 生效预算准确 | 单元+HTTP |
| F3-B02 | 两新字段分别 null/true/false/"16"/1.5/0/−1/数组/对象 | 422 invalid_request | 0 调用/尝试 | 指纹 null，失败请求 | HTTP |
| F3-B03 | 输出 4097，超时 121；分别测试二者同时超限 | 400 对应预算错误；同时错误优先输出 | 0 | 已有指纹；不计输入 | HTTP |
| F3-B04 | 官方独立黄金：basic/context 1.0/context 1.1，中文/英文/换行/标点与角色边界 | 计数精确匹配所固定本地资源，包含静态示例和生成标记 | 计数不生成请求、不下载 | 计数资源身份正确 | 单元 |
| F3-B05 | 独立 N 的小窗口等边界与超 1；显式/省略预算均测 | 等于允许；超 1 为 context_length_exceeded | 1/0；超限不裁剪、不调预算 | 拒绝有 N/预算/指纹、0 尝试 | 单元+HTTP |
| F3-B06 | 字符限制临界值及旧非法变量全部回归；小窗口让 System/Few-shot 加入后才超限 | 原字符错误保留；总 Token 超限拒绝 | 0；不能仅计末条 User | 失败阶段正确 | 单元+HTTP |
| F3-B07 | 运行期计数器抛 PRIVATE_ERROR_MARKER；测试注入可观察计数器 | 500 internal_error，安全消息 | 0 调用/尝试，不绕过预检 | 指纹保留，未知计数 null | HTTP |
| F3-B08 | 缺变量+未知模型；未知模型+超预算；输出+超时超限；字符超限+计数器异常；超输出+计数器异常 | 按设计3.4顺序返回最先错误 | 0；spy 检查后续阶段未执行 | 指纹/计数写入阶段准确 | HTTP |
| F3-B09 | 请求提交地址/凭据/adapter/thinking/stream/response_schema；外部 system/assistant/多消息 | 422 invalid_request | 0 | 无敏感回显 | HTTP+基线 |
| F3-B10 | 同一消息换逻辑模型、预算、超时；两请求 ID 不同 | 消息指纹相同、元信息分别记录 | 每请求 1，参数不串线 | 不把预算/ID 混入指纹 | HTTP |

### 2.4 Provider、结果与错误（P 组）

| 编号 | 设计规则、准备与输入 | 预期响应/错误 | 调用/其他副作用 | Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| F3-P01 | 两逻辑名经配置分别调用；使用2.1成功响应 | 200，data 精确 text/usage，无 model/actual_model/attempts | 仅目标 Provider 1 次 | 内部仍有逻辑名/实际模型/尝试数 | HTTP+协议 |
| F3-P02 | F2 三模板黄金，context 1.1 含静态 assistant | 完整消息逐字一致，2/2/6 条 | 每次 1；端点 /chat/completions，model、max_tokens、非思考、假密钥头准确 | 消息指纹黄金保留 | 协议 |
| F3-P03 | Usage 缺失/null；完整0/0/0；120/30/150；缓存命中100、未命中20 | 未知 null；其余准确归一，不累加缓存 | 1 | 与实际返回一致，预检计数不冒充 Usage | HTTP+协议 |
| F3-P04 | Usage 提供但缺任一项，true/字符串/小数/负数、总数矛盾 | 502 provider_invalid_response | 1，无恢复 | 失败，非法 Usage 不保存成已知 | 协议 |
| F3-P05 | 响应非JSON、非对象、缺/空choices、正文缺失/null/非字符串/空白、真实 model 缺失/空白、finish_reason 缺失/null/未知、意外tool_calls | 502 provider_invalid_response | 1；不返回原始正文 | 失败，安全元信息边界 | 协议 |
| F3-P06 | length+合法Usage；length+非法Usage | 前者 output_truncated；后者 provider_invalid_response；均502/data=null | 1，不加预算、不续写、不调备用 | 合法元信息保留，正文不保存 | HTTP+协议 |
| F3-P07 | finish_reason=content_filter / insufficient_system_resource / aborted | 前者502/provider_content_filtered；后两者503/provider_unavailable | 各1 | 结束原因及合法Usage保留，失败 | 协议 |
| F3-P08 | 上游401、402、429 | 分别502/auth、503/quota、503/rate，使用完整设计code | 每种1；SDK无自动重试 | 上游类别正确，无错误正文/密钥 | 协议 |
| F3-P09 | 上游400/404/422、500/503、其他未分类HTTP错误 | 分别 rejected_request / unavailable / provider_error | 每种1；SDK与HTTP映射一致 | 失败类别准确 | 协议 |
| F3-P10 | 连接拒绝、受控连接异常 | 502 provider_connection_error | 1，无备用/重试 | 失败/指纹保留 | 协议 |
| F3-P11 | 受控替身连续慢速返回，使单段读取未超时但总时限耗尽；使用合法1秒测试预算 | 504 provider_timeout，实际总时限生效 | 1，本地等待终止/连接释放；测试容忍合理调度误差 | 失败、未知Usage不补0 | 协议+HTTP |
| F3-P12 | 解析/编排代码抛未预期异常；包含敏感标记 | 500 internal_error，不伪装成上游分类 | 调用前0、调用期间1 | 正确失败阶段 | HTTP |
| F3-P13 | 并发不同模型/预算/模板，Event强制逆序进入与完成 | 两结果正确，不依列表位置配对 | 各目标1次，客户端复用但无参数覆盖 | 按request_id/固定输入关联 | HTTP+协议 |
| F3-P14 | 请求/尝试中断、ASGI响应发送失败、重复收口 | 原中断行为保留，不误记完整成功；收口只一次 | 不额外调用；资源清理 | 尝试与请求状态分别反映证据 | 基线+HTTP |

### 2.5 主动探测与运行（H 组）

| 编号 | 设计规则、准备与输入 | 预期响应/错误 | 调用/其他副作用 | Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| F3-H01 | 两逻辑名分别POST健康接口，不带body | 200，data精确latency_ms≥0 | 1；basic_text+固定用户句、16/10、非思考 | operation=provider_probe，耗时与响应一致 | HTTP+协议 |
| F3-H02 | 显式注入正文为 TEST_REPLY 的 Mock（默认正文仍为 MOCK_REPLY），不是逐字 OK | 探测成功，证明不依回复逐字匹配 | 1；正文/Usage不对外返回 | adapter=mock，不标真实可用证据 | HTTP |
| F3-H03 | 探测带非空body：自定义Prompt/预算/空对象/任意文本 | 422 invalid_request | 0 | 用途已知，指纹尚未生成 | HTTP |
| F3-H04 | 未知模型；已注册模型平台上限低于16或超时低于10 | unknown_model / invalid_output_budget / invalid_timeout | 0，不自动适配预算 | 指纹阶段及参数准确 | HTTP |
| F3-H05 | 探测路径复用P组认证/限流/超时/截断/损坏响应 | 同一错误code与信封，data=null | 每次1，无重试/缓存/自动禁用 | 失败耗时、合法Usage内部保留 | HTTP+协议 |
| F3-H06 | 连续两次探测；夹普通请求；GET /healthz、错method和不存在路径 | 两次均实际执行；存活200；405/404沿用 | 探测2+普通1，其余0 | 两种用途隔离；404/405无模型记录 | HTTP |
| F3-H07 | 临时默认Mock YAML启动真实单worker子进程，从外部cwd调用普通/健康接口后停止 | 两模型普通和探测均符合契约 | 无外部调用、无真实凭据读取；子进程可回收 | 本地证据，不冒充真实Provider | 实际进程 |

F3-H05 的超时变体保留探测固定 10 秒，使用挂起的协议替身真实等待，两适配器顺序执行约需 20 秒；不借 P11 的 1 秒普通请求预算绕过探测契约。超时结束后清理服务端挂起任务和客户端。

### 2.6 真实接入必验（L 组）

真实组用临时独立真实注册表，绑定设计3.2两个真实Provider；仅显式开启后读取本地 credentials.yaml，不修改默认配置或用户凭据。启动本地 FastAPI 实际 HTTP 服务，测试通过回环 HTTP 请求进入路由，读取该测试应用内存 Trace 检查元信息，不新增产品查询路由。

| 编号 | 设计规则、准备与输入 | 预期响应/错误 | 调用/其他副作用 | Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| F3-L01 | general-primary，context_answer@1.0.0，2.1订单事实；显式输出64、超时30 | 200，非空白正文、完整信封及合法实际Usage；不要求固定措辞 | 真实DeepSeekProvider 1次，无自动备用 | 实际模型/stop/资源版本、计数差异、耗时；输入超出本地计数的差额不超过1024 | 真实HTTP+Provider |
| F3-L02 | general-backup，context_answer@1.1.0，同订单事实；省略预算和超时 | 200；使用平台1024/30；合法实际Usage | 真实OpenAICompatibleProvider 1次 | 与L01相同，适配器/Provider身份正确 | 真实HTTP+Provider |
| F3-L03 | general-primary 主动探测，无body | 200，仅耗时；16/10策略生效 | 真实主Provider 1次 | 用途/实际Usage/计数差异与资源版本，stop | 真实HTTP+Provider |
| F3-L04 | general-backup 主动探测，无body | 同L03 | 真实备用Provider 1次 | 用途/身份/实际Usage/计数差异准确 | 真实HTTP+Provider |
| F3-L05 | 未显式开启；显式开启但缺/空凭据或真实配置错误 | 默认组不加载秘密、不连外部；显式开启则前置条件失败，不静默skip | 前置失败0真实生成 | 不伪造成功/远程Trace；报告未验证原因 | 启用边界 |

L01–L04合计4次平台生成尝试，不代表供应商最终处理或计费次数的证明。实际Usage对比必须来自原始已解析响应，不由本地计数伪造；常规耗时/Usage仅检查合法性，不写死数值。本次配置最大输出合计上限为64+1024+16+16=1120，实际通常更少；无隐式重跑。真实失败如实记录，先定位再决定复测，不以重复执行直到绿掩盖失败。

本地预检差异记录有方向：actual_input−local_count，可为负；当实际大于本地时检查是否超过安全余量。超出则本项真实计数验收不通过，保留证据、调查资源/编码，不静默扩大余量。四例成功也不推出所有输入或未来模型升级后都一致。F3不以发送1M输入或制造真实限流/余额耗尽来测试边界，相关边界由固定黄金与协议替身覆盖。

### 2.7 回归与资料一致性（R 组）

| 编号 | 场景与输入 | 预期 | 副作用/Trace | 层次 |
| --- | --- | --- | --- | --- |
| F3-R01 | F2全部223场景逐项对照，按1.1明确迁移相关装配与断言 | 无无关用例删除、无错误码/黄金/预算边界放宽 | 原零/一次调用、消息与中断语义保留 | 全量回归 |
| F3-R02 | 响应/异常/配置/CLI输出/所有Trace中搜索固定假秘密与私有标记 | 无敏感泄漏，模型元数据内部保留 | 不要求访问真实秘密来做脱敏测试 | 单元+协议+审查 |
| F3-R03 | README/AGENTS/契约/架构/进度/模板、引用和状态核对 | 当前/目标清楚，运行示例与实际实现、验收状态一致 | 原F1/F2工件与用户实际配置不改写 | 文档/文件检查 |
| F3-R04 | Claude Code审查完整变更/配置示例/资源来源/测试及真实证据 | P0–P2为0；P3逐项处置，最多3轮 | 最终判定、正常退出、实际读取范围可取证 | 独立代码审查 |

共52组编号（C12+B10+P14+H7+L5+R4），每组列举的所有变体均为必测，不将组数冒充最终 pytest 展开数。实际执行与未验证项见第3节；组数不等于参数展开后的测试数。

## 3. 执行计划、证据与结论

### 3.1 实施准备与目标命令

以下入口已实现；离线组已运行，live 组须本地凭据就绪后显式开启。

```bash
uv sync --locked
uv run python -m pytest tests/unit tests/integration -q
uv run python -m pytest tests/protocol -q
uv run python -m pytest tests/smoke -q
uv run python -m pytest -q
uv run python -m pytest tests/live --run-live -q
```

- 环境：项目Python>=3.12；保留现有uv隔离环境。实施准备选定并锁定YAML解析、tokenizers、异步HTTP与OpenAI SDK的实际兼容版本；计数依赖不加载模型权重。核验固定资源、来源/许可与摘要，不在请求里安装/下载。
- 默认全量包括离线组，live用例在未开启时标记跳过并明确列出；默认不得读取真实凭据，fixture惰性加载且不在收集阶段读取。`--run-live` 是已实现的显式 pytest 开关，不是生产接口/ENV模式。
- live启用时缺依赖/资源/凭据即前置失败；离线绿+live未执行只能报部分验证，不能F3验收。
- 所有离线配置在临时目录或显式对象中创建；当前 .env、credentials.yaml 不被测试覆盖。默认配置纯Mock，真实注册表由live fixture从设计示例建立，凭据只显式只读引用，不能检测到密钥就自行联网。
- 协议测试运行回环替身HTTP服务：服务端按脚本返回2.1正文或对应状态/分块延迟，使用假密钥；真实SDK和真实适配器完整运行。统计服务器实际收到的次数，证明没有隐藏重试。拒绝用例检查0次而不只看Gateway attempts。
- 对并发使用Event受控交错；按request_id/确定输入关联。对总超时使用1秒合法配置与持续分段慢响应，清理任务和连接，避免用Mock直接抛Timeout替代总时限验证。耗时用单调时钟并容忍调度差异。
- 进程/HTTP/客户端/临时文件由fixture的finally清理；探测耗时与内部尝试耗时同一测量来源；真实服务测试只观察元信息，不打印凭据或完整响应。

### 3.2 TDD 与执行顺序

1. 保存F2当前代码/测试/历史工件基线。只读核验计数资源具体revision/摘要和独立黄金；不把下载成功当功能实现或API一致性证明。
2. 测试先行：配置重复/引用拒绝、两逻辑名选择、预算边界、响应字段删除、健康请求等代表用例观察正确断言失败；不接受导入/依赖错误作为有效Red。每个增量先Red再Green，扩完整矩阵。
3. 实现最小配置/注册、统一Provider/计数/调用链；通过Mock层后，接入真实HTTP/SDK并用协议替身覆盖出站字段、所有错误和总超时。
4. 全量离线与真实进程验证；逐项回归迁移审查，无依据不反复扩大测试。
5. 显式开启L组，四条真实调用及启用边界验证；记录时间、命令、环境/依赖、配置身份、资源版本、请求ID、实际次数/Usage/计数差异和结果。前置失败与调用失败分别报告。
6. 同步实现文档和逐例结论，Claude Code只读审查全部变更与证据；修复后原场景复测再审，第三轮仍有P0–P2才交用户重审；不可用则待独立审查，不以替代Agent冒充。

### 3.3 证据与当前结论

| 项目 | 当前状态 |
| --- | --- |
| 分项需求、完整设计和测试方案 | 用户已确认并授权实现 |
| 资源 revision/hash、独立黄金、依赖 | 已核验，详见3.5与manifest |
| F3源码/依赖/配置文件/测试实现 | 已落地；用户已在本机配置独立凭据 |
| Red/Green/协议/实际HTTP | 已执行；完整离线570 passed、4 live skipped |
| 真实调用 | 2026-11-01四条必验4 passed；真实元信息见3.8 |
| Claude Code实现审查 | 第1轮离线无阻断；第2轮补齐真实证据，APPROVED、P0–P2为0 |
| F3验收 | 2026-11-01验收通过，离线570、真实4、Claude Code第2轮APPROVED |

执行时将关键命令的真实stdout/stderr/退出码分阶段保存到本目录evidence.json；原始失败也保留。按编号回填实际测试位置、参数展开、通过/失败/跳过、副作用、未验证项和结论，不只报总数。实际完成审查后再建立review.md并保存原文，处置写回本方案；当前不建空工件。

模板回看：现有模板已要求确定输入、替换边界、真实启用与副作用，当前只需按其内容填全；未发现需要新增通用条款的已证实缺口，本轮不增加额外QA目录或CSV。后续若实施暴露通用问题，按项目闭环规则最小回填。


### 3.4 实施前文档核对历史（非功能验收）

本小节记录实施前的文档阶段，以下状态均指当时，不代表当前实现状态。本轮仅修改 Markdown。YAML 三段示例实际解析通过，默认两模型引用与预算不等式核对通过；JSON 示例、围栏、本地链接和52个唯一用例编号已检查。源码、现有测试、依赖与F1/F2历史工件共40份受保护文件SHA-256不变；没有执行F3功能测试或真实调用，也没有创建运行配置/凭据/计数资源。

Claude Code 文档只读审查第1轮：原生CLI沿用既有配置、仅开放Read；进程退出0，subtype=success、is_error=false、stop_reason=end_turn，DOCUMENT_VERDICT: APPROVED，未发现P0–P2。实际8次Read均返回且无错误，覆盖两个F3文件、两模板和四个基线源码文件。下方原文自述的AGENTS读取没有对应工具记录，读取范围以本段实际取证为准；项目约定由执行者另行核对。不将此次文档审查冒充实现审查。

四项P3均已在本文档阶段最小对齐：H02明确为显式注入TEST_REPLY；旧全局键明确启动失败并给迁移提示；H05写明固定10秒探测超时的真实等待成本；C09补充并存多环境文件只加载gateway.yaml的负例。修订不增加功能范围，完整测试方案仍待用户确认。具体资源版本/兼容性及F3运行结果仍为未验证。

实际调用参数（输入/输出为临时文件，不含凭据）：

```sh
CLAUDE_CODE_INLINE_TOOLS=false claude --print --tools Read --allowedTools Read --no-session-persistence --output-format stream-json --verbose < doc-review-prompt.txt > doc-review-round1.jsonl
```

<details>
<summary>Claude Code 第1轮文档审查原文（保留修订前意见）</summary>

## F3 文档独立审查（只读）

**审查方式**：只读核对 F3 design/test-plan、两份模板、docs/AGENTS.md、AGENTS.md 与 src/contracts.py、services/gateway.py、providers/base.py、services/trace.py 基线。未运行任何命令、未联网、未读取实际 .env/凭据，**未执行任何 F3 测试或实现审查**（F3 无源码，未将其作为缺陷）。

### 逐项核对结论

| 核对项 | 结果 |
| --- | --- |
| 已确认要求覆盖 | 全部落文：.env 无密钥/YAML+独立凭据（3.1，C01/C05/C08）；Mock 正式适配器默认可用、测试可注入（3.2，C01/C07/C09）；主 DeepSeek/备 OpenAI 角色、均接 deepseek-flash（2.2/3.2，P01）；直接 HTTP 与 SDK 双路径（2.1/3.4，P 组两适配器各执行）；非思考、1024/4096/余量 1024、30/120（3.2/3.4，B01/B03/C06）；固定资源、缺失失败不字符回退（3.1/3.3，C10/B07）；data 仅 text/usage（3.5，P01+迁移表）；统一错误不重试/切换/裁剪（3.6，P06–P10）；探测固定 basic_text/16/10/仅耗时、GET 不调用（3.7，H01–H06）；F7 才加预算重试、持久化后置（1/3.6/3.8）；三层测试与真实双路径普通+探测必验（1.2，L01–L04） |
| 设计内部矛盾 | 无阻断级。3.4 执行顺序与 B03/B08 组合错误优先级一致；预检公式与 B05 等边界/超 1 一致；Usage 归一与 P03/P04 一致；3.6 错误表与 P07–P10 一致；3.7 固定 16/10 与 H04 拒绝路径一致 |
| 配置可操作性 | 通过。默认 Mock 无凭据文件可启动（C01）；真实注册表整段替换+凭据独立文件，启动仅本地校验、不远程认证（C12/C02） |
| 可复现性/独立断言 | 通过。黄金由固定官方资源独立编码、不调用被测计数器（B04/2.1）；替身固定响应+受控变更字段；错误映射用替身状态非 Mock 抛异常；并发 Event 受控交错（P13）；总超时用 1 秒合法预算+分段慢响应（P11）；live 显式开关、缺前置不静默跳过（L05） |
| 52 组矩阵 | 12+10+14+7+5+4=52 计数正确；1.1 迁移表 6 行均有对应用例；设计 3.1–3.8 各节均有用例覆盖，无已确认规则缺测 |
| 当前/目标边界 | 通过。两文件头部、3.1 命令段、3.3 状态表均明确"未实施/未执行"；不建空 review.md/evidence.json 符合 docs/AGENTS.md 规则 4 |
| 技术核验冒充 | 无。资源 revision/摘要、SDK/HTTP/Python 兼容、独立黄金均标"待技术核验"（design 3.3/5，test-plan 3.2 第 1 步/3.3），未声称已验证 |

### P3（不阻断，实施时对齐即可）

1. **test-plan 2.5 F3-H02 ↔ design 3.2/2.1**：H02 写"Mock 返回 TEST_REPLY"，与默认 Mock 正文 MOCK_REPLY 的约定矛盾（断言实质不受影响，两者都非 OK）。建议改为"默认 Mock 返回 MOCK_REPLY（≠OK）仍探测成功"，或明确为注入返回 TEST_REPLY 的 Mock。
2. **design 3.1 ↔ test-plan 2.2 F3-C08**："旧键出现时明确提示迁移"与 C08 预期"明确迁移错误"语义强度不一致（警告 vs 失败）。建议设计明确为"旧键存在时启动失败并给出迁移提示"。
3. **test-plan 2.5 F3-H05**：探测 timeout 固定 10 秒不可覆盖，P11 的 1 秒预算手法无法复用，超时类需挂起替身真实等待 10 秒×两适配器。可复现但慢，建议注明该变体使用挂起替身并接受约 20 秒耗时。
4. **test-plan 2.2 F3-C09**（可选）：设计 3.1"不叠加多环境 YAML"无直接负例，建议在 C09 补"并存多环境文件时仅加载 gateway.yaml"变体，或注明由配置源唯一性规则隐含覆盖。

### 结论

未发现遗漏的已确认要求、P0–P2 级契约矛盾或验收缺口；文档未冒充任何未验证事实，52 组矩阵覆盖完整。

**DOCUMENT_VERDICT: APPROVED**

（本次仅文档只读评审，未执行任何 F3 测试/实现审查；上述 P3 建议不影响进入测试方案确认。）

</details>

### 3.5 首轮实施与分层执行记录（2026-10-31历史）

实际命令、stdout/stderr 与退出码见 [evidence.json](evidence.json)。当前 Python 3.14.7，锁定依赖见 uv.lock。默认离线测试阻止实际凭据读取和外部网络连接；真实 SDK/HTTP 适配器使用本地回环协议替身，实际 HTTP 冒烟启动独立单 worker 进程。

| 阶段 | 实际结果与边界 |
| --- | --- |
| 基线 | F2 原223 passed；11份历史验收工件 SHA-256 未变 |
| Red | 精简响应/探测的2个正确断言失败；计数资源9失败；新请求约束9失败；配置79失败后扩展；响应验收24失败；协议脚本58失败。最小可导入接口后的断言失败才计为Red，早期未捕获退出码的代理记录明确为null |
| Green | 配置及Settings99通过、独立计数黄金9通过、请求约束27通过；Provider/结果/协议分层通过；中间失败原样保留，包括代理环境与HTTP头大小写的测试修正 |
| 原用例迁移 | 85个原函数全部保留，28个因Settings/Provider接口/公开字段/新增合法预算字段迁移或扩展，列表见 migration_audit；未知字段用例不再将合法 timeout_seconds 或 variables 当额外字段 |
| 完整离线回归 | `uv run python -m pytest -q` → **570 passed, 4 skipped in 45.99s**，exit 0；4跳过仅为显式启用的L01–L04 |
| Python 3.12 | 独立锁依赖环境运行关键配置、请求协议、计数、HTTP与适配器子集，**237 passed**；最初错误文件名退出4的回执保留，不冒充通过；未称整套3.12回归 |
| 计数资源 | 固定官方revision与三个资源摘要；独立脚本黄金236/358/515/17与本地计数一致；真实出站消息也与三份模板黄金逐项比对 |
| live启用负例 | 隔离子pytest显式开启但缺凭据时确实前置失败、零生成；外层验证命令exit0。该负例不等于L01–L04已运行 |
| 真实调用 | 本地credentials.yaml尚未就绪，**未验证**；不发送Key到聊天，不覆盖用户文件 |
| 实现独立审查 | 原生Claude Code第1轮已完成，exit0、is_error=false、end_turn；P0–P2为0，P3处置见3.7 |

副作用不仅检查状态码：预检拒绝检查零Provider调用与零尝试；协议错误检查服务器实际一次收件、SDK无隐藏重试；并发检查模型/参数/指纹隔离；探测使用真实10秒固定总时限；断连保留未确认状态；生命周期检查 owned 客户端关闭及注入对象归属。慢分块覆盖单段未超时但总时限耗尽。默认Mock与两真实适配器共享同一Gateway预检/验收。

模板闭环：本次暴露的是填写执行状态与迁移用例语义的具体问题，现有模板已要求当前状态、刻意契约变化、实际证据及未验证项；本次按模板补齐，不添加通用流程或额外目录。文档历史原文单独标注，不把当时的“未实施”作为当前结论。

### 3.6 52组实际测试映射

下表路径以tests/为根，多个函数共同覆盖一组变体。离线覆盖经过实际回归；L01–L04已通过；R04第2轮APPROVED，证据门槛已补齐。完整参数展开与收件/Trace断言见对应源码，不将52组称作52次测试。

| 编号 | 实际测试或证据 | 当前结论 |
| --- | --- | --- |
| F3-C01 | unit/test_f3_config.py::test_default_registry_is_explicit_mock_without_credentials | 通过（离线/本地） |
| F3-C02 | unit/test_f3_config.py::test_real_provider_and_equal_budget_boundary; protocol/test_providers.py::test_real_transport_mapping_and_success | 通过（离线/本地） |
| F3-C03 | unit/test_f3_config.py::test_yaml_invalid_or_duplicate_is_safe; unit/test_f3_config_boundaries.py::test_raw_yaml_wrong_shapes_and_duplicate_models_are_rejected | 通过（离线/本地） |
| F3-C04 | unit/test_f3_config_boundaries.py::test_registry_bad_containers_are_safe; unit/test_f3_config.py::test_model_cross_constraints | 通过（离线/本地） |
| F3-C05 | unit/test_f3_config.py::test_registry_rejects_invalid_references; unit/test_f3_config.py::test_credentials_references_and_secret_representation | 通过（离线/本地） |
| F3-C06 | unit/test_f3_config.py::test_model_numbers_are_strict_positive; unit/test_f3_config.py::test_model_cross_constraints; unit/test_f3_config.py::test_real_provider_and_equal_budget_boundary | 通过（离线/本地） |
| F3-C07 | unit/test_f3_config.py::test_registry_does_not_share_mutable_containers; unit/test_app_factory.py::test_applications_keep_independent_settings_and_traces | 通过（离线/本地） |
| F3-C08 | unit/test_settings.py::test_invalid_provider_does_not_fall_back; unit/test_settings.py::test_process_environment_overrides_file | 通过（离线/本地） |
| F3-C09 | unit/test_f3_config.py::test_yaml_missing_and_environment_files_not_selected; unit/test_app_factory.py::test_import_main_does_not_load_settings_or_call_factory; smoke/test_http_server.py::test_single_worker_real_http_server | 通过（离线/本地） |
| F3-C10 | unit/test_f3_token_budget.py::test_counter_rejects_missing_or_tampered_assets; unit/test_f3_app_resources.py::test_incompatible_model_is_rejected_before_provider_factory | 通过（离线/本地） |
| F3-C11 | unit/test_f3_app_resources.py::test_later_factory_failure_closes_first_owned_client; unit/test_f3_app_resources.py::test_one_close_failure_does_not_skip_other_owned_clients; protocol/test_providers.py::test_client_reuse_and_explicit_close | 通过（离线/本地） |
| F3-C12 | integration/test_f3_gateway.py::test_f3_probe_fixed_parameters_latency_and_usage_are_internal; unit/test_f3_app_resources.py::test_explicit_dependencies_do_not_load_env_registry_or_credentials | 通过（离线/本地） |
| F3-B01 | integration/test_f3_gateway.py::test_f3_selects_exact_provider_and_budget_defaults; unit/test_f3_contracts.py::test_omitted_request_budgets_are_resolved_by_model_not_schema | 通过（离线/本地） |
| F3-B02 | integration/test_f3_gateway.py::test_f3_rejects_bad_budget_types_before_fingerprinting; unit/test_f3_contracts.py::test_request_openapi_schema_does_not_advertise_null_budgets | 通过（离线/本地） |
| F3-B03 | integration/test_f3_gateway.py::test_f3_per_model_rejections_precede_token_count | 通过（离线/本地） |
| F3-B04 | unit/test_f3_token_budget.py::test_counter_matches_independent_official_encoding | 通过（离线/本地） |
| F3-B05 | integration/test_f3_gateway.py::test_f3_window_equal_and_one_over | 通过（离线/本地） |
| F3-B06 | integration/test_f3_gateway.py::test_f3_full_system_and_few_shots_can_exceed_window; integration/test_f2_prompts.py::test_final_user_budget_includes_template_characters | 通过（离线/本地） |
| F3-B07 | integration/test_f3_gateway.py::test_f3_count_failure_does_not_bypass_preflight | 通过（离线/本地） |
| F3-B08 | integration/test_f3_gateway.py::test_f3_per_model_rejections_precede_token_count; integration/test_f3_gateway.py::test_f3_missing_variable_precedes_unknown_model_and_count; integration/test_f2_prompts.py::test_error_precedence | 通过（离线/本地） |
| F3-B09 | integration/test_http.py::test_extra_fields_rejected_at_every_object | 通过（离线/本地） |
| F3-B10 | integration/test_f3_gateway.py::test_f3_parallel_models_and_parameters_are_isolated; integration/test_f2_prompts.py::test_environment_metadata_does_not_enter_fingerprint | 通过（离线/本地） |
| F3-P01 | integration/test_f3_gateway.py::test_f3_public_result_only_contains_answer_and_usage; integration/test_f3_gateway.py::test_f3_selects_exact_provider_and_budget_defaults | 通过（离线/本地） |
| F3-P02 | protocol/test_f3_prompt_wire.py::test_all_approved_prompt_messages_reach_real_transports_unchanged | 通过（离线/本地） |
| F3-P03 | unit/test_f3_result_boundaries.py::test_unknown_zero_and_cached_usage_do_not_invent_or_duplicate_tokens | 通过（离线/本地） |
| F3-P04 | unit/test_f3_result_boundaries.py::test_each_usage_component_is_required_and_strict_when_present; protocol/test_providers.py::test_wire_types_are_validated_before_sdk_coercion | 通过（离线/本地） |
| F3-P05 | unit/test_f3_result_boundaries.py::test_response_ambiguity_and_structural_damage_precede_completion_classification; unit/test_f3_provider_results.py::test_malformed_response_is_rejected | 通过（离线/本地） |
| F3-P06 | integration/test_f3_gateway.py::test_f3_failed_completion_keeps_only_confirmed_metadata; unit/test_f3_result_boundaries.py::test_response_ambiguity_and_structural_damage_precede_completion_classification | 通过（离线/本地） |
| F3-P07 | protocol/test_providers.py::test_completion_end_states | 通过（离线/本地） |
| F3-P08 | protocol/test_providers.py::test_http_statuses_without_hidden_retries; integration/test_f3_gateway.py::test_f3_classified_error_survives_http_boundary | 通过（离线/本地） |
| F3-P09 | protocol/test_providers.py::test_http_statuses_without_hidden_retries | 通过（离线/本地） |
| F3-P10 | protocol/test_providers.py::test_connection_failure | 通过（离线/本地） |
| F3-P11 | protocol/test_f3_gateway_probe.py::test_total_deadline_wins_over_continued_partial_reads | 通过（离线/本地） |
| F3-P12 | integration/test_http.py::test_program_error_before_call_has_no_attempt; integration/test_f2_prompts.py::test_failed_call_keeps_fingerprint | 通过（离线/本地） |
| F3-P13 | integration/test_f3_gateway.py::test_f3_parallel_models_and_parameters_are_isolated; protocol/test_providers.py::test_concurrent_call_options_are_not_client_state | 通过（离线/本地） |
| F3-P14 | integration/test_trace_integrity.py::test_interrupted_response_is_not_confirmed_success | 通过（离线/本地） |
| F3-H01 | integration/test_f3_gateway.py::test_f3_probe_fixed_parameters_latency_and_usage_are_internal | 通过（离线/本地） |
| F3-H02 | integration/test_f3_gateway.py::test_f3_probe_fixed_parameters_latency_and_usage_are_internal | 通过（离线/本地） |
| F3-H03 | integration/test_f3_gateway.py::test_f3_probe_rejects_body_before_prompt | 通过（离线/本地） |
| F3-H04 | integration/test_f3_gateway.py::test_f3_probe_obeys_model_limits | 通过（离线/本地） |
| F3-H05 | protocol/test_f3_gateway_probe.py::test_probe_real_adapters_share_gateway_error_and_trace; protocol/test_f3_gateway_probe.py::test_probe_keeps_fixed_ten_second_deadline | 通过（离线/本地） |
| F3-H06 | integration/test_f3_gateway.py::test_f3_probe_fixed_parameters_latency_and_usage_are_internal; integration/test_http.py::test_http_errors_are_enveloped_without_model_trace | 通过（离线/本地） |
| F3-H07 | smoke/test_http_server.py::test_single_worker_real_http_server | 通过（离线/本地） |
| F3-L01 | live/test_real_providers.py::test_real_provider_case；live-results.xml | 通过（真实HTTP+Provider） |
| F3-L02 | live/test_real_providers.py::test_real_provider_case；live-results.xml | 通过（真实HTTP+Provider） |
| F3-L03 | live/test_real_providers.py::test_real_provider_case；live-results.xml | 通过（真实HTTP+Provider） |
| F3-L04 | live/test_real_providers.py::test_real_provider_case；live-results.xml | 通过（真实HTTP+Provider） |
| F3-L05 | tests/conftest.py live标记默认跳过/离线访问保护；evidence.json live-missing-credentials-gate的隔离子pytest前置失败验证 | 通过（离线/本地） |
| F3-R01 | evidence.json migration_audit；原85个测试函数保留，声明的28个迁移/扩展函数单独列出 | 通过（离线/本地） |
| F3-R02 | unit/test_f3_provider_results.py::test_raw_exception_text_is_never_retained; integration/test_f2_prompts.py::test_trace_does_not_retain_rendered_data; unit/test_f3_config_boundaries.py::test_credential_shapes_and_types_are_rejected_without_echo | 通过（离线/本地） |
| F3-R03 | document-contract-verification及收尾复核回执；首轮9文档/85本地链接，补审前最新86；示例解析与历史11份摘要均通过 | 通过（本地） |
| F3-R04 | [实现审查原文](review.md)及evidence.json implementation_review；四条真实回执已归档；implementation_review_round2 | 通过，第2轮APPROVED |

### 3.7 Claude Code 实现第1轮审查与处置（2026-10-31历史）

[原文](review.md)由原生CLI实际产生，沿用既有连接与认证，仅开放Read；退出0，`is_error=false`、`stop_reason=end_turn`，最终 **CODE_VERDICT: APPROVED_OFFLINE_PENDING_LIVE**，P0/P1/P2均0、P3四项。审查未重跑测试，测试结果来自执行者回执核对。实现前的文档审查不计本项代码审查轮次。

实际工具足迹为90次Read：88次成功、2次读取大段evidence超过工具Token上限，缩小范围后继续成功；共覆盖62个不同文件，其中59个项目指定文件、2个临时差分/回执索引及原始evidence。原始evidence按范围核对，并非全文读取。审查前后52份源码/测试/配置示例的SHA-256一致；计数三资源摘要仍与固定manifest一致。实际读取参数与摘要写入evidence.json。原文声称读取references经验文件，但无相应Read，覆盖范围以实际足迹为准；该经验由执行者读取核对，不能算审查者读取。

| P3 | 核实与处置 |
| --- | --- |
| 1 计数模型失配抛ValueError | 保留。启动前安全拒绝、零Provider构造且有回归；当前无需增加异常层 |
| 2 YAML错误缺细分位置 | 保留安全文件/结构诊断；避免回显未知键或原文，细分去敏位置非本轮要求 |
| 3 未知Provider错误code兜底 | 原报告位置修正：直接下标在src/errors.py构造器，并不在api处理器。当前code均受控；未知内部code是编程错误，internal_error收口合理，不增加默许未知code的兜底 |
| 4 早期note空白 | 两条未捕获pytest退出码的协议记录已有明确note；空note的结果Red/Green有实际exit_code。保留原始回执，补充此核实解释，不更改原输出或补造退出码 |

本轮后仅同步文档状态、审查原文及核实说明，源码/测试未变，无需为P3引入范围外实现。**整项F3仍未验收**：真实凭据文件尚未由用户本地填写，L01–L04未执行。就绪后先显式运行四例并记录真实Usage/计数差异；若引起代码修改，原场景复测后进入第2轮，整项最多三轮。

### 3.8 真实接入验收（2026-11-01）

用户确认已在本机配置独立credentials文件后，显式执行已确认四例：

```bash
uv run python -m pytest tests/live --run-live -q --tb=short -o junit_family=legacy --junitxml=docs/features/F3/live-results.xml
```

实际 **4 passed in 4.43s，exit0**。普通调用使用context_answer 1.0/1.1；主、备探测均固定basic_text/16 Token/10秒。每例经本地FastAPI实际HTTP进入完整Gateway，检查成功信封、单次尝试、stop、合法实际Usage、预算、用途及计数差异。四例没有自动重试或备用切换，没有修改实际凭据、.env或默认Mock YAML。

原始命令回执在evidence.json `real-provider-acceptance`；[JUnit原始结果](live-results.xml)与evidence.json `real_provider_acceptance`保存请求ID、计数版本、实际模型/Usage和耗时，未记录Key、完整Prompt或回答正文。

| 用例 | 适配器/用途 | 本地输入 | 实际输入 | 差值 | 实际输出 | 总Token | 尝试耗时ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F3-L01-primary-completion | deepseek | 358 | 358 | 0 | 13 | 371 | 809.05 |
| F3-L02-backup-completion | openai_compatible | 515 | 515 | 0 | 13 | 528 | 739.82 |
| F3-L03-primary-probe | deepseek | 237 | 237 | 0 | 1 | 238 | 868.26 |
| F3-L04-backup-probe | openai_compatible | 237 | 237 | 0 | 1 | 238 | 871.18 |

实际模型四例均为deepseek-flash；资源revision仍为314113f8a7dadb388215396ba5fbee2c610a04d6。**四次差值为0仅证明本次输入与当前云端一致**，不推出所有输入、未来模型升级或生产环境均一致；文档最大窗口/输出能力仍不是极限实测。

用户本地凭据就绪是外部前置条件变化，源码/测试/默认配置未变。凭据存在时重新运行默认离线回归：`uv run python -m pytest -q --tb=short` → **570 passed, 4 skipped in 46.82s，exit0**；4 live仅在默认模式跳过，已显式执行通过的结果不被这次跳过覆盖。离线访问保护仍开启，未触发实际凭据读取或外部连接。第2轮Claude Code仅补审本次新增真实/回归证据、live测试及相关主链并核对首轮后源码摘要，第2轮实际结论见3.9。

### 3.9 最终独立补审与验收结论（2026-11-01）

Claude Code原生CLI第2轮仅Read，18个指定文件均成功读取；退出0、`is_error=false`、`stop_reason=end_turn`，**CODE_VERDICT: APPROVED**。未重跑测试，读取实际JUnit/命令回执、live测试和调用代码，对比首轮后52份相同源码/测试/配置摘要；完整代码结论来自首轮完整审查与本轮新增证据核验。原文见[review.md第2轮](review.md)，实际读取范围、结果、前后摘要与本轮回执已保存于evidence.json implementation_review_round2。

本轮P0/P1/P2均0。新增两项P3：R03链接数85为首轮历史、补审前86为新增引用后的核验结果，现已注明；design/test-plan等头部状态现统一验收通过。首轮四项P3维持3.7核实处置，无范围外代码修改。

原文将live写成“单worker进程”，执行者核实为**pytest进程内启动uvicorn.Server，测试经真实回环HTTP访问，再由真实适配器访问Provider**，没有用TestClient替代网络；独立单worker子进程证明来自离线smoke。静态补审没有重跑测试或独立重算SHA-256；摘要由执行者在本轮前后实际计算，52份源码/测试/默认配置一致、11份历史工件一致。

最终：**F3验收通过**。52组用例映射已闭环；本轮真实4 passed、凭据存在后的默认离线570 passed/4 skipped、两轮非阻断独立审查均具备实际回执。四次真实输入差值均0，只覆盖本次账户/网络/输入组合；1M极限窗口、通用计数一致性、生产可靠性未验证。默认配置仍为两条Mock，真实凭据由用户本地维护，不自动选择真实Provider。

模板闭环：新增证据带来的问题是当前状态/历史状态与最新核验数区分，现有模板已要求状态、证据及未验证项，本次按现有条款补齐；未证明需要增加通用流程，未修改历史F1/F2工件或扩展功能。


### 3.10 用户验收反馈

2026-11-01，用户明确反馈：“F3：我验收过了，通过”。

结论：F3用户验收通过。本条记录用户本地验收反馈；此前自动化测试和独立审查记录保留原文，不将用户反馈改写为新的测试执行。本次仅更新验收记录和进度，未重新运行模型调用或测试。
