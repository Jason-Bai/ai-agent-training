# F5 Streaming 第 2 轮独立代码审查报告

审查方式：仅 Read。未修改任何文件、未运行命令、未发送真实 API、未读取 `.env`/`credentials.yaml`/`.claude`/`.venv` 或密钥文件。全部运行输出由执行者/切片任务产生，本报告区分执行者证据与审查者静态推导，不把通过数量或已读文件数当作正确性证明。

## 零、本轮 Read 范围与证据归属

**本轮直接 Read 的文件（46 份）**：必读清单 44 份全部逐份直接 Read，包括首轮遗漏的 4 份：

| 首轮遗漏文件 | 本轮核实结果 |
| --- | --- |
| `AGENTS.md`（项目根） | 内容与当前会话系统上下文一致，F5 流程/边界/Don't 条款完整 |
| `src/utils/__init__.py` | 确认为空文件；与审前摘要 `e3b0c44…`（空串 SHA-256）一致 |
| `tests/unit/test_f3_config.py` | 已读：F3 配置契约含 `supports_streaming=True` 迁移、能力严格布尔、凭据不泄漏断言 |
| `tests/live/test_f5_streaming.py` | 已读：两条显式真实路径，断言事件族、usage 与 Trace 一致、`input_delta==0`、元信息经 JUnit record_property 安全回传 |

另补充直读 2 份（不在清单、F5 承载依赖）：`src/errors.py`（PROVIDER_MESSAGES 含全部平台失败码，ProviderStreamEvent 校验依赖成立）、`src/api/errors.py`（error_response 写 `state["error_code"]`，与 Middleware 收口配合正确）。

**复用而未直接读**（均为未变 F2/F4 公共件）：`src/services/prompts.py`、`token_budget.py`、`validation.py`、`credentials.py`、`token_counters/*`、`src/settings.py`、`src/utils/json_codec.py`、`tests/conftest.py`、`tests/smoke/conftest.py`、`tests/test_real_providers.py`（live fixture）、`tests/f4_cases.py`、`tests/prompt_cases.py`、`docs/test-plan.md`、`docs/progress.md`、`docs/templates/*`、`docs/references/*`、`evidence.json` 中间段。复用依据：76 份审前哈希回执前后一致（执行者证据）、首轮原始 Read 日志、以及本轮核心执行/传输/协议/契约/绑定及全部新测试的直接完整阅读。所有哈希、运行输出、Red 回执均为执行者证据；我未重跑、未计算哈希。

**增量核对**：`review-diff-r2.patch` 全文 212 行已读，6 份变更文件（4 源码 + 2 测试）逐一与盘上内容比对，+ 侧内容全部存在且一致；变更范围与声明相符，无暗改。

## 一、首轮遗留事项核实

1. **Usage/模型元信息丢失**：已核实修复。`observed` 为每次 `stream()` 调用的局部字典（deepseek.py:48、openai_compatible.py:52）；chat 逐块 `observed.update(meta)` 且在 stop 块构造验证后的 `terminal` 时记录 usage/finish_reason（providers/streaming.py:50、72）；responses 在 created/in_progress 严格校验（object/status/model）后记录（providers/streaming.py:119–122）；所有异常出口 `failure(code, **observed)`。只保留严格校验过的信息，`upstream_terminal_received` 保持 False，Generation 终点仍未知——与设计 2.4/3.7 一致。证据：`isolated-tail-usage-red.txt`（2 failed，usage=None 即目标缺失）、`isolated-progress-model-red.txt`（2 failed，actual_model=None）为有效 Red；当前 `test_chat_missing_tail_keeps_valid_usage`、`test_responses_known_model_survives_later_protocol_failure`、`test_gateway_failure_trace_retains_observed_wire_usage`（端到端 Trace 断言 10/5/15、upstream_terminal_at is None）通过。
2. **首轮反例 6（头发送返回已过期限⇒零 Attempt）推导不成立**：已证伪并修复。`isolated-expired-start-red.txt` 实测旧代码 `provider.opened==1`（1 failed），确属正确 Red；当前 services/streaming.py:133–134 在创建 Attempt 前显式重查期限，且与 `start_attempt`/`stream()` 之间无 await，检查原子生效；`test_no_provider_attempt_after_headers_return_beyond_deadline` 断言 `attempts==0/opened==0` 通过。修复正是设计 2.3 已确认条文“响应头发送返回后再次检查 deadline，已过期则不创建 Attempt/调用 Provider”的落实，不构成需求变更。
3. **首轮工具审计**：`review-r1-metadata.json` 与描述一致（82 Read/59 唯一文件/4 项必读缺失、1 次范围外 MCP 请求被权限拒绝未执行、exit 0）。首轮“已读”自述不可靠的判断成立；本轮以实际直接 Read 为准。本轮未发起任何范围外工具请求。
4. **首轮静态判断复核**：对首轮反例表中依赖本项路径的关键项重新推导（见下节），除已被 Red 证伪的第 6 项外未发现其他推导错误；第 2、10 项归属语义与设计逐字一致。

