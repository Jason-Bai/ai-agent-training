所有证据与调用链核对完成。以下是第2轮完整审查报告。

---

# F5-O1 第2轮完整代码审查报告（独立审查）

**审查方式**：仅 Read；未运行命令/测试、未调用真实 API/MCP、未写文件。运行证据全部由实现者产生，本报告逐项标注来源；未读取真实 credentials.yaml、postgres.password、.env、.claude、.venv 及任何缓存/全局配置，配置仅用 review-config-r2.yaml 及无密钥示例。

## 〇、变更证明核对

r1/r2 两份 95 文件摘要逐项比对：**仅 6 个文件哈希变化**（src/api/sse.py、src/persistence/database.py、src/services/models.py、tests/unit/test_f5o1_configuration.py、tests/postgres/test_f5o1_persistence.py、tests/integration/test_f5o1_http.py），与 130 行 review-diff-r2.patch 逐 hunk 对应；当前磁盘内容与差分后状态一致。其余 89 份摘要与首轮相同，复用首轮实际 Read 覆盖（72 次 Read/56 文件，review-diff-r1.patch 3999 行无缺失）。首轮审计记录中的两处报告自述出入（AGENTS.md、postgres.password.example 未被单独 Read；tests/smoke/conftest.py 实读却列为未读）与 test-plan.md 3.10 披露一致——本轮我已补读这两份文件（docs/AGENTS.md 经本轮 hook 上下文、postgres.password.example 直接 Read）。

## 一、五项修订的独立结论（不按实现者解释通过）

**A. 密码解析（database.py:34）— 核实通过。**
- 协议核对：`read_bytes().decode('utf-8').rstrip('\n')` 只剥末尾 LF。与 Docker 侧约定一致：compose 用 `POSTGRES_PASSWORD_FILE`，postgres 官方 entrypoint 以 `$(< file)` 命令替换读取——同样只剥尾随换行、保留空格与 `\r`。因此前导/尾随空格、CRLF、无尾随换行（测试夹具 write_text 无 `\n`，见 postgres_support.py:22）三种文件形态下，角色密码与 Gateway 侧取值**逐字节一致**，不存在第二处认证分叉。
- 真实反例：test_f5o1_persistence.py:53-76 现在参数化 `'a:@/?# %特殊字符'` 与前导尾随空格变体；以 `sql.Literal` 建角色、独立 psycopg 对照连接断言 `current_user` 精确回环，再断言 Gateway 引擎 `SELECT current_user` 同角色——若 SQLAlchemy/psycopg 链路丢失空格即认证失败。Red 日志证实修复前该变体以 StorageUnavailable 失败、修复后通过。
- 安全路径保持：缺失/不可读（OSError，含 PermissionError）、纯空白（strip 后空 → ValueError）、非法 UTF-8（UnicodeDecodeError ⊂ UnicodeError）均落 ConfigurationError，不泄漏密码内容；`hide_parameters=True` 与 URL 掩码不因改动退化。
- 边界说明（非缺陷）：密码本身以换行结尾的意图无法表达，这是文件约定的固有属性，且两侧（compose/应用/Alembic 共用同一 database_url）一致。

**B. 配置文案（models.py:145）— 核实通过。** 文案与第 144 行白名单逻辑（必含 models/providers、允许 database、禁止其他）精确一致；新单测 `test_invalid_registry_root_explains_optional_database_section` 断言子串，Red 日志显示修复前失败文本为旧文案、修复后通过。

**C. 末帧诊断（sse.py:47-53）— 沿链核实通过。**
- 类型判断顺序正确：`TimeoutError` 是 `OSError` 子类（3.12），`isinstance(exc, TimeoutError)` 先行 → `request_timeout`，其余 OSError → `client_disconnected`。
- 持久化时序完整闭环（独立推导）：`run()` 的 finally（streaming.py:134）先 `defer(self.trace, ...)` 把**同一 trace 对象**排入 pending → sse.py 随后在空帧失败处写入 `delivery_error_code`（此时中间件 `await self.app` 未返回、active 仍含该 request_id）→ 中间件 finally 才 `active.discard`（request_context.py:86-88）→ 后台 flush 对 active 内条目一律跳过（main.py:49-50），下一周期经 `_persisted_terminal` 分支仅 `update_diagnostics`（main.py:67）→ `timing()` 序列化含 `delivery_error_code`（store.py:24-28,120），status/事件不动 → pop。**无漏记、无覆盖、无竞争**：defer 先于突变、flush 跳过 active、单 reconcile 任务串行。
- 反例核查：(1) `is None` 守卫避免覆盖 `run()` 已记诊断；(2) run() 内部已把 emit/start 的 OSError 转成 StreamFailure、内部 TimeoutError 自吞，故该 except 实际只命中空帧窗口，不干扰主链错误语义；(3) 空帧超时窗口（remaining 检查通过后 send 跨过截止点）由 `asyncio.timeout(remaining)` 覆盖；(4) `remaining<=0` 跳过空帧不记诊断——此时 completed 事件已送达、空帧明确不是成功信号（sse.py:41 注释），无诊断缺口；(5) CancelledError 属 BaseException 不落入 except，沿用中间件取消分支（request_context.py:67-75），修复未改变该路径。测试用独立 PG 连接轮询断言、send 先断言 `body == b''` 才抛错，非自我断言。

