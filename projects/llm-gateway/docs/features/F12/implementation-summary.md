# F12：模型管理与模型组路由——代码实现导读

> 状态：验收通过，最终完整真实回归及第2轮独立复核均通过；日期：2026-11-03。[完整设计](design.md)、[测试方案及实际批次](test-plan.md)、[实施步骤](implementation-plan.md)。本文件解释已落地代码，不将开发批次当作最终验收。

## 1. 问题与行为变化

以前`models.general-primary`同时包含实际模型接入、默认预算和一个备用模型引用。主选必须先通过能力/上下文检查，普通与流式各自组织备用；无法清晰表示“一组端点中，首选不适合本次请求，但第二个适合”。

现在调用方仍传必填`model=general-primary`，Registry解析成模型组；Router按启停、实际请求能力、输出上限、逐端点Prompt和上下文预算筛选，再按列表顺序选择。初选不适合可以直接选择后面的合法端点，不先调用一次错误请求；暂时故障发生后的重试/切换仍由F7负责。

本次不做任务语义分类，也不新增外部Provider协议接口、模型列表或管理后台。当前运行gateway.yaml已迁移结构，仍保留原来两条Mock绑定；数据库、价格、凭据和.env文件未改变。实际供应商两条协议仍共用DeepSeek，不声称独立供应商容灾。

## 2. 核心调用链与组件

```text
POST /v1/llm
  → GatewayService._complete
  → Router.route：请求及组策略校验 → 分阶段筛选 → RoutePlan
  → 原F11名额 → 原F7尝试循环
  → Store.begin_attempt：按实际端点取价/预留
  → Adapter一次调用 → 原JSON/Schema验收 → Attempt结算及请求终态

POST /v1/llm/stream
  → 同一Router.route → RoutePlan.snapshot → PostgreSQL pending（零上游）
GET /v1/llm/stream/{id}/events
  → StreamingManager认领/原F11名额 → _restore核对相关配置
  → 原StreamingExecution/F7消费有序候选 → 事件先存后发
  → 重连只读历史和新事件，不重新路由

GET /v1/traces/{id}
  → TraceQuery一致性只读投影
  → routing解释初选/排除原因，attempts解释实际执行及切换
```

Router没有网络、费用、名额或任务所有权。Gateway只编排；生命周期、关闭、取消和事务继续由已有执行器/Manager/Store负责，避免再建一套恢复或计费组件。

## 3. 建议阅读顺序

| 顺序 | 文件及类/函数 | 读完应能回答的问题 |
| --- | --- | --- |
| 1 | [安全配置示例](../../../config/gateway.example.yaml) | 一个公开组怎样关联多个端点，端点又怎样关联Provider？ |
| 2 | [models.py](../../../src/services/models.py)：EndpointConfig、ModelGroupConfig、ModelRegistry、load_registry | 哪些值属于组策略、哪些属于接入限制？旧格式为什么在启动拒绝？ |
| 3 | [routing.py](../../../src/services/routing.py)：Router.route | 能力从哪里来、分阶段怎样排除、错误优先级怎样保持稳定？ |
| 4 | 同文件RouteCandidate、RoutePlan、configuration_snapshot | 每个候选为什么有自己的消息和Token计数？快照及配置摘要覆盖什么？ |
| 5 | [gateway.py](../../../src/services/gateway.py)：prepare_stream、_complete、_ordinary_attempts | 两类入口怎样共用计划？F7为什么仍是唯一恢复入口？ |
| 6 | [streaming_manager.py](../../../src/services/streaming_manager.py)：subscribe、_restore、_cancel_operation | 第一次GET怎样核对，为什么重连和取消不会重新选型？ |
| 7 | [streaming.py](../../../src/services/streaming.py)、[recovery.py](../../../src/services/recovery.py) | 怎样按端点计次、遵守提交/取消边界、保留原请求名额与总期限？ |
| 8 | [store.py](../../../src/persistence/store.py)：timing、begin_attempt；[trace_query.py](../../../src/persistence/trace_query.py)、[trace_contracts.py](../../../src/trace_contracts.py) | 路由解释如何持久化，备用怎样按自己的价格结算，旧Trace如何兼容？ |
| 9 | [F12测试方案](test-plan.md)及新增test_f12文件 | 如何证明实际调用、消息、费用及SQL与路由说明一致，而非只校验返回文字？ |

`main.py`与`providers/factory.py`只把依赖校验/构造对象从旧models改为endpoints；Provider协议解析不因本项被重写。`ModelConfig`保留为EndpointConfig类型别名，仅服务既有Adapter注解，不是旧YAML或旧组合字段兼容层。

