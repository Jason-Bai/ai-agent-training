# F5-O1 第1轮完整代码审查报告（独立审查）

**审查方式**：仅使用 Read 逐段阅读变更与证据；未运行任何命令/测试、未调用真实 API、未写文件。运行证据全部由实现者产生，本报告逐项标注来源。

**VERDICT：APPROVED** —— P0：0，P1：0，P2：0，P3：4（均非阻断，处置建议见 §三）。三维审查完整，无关键验收证据缺口。

---

## 一、三维覆盖表

### 维度1：需求正确性

| 检查点 | 依据 | 结论 |
| --- | --- | --- |
| 普通/结构化/现有直接 POST SSE 统一走真实 PG 持久化 | `gateway.py:146-150`（`persist = self.storage is not None and trace.operation != 'provider_probe'` → prepare→begin_attempt→finish 三段）；`streaming.py` prepare/append/finish | 满足 |
| 流式业务事件先落库后发送 | `streaming.py._emit`：`response.delta` 先 `append_event` 再 send；终态先 `finish` 再 send | 满足；并有真实提交屏障测试（独立连接观测 0 事件后才发送） |
| PG 为必需依赖、显式 Alembic 迁移 | `database.py.check()` 校验 `alembic_version=='f5o1_001'` + 三表 LIMIT 0；`migrations/versions/f5o1_001.py` 冻结 metadata 副本（含"外键不重复"测试） | 满足 |
| 启动恢复语义 | `store.py.recover()` 仅 running/canceling→failed/execution_interrupted（流式有快照补 response.failed 事件），pending/终态不变 | 满足；测试覆盖保留 pending/终态/usage、双次重启幂等 |
| 运行时对账只收口已停止执行、未知提交对照 DB 真相、completed 不可被发送失败覆盖 | `main.py:47-73`（flush 跳过 `active`；`_persisted_terminal` 为真时只 update_diagnostics）；`gateway.py:51-60`（成功仅在终态提交后返回，`finish != 'completed'` 抛 503）；`request_context.py`（已持久终态后仅记 `delivery_error_code`） | 满足 |
| 探测仍只在内存、`app.state.traces` 为诊断视图 | `gateway.py:146` 探测不持久化；traces 只在进程内 | 满足 |
| 范围外不提前实现 | 未发现 O2 续传/202 接口、F6 取消接口、F7 重试、F8 查询、F9 计费、清理策略的任何实现或占位；`canceling` 仅存在于 6 态枚举/约束/recover 数据建模，非接口 | 无越界实现 |
| 实现者5条解释逐条反证核实 | （1）短事务/5连接池/每操作5秒预算并取剩余截止时间最小值：`database.py.transaction` 的 `limit = min(now+5, deadline)`、pool_size=5/max_overflow=0，且流式 `prepare()` 先于 `storage.prepare` 设 B 起点总期限、`append_event/finish` 均带 deadline —— 测试实测 pool.size()==5、overflow()==0、等待 4.8-6s、pg_sleep(3) 在 1s 期限下 0.8-2s 内失败；（2）PG18.6+psycopg3+Alembic：compose.yaml/pyproject/lock 一致；（3）PG 为真相、traces 为诊断、探测内存：代码一致；（4）行锁分配 seq 并保护终态、Request/Attempt/终态事件同事务：store.py 逐条核实 + 12 并发 append seq 1..12、竞争终态单赢家测试；（5）快照/载荷用共享精确十进制 json_codec TEXT、普通回答不缓存（无任何缓存代码路径）、completed 幸免发送失败、未知提交由生命周期管理的对账收口且不重调 Provider（flush_pending 无任何 Provider 访问） | 全部成立，无虚假声明 |

**需求正确性结论**：确认范围全部兑现；发现一处错误文案与当前能力不符（P3-1，见 §三），不影响验收。

### 维度2：实现一致性

