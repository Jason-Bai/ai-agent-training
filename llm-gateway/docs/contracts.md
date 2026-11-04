# 接口与事件契约草案

F11两层并发已按用户确认的分层标准验收通过（第2轮独立复核APPROVED），原始失败及供应商成功率保留，见[测试方案](features/F11/test-plan.md)。普通/结构化、pending首次GET启动、Provider主动探测在首次执行前同时检查全局及可信Agent请求额度；满额HTTP429、`code=concurrency_limited`、`data=null`、原四字段与请求ID响应头保持，零Provider调用/Attempt/预留。pending拒绝GET不写生成failed，不改变原状态/事件/期限。重试/退避/修复/fallback持续持有原名额，不重新准入；已开始SSE不会新增本地名额不足终态。订阅/补读、取消、Trace、账务及healthz不占生成名额。Provider限流仍为provider_rate_limited并沿用F7。完整配置及释放所有权见[F11设计](features/F11/design.md)。

> 状态：F1/F1-O1/F2/F3/F4 已验收；F5及O1/O2已验收。F6设计/测试方案均已确认，主动取消已验收通过；F7有限恢复已验收，F8 诊断查询见本页专节。当前实现与F6目标协议分节说明。

2026-11-02：[F5-O1 统一持久化设计](features/F5/optimizations/O1/design.md)已实现并验收，[测试方案](features/F5/optimizations/O1/test-plan.md)保存完整回归与两轮独审记录。顺序为 O1 持久化 → O2 两阶段/续传 → F6 取消 → F7 恢复 → F8 Trace → F9 计费 → F10 总验收。当前O2已改为POST创建/GET订阅并验收通过；O1的数据库完成判定继续保留。本项未部署生产。


F3 基线实现与验收见 [设计](features/F3/design.md)与[测试方案](features/F3/test-plan.md)，离线验证已通过，Claude Code第1轮离线实现审查已通过，真实接入四例与第2轮独立审查均已通过。F3普通请求可省略 max_output_tokens/timeout_seconds，成功 data 精确 text/usage；POST /v1/models/{model}/health 无请求体、固定16/10、成功仅 latency_ms，GET /healthz 不调用。删除公开 model/actual_model/attempts 为 F3 刻意变更；下文 F1/F2 字段描述保留为历史切片，F3基线以其设计为准，F4增量见下节。
F2 的 [受控 Prompt 设计](features/F2/design.md)已验收通过，[测试、执行证据与审查处置](features/F2/test-plan.md)已归档。模板变量已支持；F4当前增量见下节，F5/F6已验收，F7恢复已验收，F8 查询契约见本页专节。

F4 完整文档已确认并获实现授权，[设计](features/F4/design.md)与[实际执行记录](features/F4/test-plan.md)对应已验收F4版本。Schema 主链及 Responses 已接入，完整十进制保真已获确认并实现，功能测试与独立审查均通过。上文 F3 为历史基线，下节为 F4 契约与已确认边界。

## HTTP 入口

| 入口 | 用途 | 约束 |
| --- | --- | --- |
| `POST /v1/llm` | 普通完整文本 / 结构化输出 | F4 接入 response_schema 与精确十进制；完整规则见下文 |
| `POST /v1/models/{model}/health` | 当前模型主动探测 | 无请求体，固定16输出Token/10秒；成功data仅latency_ms |
| `POST /v1/llm/stream` | O2：202 JSON创建pending | 独立请求不声明 `response_schema`，提供即422/invalid_request，零调用 |
| `GET /v1/llm/stream/{request_id}/events` | O2：首次订阅触发执行，之后按游标补读 | after_seq可选默认0；200 SSE，终态且无未读为204 |
| `GET /healthz` | 存活检查 | 不调用模型，不等同供应商可用性检查 |
| `GET /v1/traces/{request_id}` | F8 按请求定位元数据 | 仅供本机或隔离开发网络使用，不返回敏感正文 |

调用方式由路径决定，请求体不设重复的 `stream` 布尔值。F6取消接口设计已确认、已验收通过，目标协议见下节。首版不提供模型管理、限流或 Prompt 管理接口。

