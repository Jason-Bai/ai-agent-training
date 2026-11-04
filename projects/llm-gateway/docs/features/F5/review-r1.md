# F5 Streaming 第 1 轮独立代码审查报告

审查方式：仅 Read。未修改任何文件、未运行命令、未发送真实 API 调用、未读取 `.env`/`credentials.yaml`/`.claude`/`.venv` 或任何密钥文件。所有运行证据由执行者产生并回传，本报告区分执行者证据与审查者判断，不把通过数量或已读文件数当作正确性证明。

## 一、三维覆盖

| 维度 | 审查对象 / 依据 | 结论 | 未覆盖项 |
| --- | --- | --- | --- |
| **需求正确性** | 用户需求原文五条（总超时+15s心跳；统一 finish_reason；请求参数不出现未支持字段；Generation Time 可推导；原生 Claude Code 三维审查）；[design.md](docs/features/F5/design.md)、[test-plan.md](docs/features/F5/test-plan.md) 与实现逐一比对 | 满足。心跳 `STREAM_HEARTBEAT_SECONDS=15`（constants.py:11）；总期限自预检后 B 点起算并覆盖实际 ASGI send（services/streaming.py:45、api/sse.py:43-46）；`ProviderStreamEvent` 统一两种协议终态证据，执行器不按原始 finish_reason 分支（services/streaming.py:161-168、providers/base.py:21-50）；`StreamRequest` extra="forbid"，`response_schema`/`stream`/`api_protocol` 均 422 零调用（contracts.py:66-78，已复现）；TTFT/Generation 由内部 Trace 时间点推算且仅明确远端终态记录 Generation 终点（services/trace.py:34-48、streaming.py:183-187） | 多 worker/多实例/跨实例取消、恢复、持久化明确不在 F5 范围（进度文件已确认），不构成本项缺陷；真实两例仅固定输入样本，不代表通用一致性或生产验证（执行者已声明） |
| **实现一致性** | 契约（统一 `response.*` 事件、单终态、背压、有界清理、错误优先级）对照 5 个 F5 测试文件与实现；真实协议回环 61 例；真实 HTTP 子进程 6 例 | 一致。事件编码经 JSON 转义防伪造帧（api/sse.py:9-10，已复现 test_unicode_newlines…）；读取只在上次发送成功后进行、无预取（streaming.py:146-160，已复现 test_send_backpressure_does_not_prefetch）；终态最多一次尝试、completed_sent 后不被晚到断连改写（streaming.py:109-119、54-59，已复现）；清理 1 秒共享宽限 + shield + 同步 finally 收口（streaming.py:69-102，含 2 个有效 Red 的闭环证据）；`request_timeout`/`client_disconnected` 可覆盖执行结果而 `attempt.provider_error_code` 保留上游分类（streaming.py:87、192-193） | 执行者未运行“断连与 provider failed 同轮完成”的精确专门用例（相关路径已有覆盖，见反例表第 2 项） |
| **系统正确性** | 调用链沿 Middleware→Gateway→Execution→Provider→Trace 检查；1090 全量回归含 F1–F4；真实 TCP 子进程并发用例；Trace 所有权划分 | 一致。预检失败由 Middleware 收口且零 Attempt（middleware/request_context.py:51-54 + api/errors.py:12，已复现）；已开始流由执行器唯一收口，`stream_execution_owned` 防双重 finish（request_context.py:53，finish_request 幂等防线 trace.py:91-94）；单流清理不关闭共享 Provider 客户端（smoke 断言 `client_closed==0`，协议回环断言共享 client 存活）；并发流隔离由真实子进程用例证明（test_concurrent_stream_survives_other_disconnect）；共享状态均为事件循环内同步操作，无跨请求可变执行器状态 | 各层运行证据为执行者产生，我未重跑；76 份输入前后哈希一致为执行者回执（pre-review-docs），我未自行计算哈希；macOS + Python 3.14 全量/3.12 关键集之外的平台未验证 |

## 二、独立推导的反例/故障时序及检查结果

