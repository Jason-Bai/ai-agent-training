# F8 代码实现导读

从同一请求的两端阅读最直观。先发起一次主动模型探测，从响应头取 request_id；然后调用 `GET /v1/traces/{request_id}`。普通生成和两阶段流式生成共用这个诊断入口。[设计](design.md) · [测试方案与验收](test-plan.md)

| 阅读顺序 | 入口 | 关注点 |
| --- | --- | --- |
| 1 | [trace_by_id](../../../src/api/routes.py)、[TraceData](../../../src/trace_contracts.py) | 从 UUID 定位一次请求。错误码描述查询本身，data.status 描述被查请求。读模型按字段列出允许返回的数据。 |
| 2 | [read_trace](../../../src/persistence/trace_query.py) | PostgreSQL 只读 REPEATABLE READ 事务提供一致快照，按 attempt_no 读取尝试，只读取取消事件的时间。历史候选跳过原因曾存于执行快照中，只有缺少新元数据时才读取、解码并挑选指定字段。 |
| 3 | [GatewayService.probe 和普通执行入口](../../../src/services/gateway.py)、[请求中间件](../../../src/middleware/request_context.py) | 主动探测复用普通调用的登记与结果保存路径，使用固定的单次尝试策略。入口拒绝与登记失败发生在 Provider 之前，提交结果不确定时由状态所有者核对。 |
| 4 | [RequestTrace、AttemptTrace](../../../src/services/trace.py)、[StreamingExecution](../../../src/services/streaming.py) | 退避等待记录实际经过的时间；Provider 耗时从本地发起调用开始。流式收到终态后，即使终态同时带最后一段文本，后续写库与发送也不计入本次 Provider 耗时。 |
| 5 | [Store](../../../src/persistence/store.py)、[flush_pending](../../../src/main.py)、[f8_001](../../../migrations/versions/f8_001.py) | 请求、尝试和业务事件沿用原三表。迁移扩展探测操作类型，新增可空的 provider_duration_ms。探测只落元数据，恢复路径核对已有终态并区分投递失败。 |
| 6 | [HTTP 与状态交错](../../../tests/integration/test_f8_lifecycle.py)、[一致性查询](../../../tests/integration/test_f8_trace_query.py)、[探测写入故障](../../../tests/integration/test_f8_probe.py) | 从 HTTP ID 出发，用独立 SQL 和 Provider 调用计数验证，而不依赖内存 Trace。 |

## 出现问题时怎么查

- GET 返回 404 时先核对 UUID。若原探测返回 503，登记未提交也可能查不到记录。用同一 request_id 查日志中的 stage 和 attempt_no，确认故障发生在登记还是结果保存。
- GET 返回 200 时先看原请求的 status、error_code 和 error_stage，再看每次尝试的 reason 和 error_code。主失败备成功时，原请求为 completed，第一条 Attempt 的失败原因仍在。
- 先看 usage.unknown_attempts，再解释 known_subtotal。输入预估和上游实报是不同来源；没有实报时小计为 null。一条进行中的尝试也可能尚未回报。
- 流请求可以在订阅前保持 pending。终态事务先落库，投递观测可能稍后补记。TTFT 衡量 Gateway 的发送观察点，无法推断客户端何时实际收到。在决策时已跳过的备用模型单列在 recovery 下，不占 Attempt 编号。

## 验证入口

[F8 用例清单及结果](test-plan.md)区分故障注入、真实 PostgreSQL、实际 Gateway 进程和两种真实接入协议的证据。部署范围为本机或隔离开发网络内的单实例、单 worker。费用核算属于 F9。

探测的 10 秒期限由上游耗尽时，仍返回 504/provider_timeout，已过期的期限不能再用来阻塞错误响应。探测在数据库登记或保存阶段不能确认结果（含耗尽期限）时，返回 503/storage_unavailable，日志标出对应写入阶段。普通生成沿用原总期限。对已经确认的上游用量，即使输出因工具调用等不受支持的内容被拒绝，也保留有效数值。

上游诊断字段独立核对。读 [Provider 解析](../../../src/providers/base.py) 与 [SSE 解析](../../../src/providers/streaming.py) 时，可配合 [可持久化的元数据](../../../src/utils/diagnostic_values.py) 和 [原始协议字节回归](../../../tests/protocol/test_f8_rejected_usage.py)。模型名缺失或含非法字符的回复仍然失败，独立验证通过的 Usage 则保留。存储层也保护由注入的 Provider 抛出的异常元数据，避免非法字符串妨碍已知终态落库。

验收通过。最终全量 1904 项通过，16 个真实调用用例均执行。第三轮独审无 P0–P2。[保留的注入式异常用量 P3](review-r3.md)仅影响不合规自定义 Provider 的普通响应汇总。