### F6 已确认的取消协议（已验收通过）

[完整设计](features/F6/design.md)已确认，[测试方案](features/F6/test-plan.md)已确认，已验收通过。`POST /v1/llm/stream/{request_id}/cancel` 无请求体/查询参数，接受有效流式请求的取消并用于后续轮询。pending/running均先提交canceling；Gateway独立停止、清理并保存终态，不依赖调用方继续轮询或保持SSE连接。

处理中返回202，终态返回200；统一四字段JSON，data仅含原request_id和status，X-Request-ID亦为原ID。status只返回canceling或三类终态canceled/failed/completed；success=true表示状态接口成功，不代表原生成成功。重复POST不重复清理或追加事件，已有终态原样返回。输入非法422/invalid_request，无有效流式资源404/not_found；存储不可用/提交结果未知503/storage_unavailable，不把接口错误当业务终态。停止/清理失败保存failed/cancel_cleanup_failed；详细规则以F6设计为准。

取消接受以后禁止新delta提交或普通终态覆盖；之前已提交的历史事件仍可发送/重放。取消事件与状态原子保存、共用连续seq。取消只保证本地执行停止及清理，不证明供应商停止计算或计费；不新增取消轮询对应的模型Trace/Attempt。

## 请求和响应

目标请求契约中，普通/流式请求共享逻辑模型名、消息、**必填**的 Prompt 模板选择（名称、版本）及调用超时；模板变量随 F2 加入。普通请求后续可提供 `response_schema`；流式请求不能提供。调用方消息只允许 `user`、`assistant`，System 规则由受控模板装配。未声明字段、未知模型、模板版本/变量错误及能力不匹配必须在 Provider 调用前拒绝。F1 只交付下段所述的普通调用子集。

F1 只实现普通文本调用的最小入口：请求选择一个固定名称/版本的受控 System 模板，`model=general-primary` 是稳定逻辑名，F1 由 MockProvider 实现；`messages` 恰好是一条非空 `user` 消息。F1 不接受 `variables`、`timeout_seconds`、`response_schema` 或 `stream` 字段。非法输入、未知模型/模板在 Provider 调用前拒绝且调用数为 0。F2 已实现变量与多版本，F3 接入真实模型及超时控制；F1 的字段、错误和 Trace 已按[设计方案](features/F1/design.md)实现并通过本地验收。

F2 当前增量：`prompt.variables` 为可省略的字符串字典，message 由唯一一条 user 原文绑定且禁止调用方覆盖；运行时变量只渲染进末条 User，静态 Few-shot 的 assistant 只由模板库提供。F2 不开放调用方多轮历史消息。固定 System/示例/变量契约按准确版本选择；最终 User 最多 20,000 字符，context_answer 的 context 最多 10,000。变量相关错误、先后顺序及指纹定义以 [F2 设计](features/F2/design.md)为准；成功响应不增加字段，指纹只在请求级 Trace。

非流式 API 的 JSON 响应使用四个顶层字段：`success: boolean`、`code: string`、`data`、`message: string`。成功为 2xx、`success=true`，F1 的成功 `code=ok`，`data` 使用明确的普通调用结果类型，包含请求逻辑模型、实际使用的供应商模型（未知可为 `null`）、原始文本、尝试次数及可取得的 Usage；后续功能再讨论如何加入耗时和可选的已校验 `parsed`。F1 的 Mock 响应中 `actual_model=null`、Usage 未取得为 `null`，不能把 Mock 名称当作真实供应商模型。没有请求 Schema 时不要求 F1 返回 `parsed`；有 Schema 而解析或校验失败时，不返回成功 `parsed`。通用 Schema 合法不代表订单事实或业务规则正确，业务 Agent 仍要复验。

