VERDICT: BLOCKED

P0：0；P1：0；P2：1；P3：2。

本轮发现一个已由执行者 root 复现的取消竞争缺陷：数据库已经提交 `canceling`，但接受事务的确认尚未返回时，原生成任务仍能首次启动 Provider。其他已审查路径未发现额外 P0–P2。

### 三维覆盖

| 维度 | 审查对象与依据 | 结论 | 边界 |
|---|---|---|---|
| 需求正确性 | review-brief 提供的用户原文/确认摘录、设计、33组测试方案、HTTP与两条接入 | 单一 POST 发起及轮询、先 canceling 再清理、三类终态及协议无关取消符合需求；启动窗口不满足 L4/F3 | 未独立取得完整聊天导出；确认来源按材料注明 |
| 实现一致性 | 路由、契约、管理器、执行器、事务存储、Trace，以及全部新增 F6 测试 | 参数、四字段、原 ID、事务写入屏障、唯一终态、历史事件与重复取消总体一致；确认一个接受后启动 Provider 的窗口 | 测试结果均来自执行者；本人未运行测试 |
| 系统正确性 | 后台任务所有权、延迟保存、订阅、共享客户端、退出/重启、迁移、隔离夹具及旧回归证据 | 单实例单 worker 下的主要所有权和恢复保护成立；阻断缺陷发生在当前支持的单 worker 并发场景 | 未证明生产容量、多实例、远端停止计算或停止计费 |

### Findings

**[P2] 接受取消已持久化，但确认延迟期间仍可首次启动 Provider**

- 位置：[streaming.py:238](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming.py:238)、[streaming_manager.py:168](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming_manager.py:168)、[streaming_manager.py:192](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming_manager.py:192)。
- 分类：**执行者 root 已复现；本人已读取原始回执并静态核对原因。**
- 条件：生成任务已提交 Attempt，但尚未开始 Provider；`accept_cancel` 已提交 `canceling`，返回确认被延迟。
- 原因：执行器在首次启动 Provider 前只检查内存 `cancel_requested`；管理器只有在 `accept_cancel` 返回后才设置该信号。两者之间没有共同的启动协调边界。
- 影响：取消已经成为数据库事实后，仍可产生一次本应避免的模型调用。现有数据库屏障能阻止后续 delta 提交，不能阻止该调用启动。未证明实际供应商收费。
- 违反契约：设计以 `canceling` 提交为取消接受点；L4 明确要求“Attempt 建立后尚未发起模型调用”处取消时保持零调用；F3 要求处理确认不确定性时不得重新启动模型。
- 最小验证：实际提交 `begin_attempt` 后暂停其返回；实际提交 `accept_cancel` 后暂停 ACK；独立 SQL 确认 `canceling`，再释放原生成，保持 ACK 阻塞，观察 Provider 调用。
- 最小修正方向：将首次 Provider 启动与取消接受纳入同一个协调边界，使接受点已经获胜的生成任务不能越过启动边界；同时保留“接受事务确定回滚时原执行可继续”的行为。仅增加一个未协调的数据库查询仍可能留下检查与启动之间的竞争。
- 回归要求：补上上述组合的确定性回归，并保留原 L4、接受 ACK 真提交/真回滚、GET/取消竞争和共享 Provider 隔离检查。

root 原始回执：

```text
Before releasing begin_attempt: {"status": "canceling", "calls": 0}
While cancel ACK blocked: {"status": "canceling", "provider_calls": 1,
"local_cancel_signal": false, "attempt_statuses": ["running"],
"events": ["response.canceling"]}
Final: {"status": "canceled", "provider_calls": 1, "stream_close_calls": 1}
AssertionError: Provider started after durable canceling while accept ACK was delayed
```

来源：[review-r1-start-window.log](/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f6-implementation-e279n58o/review-r1-start-window.log)。执行者报告退出码为 1；这是缺陷复现失败断言，不是通过记录。

**[P3] 覆盖映射来源仍保留旧节点数量**

- 位置：[evidence.json:574](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/evidence.json:574)、[evidence.json:578](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/evidence.json:578)。
- 分类：**静态确定。**
- 问题：根层数量已为 89/1435，但 `source` 指向旧 collection，`f6_keyword_match_note` 仍称新增87、选择88。
- 影响：读者不能仅凭这些字段明确识别最终节点来源；不影响产品行为。
- 核对：本人从 `final-collection.log` 提取的89个 F6 节点与映射89个节点完全一致，无缺失或未知节点。
- 修正：保留历史来源，并显式增加最终补充 collection 来源、摘要及对应数量，更新当前计数说明。root 已说明拟采用该处置，当前冻结材料尚未修改。

