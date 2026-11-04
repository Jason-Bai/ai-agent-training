strict.xml 直接核对完成（errors=0 failures=0 skipped=0 tests=352），live-two.xml 直接核对完成（2 passed，无 errors/failures）。全部材料收集与核查结束，以下为最终审查报告。

---

# F5-O2 两阶段流式调用与断点续传 — 独立代码审查报告（第 1 轮）

审查方式：原生 Claude Code，仅 Read，不修改文件、不重跑测试、不调用真实 API、未读取任何真实凭据。审查对象为当前完整产品实现（项目目录）与材料目录差分/证据。

## 一、三维覆盖表及缺口

| 维度 | 已核查要点 | 缺口 |
| --- | --- | --- |
| 需求正确性 | POST 只预检+保存 pending、202 四字段 JSON 仅含 request_id/status 且零 Provider/Attempt/事件调用；首次有效 GET 条件认领 pending→running、提交确认后唯一后台生成、后续 GET 只读 PG；after_seq 严格大于/显式 0 合法/超 19 位保护→400；断连继续、多订阅隔离、按游标补读；B 从认领前起算覆盖认领/Provider/保存、重连不刷新；订阅独立 15s 心跳与每次 send 30s、无整体 GET 时限；TTFT 由首次实际成功发送补记且只一次、历史回放不改；重启 running/canceling→failed(execution_interrupted)、pending 保持、终态不变；无 F6 取消/F7 重试 fallback/F8 查询/F9 计费/多实例/清理/新增对外兼容接口；快照不保存 Provider Key（仅 credential_ref 引用名），review-safe-config 仅 Mock | 未发现范围外能力或需求遗漏；F6/F7/F8/F9 边界如实标注为后续项 |
| 实现一致性 | 创建/认领/增量/终态四阶段 commit/rollback ACK 丢失均按 DB 实际状态核对（database.py 的 committing 标志、finish 的 TERMINAL 早退、get_stream 复核）；PG 条件 UPDATE 原子认领单赢家；分页以已观测 last_event_seq 为上界（READ COMMITTED 下安全）、页上限 100、无长事务/SQL CURSOR；SSE id=seq、业务帧不重复 seq、心跳无 id 且不落库（DB CHECK 排除）；订阅错误仅本地诊断不污染生成 error_code/Usage；默认测试哨兵仍禁止真实凭据与非回环连接（conftest 74-113 行） | 发现 1 项 P3（见 Findings），静态确定，测试未覆盖 |
| 系统正确性 | 每请求认领锁协调事务与任务注册；20 并发订阅单生成；订阅任务/空闲时钟相互隔离；发送背压不预取；有界停机（订阅取消→生成 stop_error→上游 1s 清理→DB 关闭前 1s grace）；DB 离线仍释放连接池；孤儿 reconcile 失败不阻断池关闭；重启 recover 只收口 running/canceling；单实例单 worker 假设声明准确 | 同一 P3 窗口属于并发时序边界；无其他阻断缺口 |

## 二、独立反例与时序检查

审查中独立推导并逐一核对的时序/反例（标注判定来源）：

1. **P3-1 窗口竞争（静态确定）**：见 Findings。
2. 认领成功但订阅 HTTP 在 SSE 首帧前断开 → 生成照常运行、重连按 after_seq 补读 —— 设计与 process 测试一致，安全。
3. 两个 GET 竞争认领，胜者注册任务失败 → 败者的孤儿分支收口 failed，不重复调用 Provider —— `test_registration_failure_closes_coroutine_and_unowned_claim_fails` 覆盖（已读源码+测试）。
4. 终端后 GET 且 `after_seq == last_event_seq` → 204 无未读；`after_seq > last_event_seq` → 400 不启动生成 —— routes.py 逐字核对。
5. 订阅在 `closing` 期间请求终态回放 → 终态分支先于 closing 检查，回放不受停机影响 —— subscribe 顺序核对（streaming_manager.py:49-52）。
6. 生成成功路径在 `_emit` 内联提交终态后才结束任务，故 completed 行不受 P3-1 窗口影响 —— 静态确定安全。
7. `_task_done` 的 `trace.error_code or 'internal_error'` 只在任务被取消于首次字节码前（finally 未执行）时生效；正常完成/失败均已在 `_generate` finally 中出队，不会把成功污染为 internal_error —— 静态确定。
8. 慢订阅与快订阅并发、慢查询中心跳不启动第二个查询、空闲时钟独立 —— 源码核对 + `test_f5o2_subscriptions` 覆盖。
9. 停机时先取消订阅再取消生成、flush 与 reconcile 顺序（先 reconcile 后 flush）→ 双写路径收敛为同一终态 execution_interrupted、事件只写一次 —— main.py/manager.close 核对。
10. 创建响应发送失败/阻塞 ≠ 生成完成：创建 Trace 与生成 Trace 分对象，flush 对 creation 分支按 get_stream 复核、pending 保持 —— middleware/main.py 核对 + delivery 测试覆盖。