非流式 API 的错误使用真实 4xx/5xx 状态及同一四字段 JSON 外形：`success=false`、稳定错误 `code`、`data=null`、安全 `message`。Gateway 生成的 `request_id` 只在 `X-Request-ID` 响应头，不进入 JSON 正文；应用层统一处理请求校验、已知 Gateway/Provider 错误、未预期错误及 404/405。F1 已确认错误为 `422/invalid_request`、`400/unknown_prompt_template`、`400/unknown_model`、`502/provider_error`、`500/internal_error`、`404/not_found` 和 `405/method_not_allowed`；只有命中 `POST /v1/llm` 的请求建立模型调用 Trace，包括非法 JSON 与路由前字段校验失败，404/405 只统一错误外形。受控 Provider 调用故障与未预期程序错误分开。后续错误类别包括能力、供应商连接/超时/限流/认证、JSON 语法、Schema、取消和调用不可用；认证、配置、模板和 Schema 错误不进入网络重试。F1 错误契约已按整体设计实现并本地验证；后续能力仍按各功能逐项验收。

`request_id` 不接受调用方指定；应用在请求校验前生成并在响应头返回。Agent 的任务级 `trace_id`、Gateway `request_id`、供应商响应 ID 和订单等业务 ID 各有粒度，不互相替代；任务级 ID 的可信传递方式留待接入 Agent 时确定。

## F4 结构化输出增量（已验收）

`POST /v1/llm` 新增可选 response_schema；省略时保持 text/usage，显式 null 或非对象为 422 invalid_request。结构化请求使用支持能力的模板与模型，首个模板为 context_answer@1.2.0；根必须显式 type=object，可以嵌套。完整关键字、引用、资源限制、规范化、示例与错误规则见 [response_schema 完整规则](response-schema.md)，不能仅按“任意 JSON 对象”理解此字段。入站Schema、规范化、计数、Provider出站和parsed均保留精确十进制JSON数字，不转为float或数字字符串。

F4 成功 data 精确为 text/parsed/usage：text 保留上游回答，parsed 为严格解析并通过 Schema 验收的对象。普通文本不增加 parsed:null；四字段信封和仅响应头 request_id 不变。

新增调用前 400 invalid_response_schema / unsupported_capability，以及调用后 502 invalid_json / schema_validation_failed / provider_refused。截断先报 output_truncated；其他上游错误沿用 F3。调用前错误零 Provider/零 Attempt，调用后失败一次 failed Attempt；错误 data=null，不返回半成品，不做修复、重试或 fallback。业务事实和权限仍由 Agent/业务程序检查。

调用方只选逻辑模型，不传协议或原始生成约束参数。Provider 配置决定 Chat/Responses，Gateway 装配受控输出协议。F4 不新增 Schema 能力查询接口，运行时发现留 1.7；现有接口文档将在实现阶段引用同一份完整规则。

## 原 F5 Streaming 基线（历史验收，当前变化见O2）

[F5设计](features/F5/design.md)与[测试方案](features/F5/test-plan.md)收敛本次分段讨论。请求仅model/prompt/单条user消息及可选输出/超时预算；额外字段一律422。显式模型supports_streaming严格布尔，false调用前400/unsupported_capability。沿用Prompt/字符/Token预检，通过后B起点设置固定总期限，先建立SSE再调用Provider。总期限覆盖发送等待；15秒空闲心跳不重置deadline。Provider读超时为provider_timeout，总deadline为request_timeout。

Provider统一事件包含delta、running/completed/failed、终态Usage与平台error_code。Gateway不依原始协议分支；空格/换行保留，仅空白回答不能成功。一次调用、不重试/fallback。F5即实现断连清理，F6再做主动取消。未知Usage/时间点为null，TTFT是首段成功写出，Generation是上游首段到达至最终结果到达，二者不混算；仅存内部内存Trace。

## SSE 事件族

所有对外流式事件均以 `response.` 开头。SSE 的 `event` 字段和 JSON `data` 保持同一事件语义，不把供应商原始事件名直接透出。

| `event` | 状态 | `data` 字段 | 说明 |
| --- | --- | --- | --- |
| `response.delta` | 运行中 | `request_id`、非空 `delta` | 新增文本，不把完整答案切块伪装成流式 |
| `response.heartbeat` | 运行中 | `request_id` | F5：空闲15秒；不计回答/TTFT/Usage |
| `response.completed` | 终态 | `request_id`、`usage`（未知null） | F5：明确上游正常完成且累计含非空白；不公开实际模型/尝试次数 |
| `response.failed` | 终态 | `request_id`、`code`、安全`message` | F5：流开始后的故障，不暴露密钥/堆栈 |
| `response.canceling` | 过渡 | `request_id` | F6已验收通过：与接受取消状态原子保存，供订阅及续传读取 |
| `response.canceled` | 终态 | `request_id` | F6已验收通过：本地停止/清理完成并与终态原子保存，供订阅及续传读取 |