**[P3] README 当前调用说明仍否认主动取消**

- 位置：[README.md:148](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/README.md:148)、[README.md:150](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/README.md:150)、[README.md:198](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/README.md:198)。
- 分类：**静态确定。**
- 问题：前部说明 F6 已实现、待独立审查，后部当前使用说明仍写“尚未提供主动取消”，终态指南仅列 completed/failed，目录导读又称主动取消仍为后续功能。
- 影响：当前使用说明互相冲突，遗漏 canceled 的调用方处理。
- 修正：同步当前 SSE 事件、三类终态和源码导读，同时保留“待独立审查、尚未验收”状态；无需修改 AGENTS 或历史验收记录。
- 验证：文本核对即可。

### 独立反例与核对结果

1. **Attempt 已提交 + 取消提交确认延迟 + 原生成恢复**：发现上述 P2，root 最小实验已复现。
2. **完成事务与取消事务竞争**：行锁和状态条件使数据库先提交者获胜；取消接受后普通 `finish` 返回 canceling，不能追加 completed/failed。已有双顺序测试对此有断言。
3. **旧 delta 提交与取消竞争**：同请求行锁串行分配 seq，`append_event` 仅接受 running；已提交内容保留，接受后的新内容被拒绝。不会把历史内容晚到误判为新提交。
4. **清理后终态保存失败，同时 GET/恢复到来**：取消所有者、`awaiting_persistence` 和 flush 保护避免通用恢复抢写；已有延迟保存与 Usage 测试支持此结论。
5. **取消调用方断连或并发重复 POST**：后台操作及 shield Future 不归 HTTP 任务所有；既有操作复用，重复轮询不重复停止。进程内与 TCP 测试提供执行者证据。
6. **清理任务抵抗取消、执行器迟到退出**：有界 wait、残留任务跟踪及已持久化终态保护避免虚报清理成功或内存终态倒退；已读取对应回归及 Red/Green。
7. **共享客户端与服务重启**：取消关闭本次 iterator；两条真实适配器的本地协议测试验证连接关闭及客户端后续可用，Mock/TCP 补充并发隔离；重启按 failed/execution_interrupted 收口，不重调模型。

### 已读范围与验证来源

已读取：

- review brief、设计、测试方案、导读、审查模板及项目/文档 AGENTS。
- 21份源码/测试/迁移变更的真实差分，全部新增 F6 测试和辅助实现。
- 管理器、执行器、Store、数据库事务、主程序恢复/退出、路由及相关契约上下文。
- middleware、订阅、SSE、两条实际 Provider、factory/models/Trace 的相关调用链。
- 当前 README、架构、契约、进度的相关内容，以及旧管理器和恢复竞争测试。
- evidence 的清单、覆盖映射、版本差分和关键原始运行回执；root 补充的启动窗口复现日志。

本人仅进行了文件读取、静态提取和摘要一致性核对：

- 最终113份 manifest 与当前文件一致。
- 全量版本到最终版本仅执行器取消分支和对应单元回归两文件变化。
- 内嵌日志 output 摘要均匹配。
- 最终89个 F6 节点与覆盖映射一一对应。
- evidence 中63份保护文件的前后摘要相同；不将此扩大为全部历史 JSON 工件或凭据均有实施前后证明。

未逐份重读全部旧功能测试及历史验收工件；未运行任何测试、实验、服务或真实 API，未读取凭据、`.env`、`.venv` 或缓存。通用工具具备执行/写入能力；本轮只读来自任务约束，不是工具层强制限制。

运行证据来自 root 和具名实施 Agent：

- 全量 **1418 passed / 16 skipped** 属于迟到 Trace 修正前版本。
- 最终 **142 passed / 7 deselected** 覆盖全部89个 F6节点及相关旧执行/管理器/停库路径。
- 最终 collection 为1435节点。
- 失败、中断、端口冲突和错误协议帧记录未被作为通过记录。
- 新启动窗口实验由 root 执行，已失败复现当前缺陷；既有通过数量不能覆盖该反例。

### 证据缺口与结论边界

当前关键缺口是上述启动窗口尚无修复及同场景通过证据，必须阻断验收。其余未做的组合测试不自动构成缺陷；本轮没有据此虚增问题数量。

没有实际供应商、远端计算停止/计费、多实例或生产容量证明，均属于已确认范围外。完整聊天导出和全部历史 JSON 的实施前摘要仍未提供，不据此作独立验证声明。

**最终结论：BLOCKED；P0=0，P1=0，P2=1，P3=2。** 修复 P2 并完成原场景及影响范围回归后，由同一审查者进行第2轮复核。