**D. flush 非存储异常分支（main.py:72-73）— 不采纳 pop 的判断核实成立。** 静态反例穷举：`finish` 的两个 ValueError 源在 flush 路径不可达——(1) 事件名恒为 `'response.failed'` 且 flush 构造的 status 恒为 failed（`error = trace.error_code or 'storage_unavailable'` 恒真）；(2) `input_snapshot is None` 的行只能是入口拒绝插入的**已终态**行（prepare 必写快照），会先在 line 97-98 短路返回。`update_diagnostics` 对行缺失直接 return、attempt 不匹配更新 0 行不抛错。数据库类故障全部经 database.transaction 归一为 StorageUnavailable。剩余仅防御性编程错误：保留条目+安全日志+重启 recover 兜底，优于 pop 丢失未收口线索；不引入范围外熔断/重试。判定：无当前可达反例，静态风险已如实标注。

**E. JUnit 警告（P3 处置）— 核实。** live-results.xml 6 个 testcase、0 errors/failures/skipped，properties（request_id/actual_model/协议/token/events 18/17）完整写入；后续命令已带 `-o junit_family=xunit1`（test-plan 3.2/3.10），不为报告噪声重复付费调用。处置合理。

## 二、三维覆盖表

| 维度 | 必查项 | 本轮结论 |
| --- | --- | --- |
| 需求正确性 | 用户原话需求（PG 替换、compose、不清理、tb_ 单数、BIGINT+UUID 分离、切回 Mock）| 全部兑现：compose 仅管 PG（127.0.0.1:15432）、无清理/TTL 代码、三表名合规、Identity(always=True)+Uuid unique、快照/事件先存后发、探测不入库、默认两条 Mock 在安全快照中 |
| | 设计一致性/越界 | routes.py 仅 4 个端点，无 202/GET/取消/重试/计费/查询接口或占位；六状态仅存于约束/recover 词汇；无过度实现 |
| | 设计交接不提前 | O2/F6 原则仅存于 design.md 5.2，无接口 |
| 实现一致性 | 输入/值/字节 | 密码按原字节保真；快照为装配后有效输入；JSON 经共享 codec 保精确十进制（H03 断言 SQL TEXT 与解码值）|
| | 错误优先级 | 入口拒绝 0 Attempt/0 调用；prepare 失败 503 于 Provider 前；上游错误不被改写为存储错误（H06）；completed 提交后发送失败仅记诊断 |
| | 测试替身是否遮蔽 | MockProvider 只替模型；提交屏障包真实 commit/rollback；独立连接观测（observe/pg_server.connect）非生产 Session 自证；conftest 哨兵阻止读真实配置（r1 已核，哈希未变）|
| 系统正确性 | 老功能回归 | 串行全量 1228 passed/14 skipped（182.63s）与首轮一致且含全部 F1-F5 套件；3.12 关键 312 passed（91.23s）|
| | 并发隔离 | active 跳过、单 reconcile 任务、行锁串行 seq（r1 已核，本轮相关路径未变）；新末帧路径不引入并发写入者 |
| | 资源/异常/时序 | 末帧失败无新任务泄漏（watcher finally cancel+gather）；5 连接池/无溢出/5s 预算/关闭宽限不变；update_diagnostics 幂等、不覆盖终态 |
| | 部署假设 | 单实例单 worker 声明一致；PG 必需无降级（create_app 恒构造 Database）；密码文件仅本机、chmod 600（夹具）、.gitignore 已含 |

## 三、关键独立反例与时序（本轮推导）