| # | 推导场景 | 检查结果 | 状态 |
| --- | --- | --- | --- |
| 1 | 断连通知与 completed 发送同轮竞争 | run() 首分支 `completed_sent` 优先（streaming.py:54），成功保留 | 已复现[执行者]（test_disconnect_after_completed_send_keeps_success） |
| 2 | 断连通知与 provider failed 同轮完成 | `disconnect in done` 分支优先（streaming.py:56）→ 最终 `client_disconnected`；`_observe` 先于 raise 执行，`attempt.provider_error_code` 保留上游分类，符合“发送失败覆盖执行结果时保留已确认分类”设计 | 静态确定；执行者未运行该精确同轮用例，相关路径已由 test_disconnect_cleans_only_own_stream 覆盖，非阻断 |
| 3 | `model_construct` 嵌套 Usage 绕过校验 | `_validate_event` 逐字段重验并转 dict（streaming.py:170-181）拦截 | 已复现[执行者]（test_constructed_nested_usage_is_revalidated，含 3 Red 记录） |
| 4 | 连续 2000 个空事件饿死心跳 | 循环顶部独立空闲时钟检查（streaming.py:144-145）防止 | 已复现[执行者]（test_continuously_ready_empty_events…，含有效 Red） |
| 5 | deadline 与 provider 失败同事件循环轮竞争 | 同轮由 timeout_at 胜出 → `request_timeout` | 已复现[执行者]（test_due_deadline_wins…；provenance 如实注明该用例首跑即 PASS，未据此改实现） |
| 6 | start() 阻塞恰好耗尽期限，`remaining=0` 发起尝试 | 推导：`remaining=0` 时 deadline 定时器已到期，`_work` 下一次 await 必先收到 TimeoutError → `request_timeout`、0 Attempt，provider_timeout 无胜出窗口 | 静态确定；start 阻塞不返回的变体已由 test_total_deadline_covers_actual_send(blocked='start') 复现[执行者] |
| 7 | 清理期间再次取消 | shield 循环（streaming.py:74-79）保证 cleanup 完成；循环退出时 cleanup 必已 done，`cleanup.result()` 处无孤儿任务；同步 finally 收口 Trace 后传播取消 | 已复现[执行者]（2 个有效 Red→Green，test_task_cancelled_during_cleanup…、test_cancel_during_failure_notification…） |
| 8 | 失败通知期间取消 | 收口为 `internal_error`、`provider_error_code` 保留、单终态、客户端可能只见截断流 | 已复现[执行者]（test_cancel_during_failure_notification_cannot_leave_running_trace） |
| 9 | 取消 pending anext 时 generator finally 抛错 | `stream_cleanup_error` 记录，不覆盖执行首因 | 已复现[执行者]（test_close_error_from_cancelled_pending_read_is_recorded） |
| 10 | 到期后失败通知的尽力分支绕过 `_emit` 检查（streaming.py:233-242） | 单轮尽力、不获新发送预算；创建与断连窗口内 OSError 被吞、无终态送达——与“到期后一调度轮尽力发送”“终态可能无法送达”设计一致 | 静态确定；相关阻塞变体已由 test_total_deadline_covers_actual_send(blocked='response.failed') 复现[执行者] |
| 11 | usage=0 与 unknown 区分 | 实测 `{0,0,0}` 原样保留，未知保持 null，不以 0 冒充 | 已复现[执行者]（test_remote_failure_keeps_generation_endpoint_and_exact_usage + integration usage null） |
| 12 | delta 内容伪造 `event: response.completed` 帧 | JSON 编码转义换行，客户端解析器不可伪造帧 | 已复现[执行者]（test_unicode_newlines_and_sse_looking_text_are_escaped） |
| 13 | 调用方伪造 `X-Request-ID` | 网关生成并覆盖，事件内 request_id 与响应头一致 | 已复现[执行者]（test_f5_gateway.py:117-121） |
| 14 | Provider 意外抛裸 TimeoutError/OSError | 裸 TimeoutError 归 `internal_error`（streaming.py:60-61 的时钟判定）；Provider 抛 OSError 归 `internal_error` 而非下游断连，责任方不错归 | 已复现[执行者]（test_eof_and_unexpected_error，含 1 个有效 Red：意外 Provider OSError 误归下游断连已修复） |
| 15 | deadline 到期时下游仍阻塞：终态与最终空帧均无法送达 | 客户端见截断流，Trace 仍收口 `request_timeout`、上游单流关闭、不关共享客户端 | 已复现[执行者]（smoke test_total_deadline_stops_heartbeats_and_waiting_provider，真实 TCP 子进程） |