同一请求的 `completed`、`failed`、`canceled` 互斥；`canceling` 不是终态。内容已发出后不静默换模型并拼接新回答；F6取消接受后不再提交新内容、重试或fallback，取消前已提交内容仍可发送/重放。客户端断连时可能收不到终态；客户端未见 `response.completed` 不可认定成功。

请求在SSE启动前失败时返回四字段JSON HTTP错误；生成中的故障由生成所有者保存response.failed，订阅只回放。订阅自身查询/发送故障只结束该连接，不能伪装成模型response.failed或改为JSON。`request_id` 应可由响应头取得，并出现在每个 SSE 事件中。Provider 内部需把文本增量、用量、完成和失败归一化为类型化结果，Gateway 再映射到此事件族。

当前O2业务事件以数据库seq作为SSE id，data不重复seq；心跳无id。按after_seq补读同一请求，旧游标可重投，调用方去重；不提供客户端自动重连或started事件。生成成功以唯一完成事务提交为准，订阅断连不改变它；Middleware不凭HTTP200/响应结束收口。

## F5-O1 持久化基础与当前 O2 两阶段契约

O2[设计](features/F5/optimizations/O2/design.md)与[测试方案](features/F5/optimizations/O2/test-plan.md)均已确认，已实现并验收。以下为当前202/GET/seq订阅规则。after_seq省略按0处理，断点续传显式提交最后成功处理的seq；采用应用层100ms空轮询、单页100条；实际验收证据见本项测试方案。

历史O1保留原POST请求/响应外形和事件data，不对外增加seq；新增 PostgreSQL Request/Attempt 及流式事件持久化。生成完成事件、状态和已知 Usage 提交后，即使发送失败，请求仍 completed；O1 中生成尚未结束时断连仍停止上游并按失败收口。心跳不保存，业务事件不清理。普通 /v1/llm 不增加正文 request_id、actual_model、attempts 或结果回放。

O2 将 POST /v1/llm/stream 改为创建接口，成功 HTTP 202、四字段 JSON、data仅含request_id/status=pending，响应头同时保留 ID；这是新创建协议对旧“仅响应头 ID”的明确调整。POST 保存 pending，不调用模型；首次有效 GET /v1/llm/stream/{request_id}/events 原子启动生成并 SSE 订阅。之后 GET 仅补读和订阅，不重调模型。同请求多个订阅独立维护游标，断连不停止生成。

O2的after_seq默认0，只取更大seq；空值/负数/非十进制/重复或未知查询字段及非法UUID为422/invalid_request，Last-Event-ID不隐式替代。无有效流式快照（包括普通/入口拒绝记录）404/not_found，超过当前最大序号400/invalid_event_cursor；错误不启动生成。已读到终态后 204 只表示没有未读事件，不作为生成成功证据。历史事件按原序号回放，心跳不推进 seq；未获得首次创建 ID 的重复 POST 不是续传。

O2 生成总期限从首次有效 GET 触发开始、包含上游和保存，重连不重置；订阅不设整体总超时，15 秒空闲心跳、单次发送最多 30 秒。订阅传输故障不产生表示生成失败的 response.failed。pending/running/canceling 为非终态，completed/failed/canceled 互斥终态。F6已实现并验收：pending/running取消均经canceling，完成与取消先提交者生效；证据见F6测试方案。

O1 的 storage_unavailable/503 按调用发生阶段解释，不能以 503 推断零上游调用。数据库提交无法确认时核对已存在终态/事件，而不是直接覆盖失败；入口拒绝只保留已知元信息，不保存未验收原始 body。精确本项失败出口见 O1 设计；正式测试方案已确认，实际回执与独审状态在本项记录。

## Trace 契约