## 4. 关键设计与取舍

- **默认值在组中**：重排/停用首选不改变默认输出和总超时。端点接入上限和厂商物理上限都保留，避免共享端点时绕过原限制；组允许大预算走另一个能承接的候选。
- **只有一套顺序**：candidates列表即优先顺序，不再保留fallback_models、优先级数字或嵌套模型组。每请求总次数不因换名重置，恢复配置改用max_attempts_per_endpoint。
- **先规划再执行**：每候选专属消息、生成约束和Token计数在本地确定；授权最大输出全部预留，不能用较小initial掩盖超预算。筛选不收费，真正尝试才按实际Provider/model报价。
- **快照不重规划**：流式快照包含版本、相关组/全部候选安全配置、Counter版本、恢复策略及逐候选消息。相关变化或旧pending无法证明等价就明确失败；无关组变化、映射键顺序变化不影响原请求。
- **精确小数仍保留**：调试发现默认退避0.1持久化后是Decimal，直接比较当前float会误报配置变化。恢复时仅把当前受控配置经同一精确JSON编解码后比较，不将Schema或金额转成float。
- **首轮独审修复**：探测固定10秒不能被端点上限截短后继续调用；共享文本渲染超限必须等到约定渲染阶段报告。合法文本仍缓存并保留已知指纹，超限不抢在启停/能力/输出阶段之前报错；普通与流式HTTP/PG均补零副作用断言。
- **诊断保留已知事实**：成功请求的顶层指纹/计数描述初选，各Attempt描述自身。没有合法候选时仍保留首个计数候选已确认的信息，selected_endpoint为null、Attempt为零；已知计数不代表发生了调用。
- **历史测试迁移在测试侧**：`tests/registry_helpers.py`仅帮助旧夹具拆分组合输入，返回真正生产Registry；生产解析器不接受旧格式。定义本身的校验测试直接测试生产Endpoint/Group，避免拿复制的夹具校验逻辑冒充产品验证。

## 5. 成功、拒绝与失败定位

使用basic_text、model=general-primary请求时，先看响应头request_id，再查原Trace：

| 观察 | 定位入口 |
| --- | --- |
| 不知道为什么选备用 | routing.candidates查看排除code，selected_endpoint查看初选；若初选已执行后切换，按attempt_no查看reason/endpoint/错误 |
| 503/no_available_endpoint，零Attempt | 组内全部停用，检查Registry的enabled；不是供应商503 |
| unsupported_capability / invalid_output_budget / prompt_too_long / context_length_exceeded | 先查组授权，再看routing候选原因；对照端点专属消息、Counter及授权最大输出，不靠裁剪绕过 |
| 首次GET得到execution_configuration_changed | 对照创建快照与相关配置摘要；旧pending或真实关联变化需新建请求，不通过重连换模型 |
| 修复/重试次数或超时异常 | 看F7 state计数、Attempt.reason、wait_ms和原deadline；不要在Router/Adapter另加重试 |
| 切换后费用不对 | 按Attempt.provider/configured_model、报价版本、实际Usage、预留及结算核对，不能套初选价格；未知仍待核实 |
| 断连后重复生成、取消后仍切换或名额不归零 | 检查StreamingManager唯一认领、原取消/清理所有权及Limiter；路由不持有订阅或上游任务 |

普通/结构化调用的成功体仍仅text/usage及可选parsed，不增加内部端点或尝试次数。公开Trace不返回地址、凭据引用、秘密或正文；内部有效输入快照与对外诊断是不同边界。

## 6. 验证与边界

开发中已取得停用主选、三层解析和精确小数快照的实际Red及Green；配置、路由、恢复、协议、PG快照、差异价格和OS进程重启分别留证。完整数量、批次重叠、迁移失败及修正原因在[测试方案](test-plan.md)记录，不累加开发批次冒充最终全量。

首轮完整2497通过后，独审提出两项P2并已修复。修复后完整--run-live再次执行：2508全部通过，含42个真实节点，原2265全保留、新增243；20并发600秒及合计16784个真实容量请求全部完成，最终名额/任务归零，账务核对通过。[第2轮独立复核](review-r2.md)APPROVED、P0—P3均为0，首轮两项P2已闭环。真实输出质量/供应商成功率与Gateway正确性分开报告，Mock不证明真实表现，回环503不称为供应商故障。F13将复用已验证的组路由，任务分类、分类费用及失败策略届时独立讨论。