## 三、Findings

- **P0（密钥泄漏/越权/严重数据破坏）：0**
- **P1（主链或关键安全边界失效）：0**
- **P2（约定场景不成立）：0**
- **P3（不影响验收的轻微问题）：0**

说明：候选观察点在推导检查中均被排除或证实符合已确认设计（如反例表第 2、6、10 项的归属/尽力语义均与设计逐字一致；第 7 项初疑“孤儿 cleanup 任务”在重推后发现 shield 循环保证 cleanup 完成，不成立）。按审查约定不凑问题数量，上述反例表即处置记录。所有反例结论均注明“已复现[执行者]”或“静态确定”，无把推测冒充复现的项。

## 四、执行者证据核实结果

- 原始 Red 证据有效：route-red（6×404 发生于路由实现前）、cleanup-cancel-red（2 例 trace 留 running 发生于同步收口实现前）；provenance 对 same-turn-deadline-red 首跑即 PASS 的披露如实，未据此改实现。
- 基线 878/6 skip（F4-O1 后、F5 前）→ frozen-full **1090 passed / 8 skipped**（72.64s，新增 2 个 live 默认跳过与测试数变化吻合）；py312-frozen **226 passed**（14.47s）；live-release **2 passed**（1.86s）。
- 最新真实回执（live-release-results.xml）逐字段核验：主侧 DeepSeek/Chat（stop、usage 514/9/523、TTFT 534.88ms、Generation 137.70ms）与备侧 Compatible/Responses（completed、同 usage、563.82/117.54ms）各 1 次、`input_delta=0`、attempts=1、status=completed；上游诊断字段与事件模型 Literal 约束一致。累计 4 次 F5 真实调用与 test-plan.md 3.7 记录一致。
- 证据边界（执行者已声明并核实）：两次固定输入样本不代表通用一致性；两条适配器都连 DeepSeek，不构成双供应商容灾；短心跳（冒烟 0.05s）为测试私有注入，不冒充 15 秒墙钟实测。
- pre-review-docs 回执：76 份审查输入前后一致、51 份受保护资源/历史验收/本机配置未变——为执行者回执；我抽查的全部 22 个 src 文件与 5 个 F5 测试文件当前盘上内容与 review-diff-r1.patch 的 + 侧一致。

## 五、已读/未读材料与验证来源

**已读（直接读盘）**：项目与 F5 全部文档（AGENTS.md、docs/AGENTS.md、progress.md、README.md、F5 design/test-plan/implementation-summary/implementation-plan、evidence.json 头部与尾部、live-release-results.xml、临时目录 7 份 txt 证据）；全部 22 个 src 文件；test_f5_contracts/execution/gateway/providers/http_server、f3_cases、f4_cases、test_trace_integrity、test_f3_gateway、test_f4_gateway、test_f4_providers、deepseek_v41.py；config/gateway.yaml。review-diff-r1.patch 全文 2277 行（含 tests/live/test_f5_streaming.py）于本会话压缩前完整读取。

**未读（如实列出）**：tests/smoke/conftest.py（在哈希清单中，非必读）；首轮真实回执 live-results.xml 原文（仅有哈希，最新轮已读）；evidence.json 中间段（第 150–1790 行各阶段 raw_runs 明细，结论以 test-plan.md 3.6 汇总、首尾回执与哈希一致性为准）。

**验证来源划分**：一切运行输出、哈希、Red/Green 记录为执行者证据；本报告结论所依赖的代码行为由我逐行静态推导并逐项标注状态；未验证项（其余平台、通用真实一致性、多实例）如实列出。

## 六、VERDICT

**APPROVED**（P0=0，P1=0，P2=0，P3=0）

三维审查均完成：需求正确性（用户五条需求全部落实且边界声明准确）、实现一致性（契约与实现逐项吻合，反例推导无契约违背）、系统正确性（老功能回归、并发隔离、资源生命周期、异常传播与 Trace 所有权闭环）均已核实。关键验收证据（有效 Red、最终全量、3.12 关键集、最新真实回执、审查输入不变）齐全，无关键证据缺口，无 P0–P2 阻断项。审查边界（仅 Read、不重跑测试、不称执行者运行为自己运行）已遵守。