## 三、Findings

### P3-1（静态确定）：生成失败收口与孤儿 reconcile 的窗口竞争会覆盖真实错误码并丢失 Usage

- **位置**：
  - `src/services/streaming_manager.py:89-93`（subscribe 的孤儿分支：`if row['status'] in {'running','canceling'} and request_id not in self.tasks:` → `await self.storage.reconcile(request_id)`）
  - `src/services/streaming_manager.py:150-155`（`_generate` finally 先 `self.tasks.pop` 再 `self.defer`，终态由 `main.py` 的 flush_pending 延后提交）
  - `src/persistence/store.py` 的 `reconcile/_recover_row`（running → failed `execution_interrupted` + `response.failed` 事件，attempt 置 failed 但不写 Usage 列）；`main.py` 的 flush_pending → `finish` 对 TERMINAL 行早退
- **触发条件**：生成以失败结束（`request_timeout`/Provider 错误/`internal_error`/`storage_unavailable` 等），`_generate` finally 已从 `self.tasks` 移除任务（行 152），但 flush_pending 尚未提交终态（看门狗约 1 秒间隔，DB 慢时更长）。此时任一 GET 到达：`get_stream` 看到行仍为 running，孤儿分支命中（任务不在 `self.tasks`），调用 `storage.reconcile` 抢占写入终态。
- **影响**：真实错误码被替换为 `execution_interrupted`，`response.failed` 事件内容为 reconcile 版本；随后 flush 的 `finish` 因 TERMINAL 早退，attempt 已观测 Usage 永不落库（reconcile 无 Usage 数据）。终态仍唯一、无重复 Provider 调用、无状态分裂——因此为 P3 而非 P2。
- **违反契约**：「生成所有者写入唯一终态」在窗口内由订阅侧 reconcile 代替完成；「数据库结果为准」的错误码/Usage 保真度下降。
- **最小验证**（需执行者运行回执）：在集成测试中让生成以已知码失败（如 MockProvider 注入 `request_timeout`），等待 `_generate` 任务完成（`request_id not in manager.tasks`）后在 flush 前直接调用 `manager.subscribe(request_id, 0)`；观察行变为 failed/`execution_interrupted`、事件为该码、attempt Usage 为 NULL；随后运行 flush_pending，确认不再覆盖。预期与上述静态分析一致。
- **建议方向**：flush 提交前保留所有权标记（如终态待提交集合，subscribe 孤儿分支先查询该集合），或让 flush 对 reconcile 已写终态的行走既有 post-terminal 诊断/Usage 补写路径（`update_diagnostics` 机制已存在）。是否修复由执行者按 P3 处置规则记录。

等级汇总：**P0=0、P1=0、P2=0、P3=1**。

## 四、实际阅读与未读/未重跑边界

**已读**：设计/测试方案/实施步骤/代码导读四份文档；changes.json 与 evidence-summary.md；16 个修改文件的全部 diff；新增模块完整源码（streaming_manager.py、subscriptions.py、api/sse.py、middleware/request_context.py）；主链完整源码（main.py、persistence/store.py、persistence/database.py、services/streaming.py、services/gateway.py、api/routes.py）；未修改依赖（contracts/errors/tables/dependencies/providers/base/factory 等）；优先测试文件全部（faults/delivery/http/replay/metrics/lifecycle/storage/subscriptions/process/live）与历史测试 diff；6 份公共文档 diff；review-safe-config.yaml；证据文件 evidence-summary.md、live-two.xml（2 passed、0 errors/failures，request_id/Usage/事件数与 test-plan 记录一致）、strict.xml（352 tests、errors=0 failures=0 skipped=0）。

**未读/未重跑**：full-final.xml 为 56k tokens 单行文件未整读（1326 数字依据 evidence-summary + test-plan 记录 + 已直接核对的 strict/live junit 交叉印证）；未读 evidence.json 归档、protected-check.json、full-final.log/strict.log 原始输出；**未重跑任何测试**，两条真实调用证据来自执行者回执（live-two.xml 已核对，非本审查产生）；未触碰 `.env`、`.claude`、`.venv`、缓存、`config/credentials.yaml`、`postgres.password`。

## 五、VERDICT

**APPROVED**（第 1 轮）。三维审查无 P0–P2 阻断项、无关键验收证据缺口；P3 一项已给出最小验证与处置方向。三项已通过证据（full-final 1326/16 live skipped、strict 352、live-two 2）与文档记录一致，历史测试迁移经核对为真实语义更新而非断言删除。