| 检查点 | 依据 | 结论 |
| --- | --- | --- |
| 有效输入快照内容 | `gateway.py:148` 对普通/结构化路径使用 `build_messages` **装配后**的消息（含受控 System 模板与结构化输出契约）构建快照，即"实际发送给 Provider 的有效输入" | 与"有效输入快照保存正文"契约一致 |
| DB 值与约束 | 6 态 status、usage 全有全无 + total=input+output 检查约束、`last_event_seq` 与 operation 配对、seq>0、终态事件名必须等于 `'response.'+status`（`store.py.finish` 行级校验，错配整体回滚——有测试） | 一致 |
| 公共输出契约 | SSE 线上无 `id:`/`seq`（测试断言）；request_id 仅放 `X-Request-ID` 头；普通 JSON 四字段成功/错误统一格式经 F4 错误处理器不变 | 一致 |
| 错误优先级 | 15 类入口参数拒绝：无 Attempt、无快照、零上游调用（测试断言调用数）；存储不可用在 Provider 调用前失败（prepare/begin_attempt 失败 → 零上游调用）；7 类上游错误码分类存留；结构化失败保留 usage | 一致 |
| 副作用与调用计数 | 探测/404/405 零持久化；Provider 调用次数在关键测试中显式断言；拒绝记录失败时日志含 request_id+code、无原始 body、零行（fail-safe） | 一致 |
| 测试替身与证据是否掩盖缺陷 | MockProvider 仅替换模型调用，事务走真实 PG；"提交确认丢失"用 monkeypatch 的 `__aexit__` 包裹**真实 commit/rollback**；"先落库后发送"用独立连接观测 + 真实数据库触发器屏障，非自我断言；conftest 哨兵阻止读取真实配置/密码/非回环 socket | 无掩盖 |
| 有意行为变化 | 截止到期且带存储时**不发终态帧**（`streaming.py:296-303`，remaining≤0 直接返回错误）——设计 3.4 已确认，F5 回归断言相应改为 `terminal_attempts <= 1` | 属已确认设计，非缺陷 |

### 维度3：系统正确性

| 检查点 | 依据 | 结论 |
| --- | --- | --- |
| 老功能回归 | 1224 通过/14 跳过（live 显式跳过）含全部 F1-F5 套件；F5 单测（storage=None 内存分支）语义不变：期限覆盖发送、心跳不重置期限、断连只清自有流、清理错误不覆盖成败、无孤儿任务 | 通过 |
| 并发请求隔离 | 行锁串行化 seq；并发普通+流式独立（finish 顺序颠倒仍各自正确）；`active` 集合阻止对账误伤活跃请求；`flush_pending` 仅在单 reconcile 任务中运行、对 `list(pending.items())` 快照迭代，单事件循环下无并发字典问题 | 通过 |
| 资源构造/释放 | 5 连接池无溢出（测试实测）；正常关闭后 `pg_stat_activity` 零事务、Provider 客户端关闭（provider_closed==2、-SIGTERM）；watch_disconnect 任务 gather 回收 | 通过 |
| 异常/取消传播 | CancelledError 属 BaseException：`gateway.py:34` 捕获后在取消语境中尝试持久化失败终态，二次取消中断时由中间件 defer 收口（`request_context.py` 取消分支在未持久化时置 error_code 并 defer）；外部取消在提交前/提交中均不破坏 DB 真相（有测试）；`database.py` 明确只把 DBAPIError/Disconnection/PoolTimeout/OSError/Timeout 转为 StorageUnavailable(uncertain=committing)，编程错误（ValueError/InvalidRequestError 等）原样传播（有测试） | 通过 |
| 失败窗口 | 提交确认丢失→对账 DB 真相；回滚后丢失→failed+failed 事件；SIGKILL→启动恢复不回调 Provider、双重启幂等；数据库中断→仅收口已停止执行、活跃请求不受扰；离线关闭→SIGTERM 干净退出 | 通过（均为执行者进程级测试证据） |
| 资源上限 | 池饱和时确定性退化为 storage_unavailable 失败（uncertain=False→流式发失败帧/普通 defer），DB 不出现矛盾；`deadline_at` 精度、`timing_snapshot` 后置更新不覆盖执行真相（latency 为 None 时保留） | 通过（单等待者场景）；多请求同时排队无专门测试【待验证风险】 |
| 部署假设 | 单实例单 worker 声明与实现一致（active/pending 进程内）；compose 只绑 127.0.0.1:15432；密码独立文件且测试写 chmod 600；`.gitignore` 含 `config/postgres.password`（已核实第7行） | 一致；多实例不在本项范围 |