## 二、三维覆盖

| 维度 | 审查对象与依据 | 结论 | 未覆盖/未验证 |
| --- | --- | --- | --- |
| 需求正确性 | 用户需求五条（总 timeout+15 秒空闲心跳；统一抽象不把 finish_reason 当平台接口；不支持的参数不出现在请求里；Generation 可记录/可反推；原生 Claude Code 三维审查）→ design.md/test-plan.md（用户分段确认）→ 代码 | 满足，无遗漏或共同歪曲。B 起点固定 deadline 覆盖响应头/建连/读取/全部实际 send（services/streaming.py:45、52–59、104–119；api/sse.py:32–33、42–46）；`STREAM_HEARTBEAT_SECONDS=15` 且测试硬编码 15 断言常量、边界用例不引自生产常量（test_f5_execution.py:361–374）；ProviderStreamEvent 统一两协议，执行器只按 status/error_code 收口、不按原始 finish_reason/事件名分支；StreamRequest extra=forbid、OpenAPI 仅 5 字段（contracts.py:66–78，已复现）；TTFT/Generation 由内部时间点推算、未知 null、不冒充 | 15 秒心跳无真实墙钟 15 秒端到端实测（短心跳为测试私有注入，设计已声明，非缺陷）；真实两例为固定输入样本，不代表通用一致性（执行者已声明） |
| 实现一致性 | 契约（统一事件族、单终态、背压、有界清理、错误优先级、调用次数、Trace 元信息）对照 5 份 F5 测试与全部核心源码；r2 增量 212 行全文 | 一致。事件编码经 JSON 转义（api/sse.py:9–10）；“当前增量写出成功后才读下一条”且心跳不重建挂起读取（streaming.py:143–157）；`_emit` 在 await send 前先置 `terminal_attempted` → 无第二终态窗口（:109–110）；到期后失败通知为单调度轮尽力、不获新预算（:235–244）；嵌套 Usage/非法内部事件逐字段重验（:172–183）；`usage=0` 与未知 null 区分；`provider_error_code` 与最终 `error_code` 分离保留 | 断连与 provider failed 同轮竞争无专门用例（静态确定+子路径覆盖，非阻断，见反例 4） |
| 系统正确性 | Middleware→Gateway→Execution→Provider→Trace 全链；F1–F4 回归（r2-full 1097 内）；真实子进程并发/断连/期限用例；资源所有权 | 一致。预检失败由 Middleware 收口且零 Attempt（request_context.py:53–54 + stream_execution_owned 在 run() 前设置，api/sse.py:23，无双收口）；已开始流由执行器唯一收口、同步 finally 保证不留 running（streaming.py:91–100）；单流清理不关共享客户端（smoke 断言 `client_closed==0`；协议回环断言共享 client 存活）；并发隔离由真实单 worker 子进程用例证明；`observed`/`deadline`/`next_task` 均为每流局部状态，无跨请求共享可变状态 | 哈希一致性（76/51）为执行者回执，我未自行计算；macOS 3.14 全量/3.12 关键集之外平台未验证；多实例/多 worker 范围外 |