Trace 分两层：一条 Gateway 请求摘要，以及每次真实 Provider 调用对应的一条尝试记录。入口拒绝有请求记录但无尝试记录，`attempts=0`；主模型两次超时、备用成功一次则是三条尝试，最终实际模型是备用。

请求摘要至少记录 `request_id`、请求/实际模型、Prompt 名称版本、开始与结束时间、终态、尝试次数、最终错误码、可取得的 Usage；成本计算留 F9，当前没有计费能力。尝试记录关联请求、序号、Provider/模型、耗时与结果。原F5仅内存元数据；当前O1/O2已增加有效执行输入快照及流式业务事件正文，但不增加普通回答结果缓存。Usage 或价格信息不足时记未知，不记实测 0。

F1 至原 F5 使用内存记录；O1 已以 PostgreSQL 替代原 SQLite 安排：tb_llm_request 使用 BIGINT 内部主键和非空唯一 UUID request_id，tb_llm_attempt 与 tb_llm_stream_event 通过 request_pk 关联，各自以 (request_pk, attempt_no) 和 (request_pk, seq) 为联合主键。O1 仅普通/流式落库，主动探测及公开 Trace 查询留 F8。数据库启动检查失败不接流量，不降级为内存；Gateway 重启后 running/canceling 均 failed，pending 保持，终态不变；不自动恢复上游生成。数据库共享不等于具备 1.7 多实例协调。

## F7 有限恢复增量（已验收）

完整规则见[已确认设计](features/F7/design.md)，逐例验收见[测试方案](features/F7/test-plan.md)。本节更新F4/F5历史“一次调用/不修复”的当前目标，不改写原历史验收。

普通/stream请求新增可选严格正整数initial_output_tokens；省略=有效max_output_tokens，显式null/bool/字符串/小数拒绝。initial大于max为400/invalid_output_budget，零调用/零Attempt。max省略仍取主模型默认，按输入+完整max+margin预检，不裁剪。截断先于JSON验收，在授权内同候选完整重答，不能拼接；格式修复只普通Schema路径至多一次，修复后任意失败不再衍生恢复。

恢复上限由服务配置：总3次/每候选2次，不能由调用方扩大。仅允许明确瞬时错误；认证/额度/参数/拒绝/未知故障不fallback。Retry-After不被本地退避上限缩短，同origin+credential_ref不能以换别名/协议绕过等待。初始确定性错误不会被备用纠正。

整次timeout_seconds含持久化、尝试、校验、等待；单次min(候选max_timeout_seconds, 剩余时间)。总期限耗尽request_timeout，模型单次先耗尽provider_timeout；无统一单次10秒。期限到达后只允许原有有界清理/延迟保存，不再调用模型。

所有SSE事件、seq、接口及取消轮询外形不变。首条非空delta提交PG后禁止任何恢复，与订阅者收包无关；中间Attempt失败不提交response.failed，最终才唯一终态。取消接受/准入共用F6屏障。重连不启动第二执行链，不重建计划。

普通成功text/usage/(parsed)、流式completed的request_id/usage不变；不公开actual_model/attempts/协议。Usage聚合所有尝试，任一未知则整体null；不伪造零用量或等价费用。Attempt准确输入快照在受控PG保存，内存Trace及公开诊断保留元信息；无凭据/异常原文/堆栈。探测不加入恢复策略。