**行为说明（非缺陷，静态确定）**：普通请求无覆盖存储操作的网关总期限——Provider 调用仍精确受 `timeout` 约束，存储三操作各 ≤5s 是额外开销；与 F3/F4 超时契约（只约束 Provider 调用）不冲突。普通请求主机断开记录为 failed/internal_error + `delivery_error_code='client_disconnected'` 诊断，与 F5 记忆语义及 `test_trace_integrity` 注释一致，不虚构 F6 canceled 状态。

---

## 二、独立推导反例与时序

**T1 终态提交后发送失败**：客户端在 completed 事件发送前消失 → `_emit` 的 send 抛 OSError/超时 → 中间件检测到已持久终态，仅写 `delivery_error_code` → DB 保持 completed，失败无法覆盖成功。【执行者已复现】`test_committed_success_survives_final_delivery_failure`（SSE 阻塞→`request_timeout`；JSON OSError→`client_disconnected`）。

**T2 截止到期瞬间**：remaining≤0 → 不发任何终态帧（设计 3.4），后台 flush 将 DB 置 failed 并补 response.failed 事件 → **线上客户端看到无终态帧的流结束，而 DB 真相为 failed**，二者刻意不一致但可解释。【执行者已复现】"database wait obeys remaining deadline"（elapsed 0.8-2s，delta 从未发出，seq 1 失败事件经后台 flush 落库）。

**T3 未知提交三分支**：终态事务提交中连接丢失 → uncertain=True → 抑制 failed 帧（不误导客户端）→ 后台对账：DB 已 completed → 请求实际成功、仅记投递错误；DB 仍 running → 落 failed + failed 事件；回滚后丢失 → 同上。【执行者已复现】commit-confirmation-loss（uncertain→reconcile→completed）、unknown terminal commit（committed True/False）、rollback-then-lose。

**T4 入口拒绝且存储不可用**：拒绝记录插入失败 → defer → 后台以 `on_conflict_do_nothing` 补插仅含已知元信息的 failed 行；日志含 request_id+code、不含原始 body。【执行者已复现】"rejection record failure safe log"（零行 + 日志断言）。

**T5 并发与竞争**：12 个并发 append 行锁串行 → seq 1..12 无空洞；两个终态事务竞争 → 单赢家、无矛盾事件；普通/流式并发、finish 反序仍各自正确。【执行者已复现】三个专项测试。

**T6 二次取消窗口**：清理期间再次取消中断 finish 的 await → CancelledError 穿透 `except StorageUnavailable`（不吞）→ 中间件取消分支在未持久化时 defer → flush 收口；若进程在此窗口死亡 → 启动 recover 兜底。【执行者已复现】"repeated host cancel during cleanup" + SIGKILL 恢复套件。

**T7 连接池饱和**（资源上限）：5 连接全忙时新操作最多等 5s → StorageUnavailable(uncertain=False) → 流式发 storage_unavailable 失败帧并终止、普通 defer 后收口；DB 不产生矛盾。【执行者已复现】pool wait 4.8-6s 单等待者场景；【待验证风险】多请求同时排队未专门测试，实现者已声明无压力验证。

**T8 关闭窗口**：shutdown 取消 reconcile → 1s 宽限 flush → TimeoutError 放行 → 未收口请求留 running → 下次启动 recover 兜底（不回调 Provider）。【执行者已复现】shutdown_while_offline、normal shutdown。

---

## 三、Findings