## 三、独立推导的反例/时序及检查结果

| # | 推导场景 | 检查结果 | 状态 |
| --- | --- | --- | --- |
| 1 | Chat 合法 stop+Usage 后缺 `[DONE]`/坏尾块 | Usage/actual_model/finish_reason 经 per-stream observed 保留进失败事件与 Trace；`upstream_terminal_received=False` → Generation 终点未知 | 已复现[执行者]（2 Red→Green，含端到端 Trace 断言） |
| 2 | Responses 已给 created/in_progress 后 EOF/坏块 | actual_model/upstream_status='in_progress' 保留；usage 从不经 observed（responses 终态后即 return，无后置失败窗口） | 已复现[执行者]（2 Red→Green） |
| 3 | 同步受控头发送阻塞越过 deadline 才返回 | 创建 Attempt 前重查期限 → `request_timeout`、0 调用/0 Attempt；与“start 永不返回被取消”是不同时序，不可互推 | 已复现[执行者]（Red：旧代码 opened==1；Green：opened==0） |
| 4 | 断连与 provider failed 同轮完成 | run() 分支 `completed_sent` > `disconnect in done` > worker（streaming.py:54–59）；`_observe` 先于 raise 执行 → `attempt.provider_error_code` 保留，最终 `client_disconnected` | 静态确定；无专门同轮用例，子路径各有覆盖（test_disconnect_cleans_only_own_stream、ASGI blocked='response.failed'），与设计 2.4 逐字一致 |
| 5 | deadline 到期时下游仍阻塞（终态/心跳/空帧无法送达） | run 级 timeout 取消 worker → cleanup 关单流；sse.py 剩余期限≤0 跳过最终空帧 → 传输关闭；Trace 仍 `request_timeout` | 已复现[执行者]（ASGI 五种阻塞 + smoke 真实 TCP） |
| 6 | completed 携带最后 delta 的同一事件 | `_work` 先发 delta 再处理终态（:160–170），与 failed 同事件路径共用、行为正确 | 静态确定；**无直接端到端用例 → 见 P3-1** |
| 7 | 到期后失败通知尽力分支绕过 `_emit` 检查 | 先置 `terminal_attempted`，`asyncio.sleep(0)` 单调度轮，未完成即 cancel；不获新发送预算、至多一次终态尝试 | 静态确定；阻塞变体已复现[执行者]（blocked='response.failed'） |
| 8 | model_construct 嵌套 Usage/非法内部事件绕过校验 | `_validate_event` 取字段值转 dict 重验，非法不发其 delta | 已复现[执行者]（test_constructed_nested_usage_is_revalidated、test_illegal_internal_event_is_rejected_before_its_delta） |
| 9 | 连续空事件饿死心跳 | 循环顶部独立空闲时钟检查 | 已复现[执行者]（2000 空事件用例） |
| 10 | 清理期间再次取消 / 关闭异常 / 挂起 close | shield 循环保证 cleanup 完成、同步 finally 收口；`cleanup_error_code` 不覆盖首因与已完成终态；1 秒共享宽限有界 | 已复现[执行者]（3 个用例，含取消读取中的关闭异常） |
| 11 | 终态发送异常 | 不启动第二个终态；OSError→client_disconnected、其他→internal_error；`upstream_terminal_at` 仍保留 | 已复现[执行者] |
| 12 | Provider 裸 TimeoutError/OSError 责任方 | 非发送边界的裸 TimeoutError 归 internal_error（clock 判定）；Provider 意外 OSError 不误归下游断连 | 已复现[执行者]（含 1 个有效 Red 记录） |
| 13 | delta 伪造 `event:`/`data:` 帧、换行/引号 | JSON 编码转义，解码后原文精确 | 已复现[执行者] |
| 14 | 调用方伪造 X-Request-ID | 中间件生成并覆盖，事件内 request_id 与响应头/Trace 一致 | 已复现[执行者] |
| 15 | completed 写出后断连 | 成功保留，不被晚到断连改写 | 已复现[执行者]（单测+smoke） |
| 16 | 并发 A 断连/超时、B 正常完成 | 只关 A 的流，共享客户端存活，按 request_id 关联 | 已复现[执行者]（真实单 worker 子进程） |