2026-11-02最新验收：最终版本完整开启真实调用，1776项全部通过，0跳过/0未选中；16个真实用例和原297项均实际执行，见[F7验收](features/F7/test-plan.md#39-用户修订验收标准最终版本全部重跑并启用真实provider)。

## F8 按请求查询诊断信息

`GET /v1/traces/{request_id}` 查询一个 Gateway 生成的 UUID。200 表示记录可读，原操作是否成功看 `data.status`。响应沿用 success、code、data、message。ID 格式非法为 422/invalid_request，未登记为 404/not_found，数据库读取失败为 503/storage_unavailable。本接口用于本机或隔离开发网络。

| 字段 | 解释 |
| --- | --- |
| request_id、operation、status、error_code、error_stage | 标识普通生成、流式生成或主动探测的一次请求及其处理结果。未观测的故障阶段为 null。 |
| requested_model、模板/Schema 指纹、预算 | 模型选择、输入版本和校验上下文。只提供元数据。 |
| attempts | 按 attempt_no 递增。model 和 configured_model 是本次选择及配置，actual_model 是上游实际回报。provider_entered 有已持久化的肯定证据时为 true，缺少证据时为 null，不能据此确认供应商收费。 |
| timing | request_duration_ms 从创建到终态，含 pending 等待。ttft_ms 从流启动到 Gateway 首次成功发送文本；generation_duration_ms 从观察到首段上游文本到收到上游终态；stream_duration_ms 使用服务端流起止点。它们均以毫秒计。 |
| attempts[].latency_ms、provider_duration_ms | 前者是 Attempt 整段用时，后者是 Gateway 观察到的上游调用区间，含流式两次读取间的本地处理。缺少端点时为 null。 |
| recovery | 实际观察的累计等待毫秒数、预检跳过的候选及原因、停止恢复原因。跳过的候选不占 Attempt 编号。 |
| cancellation、delivery_error_code | 取消已接受的时间、结束时间和清理错误。投递或提交确认故障与请求终态分别呈现。 |
| usage | known_subtotal 对所有已回报的 Attempt 分别汇总输入、输出、总 Token；没有任何回报时为 null。unknown_attempts 给出仍未知的编号，单次上游计数须为非负整数、总和相符且可由 SQL BIGINT 表示。跨尝试小计用精确整数累加，不受单次计数的上界约束。模型标识缺失不会抹掉合法实报。 |

主动探测先持久化登记再调用指定模型，结果存储失败也不重新调用。诊断读取使用 PostgreSQL 一致视图。原始消息、变量、Schema、结果正文、事件正文与凭据不在响应字段内。

## F9 Agent 身份和账务接口

每个 /v1/ 请求携带 Authorization Bearer 凭据。缺失或无效为 401/unauthorized，其他账户的请求与不存在的请求同为 404。旧版无归属记录仅供本机管理。价格不可用为 503/price_unavailable，余额不足为 402/insufficient_budget。已开始的 SSE 通过 response.failed 返回错误码。拒绝预留时没有上游调用。/healthz 保持公开，模型探测按调用方归属计价。

- GET /v1/billing/summary 返回 currency、deposited、charged、reserved、pending、pending_attempts、available 和 usage。pending 已含在 reserved 中。usage.real 和 usage.mock 各自报告 attempts、unknown_attempts、known_subtotal。真实用量小计只纳入具备终态实报证据的计数，中间帧计数继续列为未知。
- GET /v1/billing/requests 接受 from、to（带时区的创建时间，左闭右开）、limit（1 至 100，默认 50）及上页 next_cursor。按创建时间和唯一 ID 倒序返回 items 和 next_cursor，每个请求只有一行。游标按原值做 URL 编码传递。
- GET /v1/billing/requests/{request_id} 包含同一请求的金额合计与 attempts。每次尝试可见状态、模型、来源、Token 与缓存分类、价格快照、当初预留的 reserved_amount、已确认的 agent_charge、supplier_cost 和账务处置类别。usage_complete 表示最终用量是否已确认，独立于费用是否可以结算。账务金额是人民币十进制字符串，未知为 null。请求级 charged 是已知小计，需要同时查看 reserved/pending 和 billing_status。

同一次流的创建、回放、取消、查询均不会额外启动付费尝试。平台承担的未核实费用可由管理员后续补记供应商成本，Agent 端已解除的预留不会重新扣款。核对真实账单时按价格配置中的供应商计费账户和账期匹配；同一操作号重复提交不重复调账。


## F12模型组及路由增量（验收通过）

完整[设计](features/F12/design.md)和[测试方案](features/F12/test-plan.md)已分别确认，最终2508项完整回归通过，验收通过。此节覆盖历史直接模型绑定/主选默认预算的当前增量，不改写原阶段验收。

- POST /v1/llm及POST /v1/llm/stream保留必填严格字符串model，精确选择受控模型组；不接受端点/Provider/能力清单参数，也没有省略model后的默认组。其他请求字段、响应四字段、ID位置及SSE事件不变。
- YAML为providers/endpoints/model_groups三层；端点声明当前适配器实际可交付能力、接入输出上限和厂商物理限制，组声明默认/最大授权输出和总超时。有序candidates是唯一排序依据，不嵌套组。旧models/fallback_models及max_attempts_per_model必须迁移，恢复策略改max_attempts_per_endpoint（默认总3/每端点2）。修改重启生效，不增加管理/模型列表接口。
- 普通/流式均按启用→能力→输出→专属User渲染→上下文筛选，不降额或裁剪。所有停用为503/no_available_endpoint；其余阶段耗尽分别400/unsupported_capability、invalid_output_budget、prompt_too_long、context_length_exceeded。请求本身错误仍沿用原code；所有本地预检拒绝零Provider调用/Attempt/费用预留，路由阶段错误的error_stage为routing。
- 流式POST冻结计划及相关配置，首次GET执行。相关组/端点/Provider引用和连接配置、Counter版本或恢复策略变化，或旧pending无法证明等价时，唯一failed/response.failed并记execution_configuration_changed，不静默重规划；无关组变化不影响原请求，历史事件补读无需当前模型配置。
- GET /v1/traces/{request_id}新增可空routing元数据：version、strategy、config_fingerprint、有序candidates及其status/code、selected_endpoint、reason。selected_endpoint始终是初选，实际执行查看attempts；请求requested_model为组，F12的attempts[].model为端点ID。旧记录routing=null、旧model值不重写。指纹只标识相关受控配置，不保证回答质量或远端模型版本。
- 普通响应仍不增加actual_model/attempts等内部信息。公开Trace不展示地址、凭据引用、密钥、Prompt/Schema正文；既有所有权检查不变。每次真实尝试按实际Provider/model取价，未执行备用不预扣，未知费用保持待核实。
- POST /v1/models/{model}/health的model为组，只尝试首个启用端点一次；探测预算不适配直接拒绝，不换到备用；不因探测结果自动启停。trace strategy为probe_first_enabled，结果只证明一次探测。/healthz零上游调用。

各字段、错误优先级和完整示例集中维护在F12设计；Mock、受控双协议、真实Provider及全量/独审证据以F12测试方案为准。任务自动选型归F13，未在F12实现。

## F13自动入口增量（已验收）

`model`仍必填，既可精确选择普通组，也可选择配置的`auto-general`。对外不新增Chat Completions/Responses兼容路径，不接受调用方的provider、protocol、capabilities、system或classifier字段。普通/结构化与流式两阶段接口外形保持；流式仍不接受response_schema。

- 自动入口自身默认生成输出1024、最多4096，总期限默认30秒、最多120秒。分类另外固定最多128输出/5秒/一次，受原总期限约束；生成恢复仍最多三次、单端点两次，所有Attempt编号按原请求连续。
- 零合法组400/no_available_group；唯一组直接执行；分类可恢复失败只使用仍合法的默认组，否则503/auto_route_failed。认证、额度、账务、存储、原总超时与取消不降级绕过。各层具体错误见[F13设计](features/F13/design.md)。
- 成功data仍仅text/usage及可选parsed；usage包含分类与生成实际消耗，任一未知则不伪造完整总数。Trace新增auto_routing（候选原因、resolved_group、source、fallback_reason、分类模板及耗时）、input_token_kind和每Attempt的purpose。历史缺用途投影generation，缺计数来源保持null。
- 原取消、心跳、互斥终态和after_seq规则覆盖两个阶段；分类结果不会写入response.delta。重连不重新分类；相关pending配置/Prompt/Counter变化按execution_configuration_changed失败，无关组变化不误伤。
- Gateway保留数值精度；真实百炼如将高精度数值Schema舍入而返回不符值，仍为schema_validation_failed并照实记录消耗。逐位字段由调用方声明字符串，Gateway不擅改Schema。报价按公开版本预留，实际按Usage输入档位/缓存数结算，余额可因补差成欠额，下一付费尝试继续检查余额。

F13已按用户确认的分层标准验收；分版本回归、原容量失败和补充独审结论见[测试记录](features/F13/test-plan.md)。