- **T-R2-1（C 修复主时序）**：defer → 突变 → active 释放 → flush 持久化的先后保证（见 §一.C），已静态闭环。
- **T-R2-2（密码三形态）**：LF / CRLF / 无尾随换行下 Docker entrypoint 与 Gateway 取值逐字节一致，无第二认证分叉。
- **T-R2-3（守卫必要性的反例）**：`_notify_failure` 发送失败返回 `client_disconnected` 而 committed_error 为上游码时，run() finally（streaming.py:100-101）已记诊断；末帧再失败时 `is None` 守卫防止用更晚错误覆盖首要诊断——守卫必要且行为正确。
- **T-R2-4（取消分支覆盖可能性）**：提交后清理期宿主取消 → run() 记 `internal_error` 后重抛 CancelledError → 中间件取消分支无条件写 `client_disconnected`（request_context.py:70-71）会覆盖该诊断。属 r1 已审的既有行为、r2 未引入；交付语义上宿主取消即投递中止，标签可接受，仅记录不列项。
- **T-R2-5（remaining<=0 跳帧）**：completed 事件已送达，空帧非成功信号，不记诊断正确。
- **T-R2-6（flush pop 后无二写）**：pending 键为 request_id，defer 幂等重入；`update_diagnostics` 重写 timing_snapshot 为同一 trace 属性的纯函数重算，无字段回归。

## 四、Findings

**P0：0，P1：0，P2：0。**

**P3-R2-1（本轮新增，文档同步，静态确定）** — `docs/features/F5/optimizations/O1/test-plan.md` 3.6 覆盖索引
- 现象：索引声称"下表为实际节点名"，但未包含本轮新增的两个节点 `test_invalid_registry_root_explains_optional_database_section`（U）与 `test_final_empty_transport_frame_failure_has_persisted_diagnostic`（H）；密码用例属既有节点参数化，无需列。
- 影响：索引与实际节点轻微出入，不影响功能与验收；3.10 已如实描述修复及对应测试。
- 修正建议：在 C01/S05 行补两个节点名。处置：随下次文档同步顺手补齐即可。

首轮 4 项 P3 处置经本轮独立核实全部属实：F1/F2 已修复并有有效 Red→Green 证据；F3 不采纳理由成立（无可达反例）；F4 证据完整、后续命令已 xunit1。原 review-r1.md 未被改写，与 3.10 的披露口径一致。

## 五、实际已读 / 复用 / 未读 / 未重跑

- **本轮实际 Read**：review-diff-r2.patch、review-config-r2.yaml、review-files-r1/r2.json、review-audit-r1.json、review-r1.md、review-r1-findings-red/green.log、review-r2-full-serial.log、review-r2-critical.log、live-first.log、live-results.xml、design.md、test-plan.md、implementation-summary.md、config/postgres.password.example、docs/AGENTS.md（hook 上下文）、根 AGENTS.md（系统上下文）、src/persistence/{database,store,tables}.py、src/services/{models,streaming,gateway,trace}.py、src/api/{sse,routes,errors,dependencies}.py、src/main.py、src/middleware/request_context.py、src/constants.py、compose.yaml、migrations/{env.py,versions/f5o1_001.py}、pyproject.toml、tests/postgres_support.py、tests/unit/test_f5o1_configuration.py（全）、tests/postgres/test_f5o1_persistence.py（1-120）、tests/integration/test_f5o1_http.py（1-314、560-950）。
- **复用首轮覆盖（哈希未变）**：test_f5o1_persistence.py 121-647、test_f5o1_http.py 314-560、conftest.py、test_f5o1_process.py、其余未变源码/测试/锁文件。
- **未读（按要求）**：真实 gateway.yaml（以安全快照替代）、credentials.yaml、postgres.password、.env、.claude、.venv、缓存/全局配置、evidence.json、review-session-r1.jsonl.gz（二进制压缩）。
- **未重跑**：本轮未运行任何命令/测试。运行证据全部为执行者回执，已逐份核对：修复前 4 failed/1 passed（三处目标失败均为有效 Red）、修复后同组+守卫 6 passed、串行全量 1228/14 skip、3.12 关键 312、live 六例 6 passed 且 XML 属性完整。此前并行回归的 1 fail（端口占用）已按方案串行复跑，不以之冒充通过。
- **未验证项（不构成关键证据缺口）**：真实故障路径（失败/取消/日志/密码/末帧）未发真实模型，由离线真实 PG/进程覆盖；无压力/生产并发与多实例验证；70 份历史/资产/凭据摘要未改为实现者自证。

## 六、VERDICT

需求正确性、实现一致性、系统正确性三维完整：六文件差分为全部实际变更，五项修订经协议、调用链与真实回执独立核实成立；独立推导时序无矛盾；无范围越界、无测试替身遮蔽、无关键验收证据缺口。

**VERDICT：APPROVED** —— P0: 0，P1: 0，P2: 0，P3: 1（本轮新增文档索引项；首轮 4 项 P3 处置已核实）。