## 四、Findings

- **P0（密钥泄漏/越权/严重数据破坏）：0**
- **P1（主链或关键安全边界失效）：0**
- **P2（约定场景不满足）：0**
- **P3（不影响验收的轻微问题）：1**

**P3-1：已确认测试方案 E02 的“completed 与最后 delta 同事件”变体无直接端到端用例。**
- 位置：tests/unit/test_f5_execution.py（全文已读，completed 终态用例均不含 delta；failed 同事件变体由 test_last_delta_with_failed_terminal_and_no_further_reads 覆盖）；对应 test-plan.md 2.2 E02 行与 3.7 节“E01—E09”覆盖声明。
- 触发/影响：同一 ProviderStreamEvent 同时携带 `status='completed'` 与非空 delta 的发出顺序缺少直接断言，属测试覆盖缺口，不是行为缺陷。
- 静态核实：services/streaming.py:160–162 先发 delta、:165–170 后发 completed，与已测的 failed 同事件路径共用同一代码序，行为正确。
- 最小验证/修正：在 test_f5_execution.py 增加一条 `[event(delta='正文', status='completed')]` 断言输出恰为 `['response.delta','response.completed']`；不阻断验收，处置结论记录在案即可。

观察项（非 Findings，不凑数）：流式 Attempt 的 `latency_ms` 在同步 finally 中记录（streaming.py:99），含 ≤1 秒的有界清理与失败通知时间；该字段对流式的语义设计未单独定义、仅内部保存，不构成契约违反。`_heartbeat_delay` 用 `time.monotonic`、deadline 用 `loop.time()`，CPython 下同一 perf_counter 基准，无混钟问题。`api/sse.py:18` 的 `del self.headers['content-length']` 依赖 Starlette MutableHeaders 对缺失键不抛 KeyError 的语义，属防御性 no-op，集成/冒烟测试佐证。

## 五、证据来源与未验证项

- 已核对原始回执：`r2-full.txt`（1097 passed/8 skipped，72.91s）、`r2-py312.txt`（233 passed，14.85s）、`r2-live.txt`（2 passed，2.57s，与 `live-r2-results.xml` time=2.572 一致）；live XML 逐字段核验：两例 attempts=1、status=completed、usage 514/9/523 且 input+output=total、`input_delta=0`、ttft ≤ stream_duration、Chat stop/Responses completed 分类正确、9 次 delta 事件、无敏感正文。
- 隔离 Red 三份原始 txt 均为有效 Red（目标行为缺失导致的断言失败，非导入/拼写错误）；`r2-docs.txt`（review_inputs_unchanged=76、protected_unchanged=51、test_groups=51）与 `review-source-before-r2.json`（76 份哈希，含空文件空串哈希）为执行者回执。
- 未验证：76/51 哈希一致性未自行计算；真实两例为固定输入样本、两条适配器同连 DeepSeek，不构成双供应商容灾或通用一致性证明（执行者已声明）；15 秒心跳无生产墙钟实测；其他平台/多实例为范围外。累计 6 次 F5 真实调用与 test-plan 3.9 记录一致。

## 六、VERDICT

**APPROVED**（P0=0，P1=0，P2=0，P3=1）

三维审查均完成：首轮遗漏的 4 份必读已全部直接 Read；首轮报告两项实质问题（元信息丢失、反例 6 零 Attempt 误推导）均以有效 Red 复现并修复，修复落实的是已确认设计条文而非新需求；本轮未发现新的 P0–P2。关键验收证据（隔离 Red、r2 全量/3.12/真实两例原始回执、212 行增量与盘上内容一致、审查输入不变回执）齐全；P3-1 为测试覆盖缺口，行为已静态确认正确，不阻断验收，处置结论建议按上述最小验证补一条用例并记录。审查边界（仅 Read、不重跑、不称执行者运行为自己运行）已遵守。