**F1（P3，静态确定）** — `src/services/models.py:145`
- 现象：`load_registry` 报错文案为 `'gateway.yaml 必须且只能包含 models、providers'`，而第144行条件实际允许顶层 `database` 键。
- 触发条件：操作者在 gateway.yaml 写入任何非法键（如 `databases` 笔误）。
- 影响：错误信息漏报当前合法的 `database` 段，误导排障；无功能与安全影响。
- 违反契约：F5-O1 设计将 database 配置并入 gateway.yaml，文案未同步。
- 最小验证：以含额外键的 YAML 调用 `load_registry` 读报错文本。
- 修正建议：改为类似 `'gateway.yaml 必须包含 models、providers，且只能包含 models、providers、database'`。处置：随下一 Feature 顺手修复即可。

**F2（P3，静态确定）** — `src/api/sse.py:47`
- 现象：最终空传输帧（`more_body=False`）发送失败的 `OSError/TimeoutError` 被静默吞掉，未记录 `delivery_error_code`。
- 触发条件：终端事件帧已发出后、空帧发送前客户端消失，或 `remaining` 检查通过后 send 跨过截止点。
- 影响：仅诊断缺口——常见断连路径会被中间件 `http.disconnect` 分支先记 `client_disconnected`；未覆盖的是非断连 OSError 与最终帧超时窗口。DB 真相不受影响（completed 不可覆盖的契约仍满足，第41行注释亦正确声明空帧不是成功信号）。
- 违反契约：O1 设计"发送失败不覆盖 completed，投递错误单独记录"——单独记录在窗口内缺失。
- 最小验证：阻止最终空帧 send 并断言 trace.delivery_error_code。
- 修正建议：`except (OSError, TimeoutError): self.execution.trace.delivery_error_code = 'client_disconnected'`（按异常类型区分）后再 pass。处置：随下一 Feature 修复。

**F3（P3，静态确定）** — `src/main.py:72-73`
- 现象：`flush_pending` 的非 StorageUnavailable 异常分支只记日志、不弹出 pending 条目 → 该请求每 1 秒重试一次并持续刷错误日志。
- 触发条件：静态推导下几乎不可达——`store.finish` 的 DB 故障均转为 StorageUnavailable；ValueError（终态事件名不匹配）在 flush 路径不可能发生（事件名恒为 `'response.'+计算出的 status`，且终态行在事件校验前提前返回）。属防御性分支的兜底缺陷。
- 影响：若未来 store 新增校验抛出新异常，该请求将永久重试 + 日志噪音；不影响当前验收场景。
- 违反契约：未违反已确认契约；违反"有界清理"的实现意图。
- 最小验证：静态推导已足够；若需实验，monkeypatch `storage.finish` 抛 RuntimeError 观察条目滞留。
- 修正建议：通用分支同样 `pending.pop(request_id, None)`（或标记后停止重试）。处置：随下一 Feature 修复。

**F4（P3，测试基础设施噪声）** — `tests/live/test_f5o1_acceptance.py:67` + `live-first.log`
- 现象：`record_property` 与 junit_family 'xunit2' 的 pytest 兼容警告（6 条）。
- 影响：无——我核对了 `live-results.xml`：6 个 testcase、0 errors/failures/skipped，properties（真实 request_id、actual_model、token 数、events 18/17）**均已写入 XML**，证据未丢失。
- 修正建议：在 pytest 配置固定 `junit_family`（xunit1/legacy）或接受该警告。处置：不改动证据归档，可选清理。

无 P0-P2。上列四项均为 P3，不阻断验收；建议处置如上，是否随后续 Feature 修复由实现者决定。

---

## 四、实际已读 / 未读 / 未覆盖 / 未重跑声明

**逐行阅读并核实**：`review-diff-r1.patch`（按 offset/limit 分段读完全部 hunk）；变更源码 `main.py`、`middleware/request_context.py`、`persistence/database.py`、`persistence/store.py`、`persistence/tables.py`、`migrations/env.py`、`migrations/versions/f5o1_001.py`、`services/gateway.py`、`services/streaming.py`、`services/trace.py`、`services/models.py`、`api/routes.py`、`api/errors.py`、`api/sse.py`、`api/dependencies.py`、`providers/base.py`、`providers/mock.py`、`utils/json_codec.py`、`constants.py`、`contracts.py`、`errors.py`、`settings.py`；配置 `compose.yaml`、`alembic.ini`、`pyproject.toml`、`.gitignore`；O1 测试 `tests/conftest.py`、`tests/postgres_support.py`、`test_f5o1_configuration.py`、`test_f5o1_persistence.py`、`test_f5o1_http.py`、`test_f5o1_process.py`、`test_f5o1_acceptance.py`；受 O1 影响的 F5 回归 `test_f5_gateway.py`、`test_f5_execution.py`、`test_f5_providers.py`、`test_f5_streaming.py`、`test_trace_integrity.py`、`test_f2_prompts.py`、`test_real_providers.py`；安全快照 `review-config-r1.yaml`；哈希清单 `review-files-r1.json`；四份运行证据 `frozen-full-regression.log`、`final-acceptance-critical.log`、`live-first.log`、`live-results.xml`（核对：1224 passed/14 skipped；308 passed；6 passed 且 XML 属性完整）。

**未逐行重读（以 review-files-r1.json 哈希清单 + 1224 回归通过为覆盖依据）**：未变更基线文件 `providers/deepseek.py`、`providers/openai_compatible.py`、`providers/factory.py`、`providers/streaming.py`、`services/prompts.py`、`services/token_budget.py`、`services/token_counters/*`、`services/validation.py`、`services/credentials.py`、`config/credentials.example.yaml`、`config/postgres.password.example`、`tests/f3_cases.py`、`tests/f4_cases.py`、`tests/prompt_cases.py`、`tests/fixtures/*`、`tests/smoke/conftest.py` 及全部历史 F3/F4 测试文件、`docs/progress.md`、`docs/test-plan.md`。

**按要求未读**：真实 `config/gateway.yaml`（以安全快照 `review-config-r1.yaml` 替代）、`config/credentials.yaml`、`config/postgres.password`、`.env`、`.claude`、`.venv`、任何缓存与全局配置；`evidence.json` 未打开（实现者声明经上述四份日志/XML 交叉核实）。

**未重跑**：本审查未运行任何测试或命令。全部运行证据由实现者产生（frozen-full-regression.log、final-acceptance-critical.log、live-first.log、live-results.xml 及 smoke/进程级测试），本报告引用的"执行者已复现"均指实现者既有测试证据，非本审查重跑。

**未覆盖/未验证项**：
1. 六例真实调用采集于三项后续修复（failed-Attempt error_code、cancel-then-reconcile、entry-rejection-log-failure）之前；修复未做真实重跑——但三项修复均有离线回归覆盖（真实 PG、真实进程/HTTP 语义），不构成关键验收证据缺口；
2. 无压力/生产并发验证（实现者已声明）；连接池饱和的多请求同时排队场景无专项测试（T7）；
3. 多实例/分布式按已确认范围排除，本项依赖"单实例单 worker"声明的正确性已核实，未验证未来多实例行为。

---

## 五、结论

三维审查完成：需求正确性（确认范围全部兑现、无越界实现、5 条实现者解释逐条反证成立）、实现一致性（快照=装配后有效输入、DB 约束与公共契约一致、测试替身未掩盖缺陷、唯一行为变化属已确认设计 3.4）、系统正确性（回归/并发/资源/取消/失败窗口/部署假设均经代码与执行者进程级证据核实）。独立推导 8 条时序无一条出现矛盾。P0-P2 为 0；4 项 P3 均为轻微问题（1 项错误文案、2 项诊断/兜底缺口、1 项测试噪声），处置建议如上，不阻断验收。

**VERDICT：APPROVED**（P0: 0，P1: 0，P2: 0，P3: 4）
