第1轮完整独立审查结论：**BLOCKED，3项P2。** 三项均由独立反例推导，根执行者运行最小实验后，我已读取脚本、原始输出和exit 0回执复核。未发现P0/P1；未另列P3。

### 三维审查覆盖

| 维度 | 审查内容与结论 |
| --- | --- |
| 需求正确性 | 核对用户约束来源、F7完整设计、52组测试方案及F6取消契约。共享次数、预算授权、一次格式修复、首delta提交屏障及单实例范围基本一致。模型单次期限与请求总期限的实现存在两处偏差。 |
| 实现一致性 | 沿输入→候选装配/计数→恢复决定→Provider→Attempt→Request/事件检查。确认普通及流式共用恢复决策、逐候选预算、增长预算沿用、修复后禁止派生恢复、Usage未知传播。发现流式单次计时错误及普通失败收口超期。 |
| 系统正确性 | 检查每次调用的取消准入、提交未知、清理所有权、延迟保存、迁移、pending还原、重启和订阅链。未发现取消或首内容屏障被恢复绕过的额外问题；发现F6 pending快照兼容回归。 |

### Findings

**1. [P2] 流式模型单次期限错误包含Attempt登记与存储操作**

位置：[streaming.py:263](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming.py:263)，关联`_single_attempt`第333–351、386行。

`asyncio.timeout(limit)`包住整个`_single_attempt()`，因此`begin_attempt`、取消准入查询及完成提交也消耗候选模型的单次Provider期限。普通路径则在登记后重新计算Provider期限。

触发条件：备用模型时限小于请求剩余期限，Attempt登记/准入稍慢。实验中请求期限5秒，备用模型时限1秒，登记后等待0.7秒，Provider只需0.5秒生成；实际Provider运行 **0.2908秒**便被取消，Request为`failed/provider_timeout`，共3个Attempt，事件仅`response.failed`。

这违反设计3.2“单次Provider尝试最多min(模型时限、请求剩余时间)”的计时对象，错误拒绝本应成功的请求。

最小验证即上述输入。修正建议：登记与存储受请求总期限控制；在实际Provider准入之后建立固定的Provider截止点，覆盖整段生成，不将最终持久化计入模型时限。补充登记慢、准入慢、终态提交慢的边界测试。

**2. [P2] 普通失败终态保存脱离请求总期限**

位置：[gateway.py:44](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/gateway.py:44)，第46行调用`storage.finish`；对照第53–54行成功路径。

`_complete`抛出非恢复错误后，其总期限上下文已经退出；`complete`在异常分支直接等待`storage.finish`，既不传`deadline`，也没有`timeout_at`。因此失败响应可额外等待完整数据库操作时间。

实验输入：请求期限1秒，Provider立即返回`provider_auth_error`，终态保存前等待1.3秒。实际响应耗时 **1.3356秒**，仍返回`provider_auth_error`；Provider调用1次，Request及Attempt均保存为该错误。

这违反设计3.2总期限包含持久化的约定，也与成功终态保存的行为不一致。

最小验证即上述故障注入。修正建议： admitted请求的失败终态保存沿用原截止点；期限耗尽时返回`request_timeout`并转交已有延迟保存所有者，同时保留Attempt真实错误。补充与现有成功保存超期测试对应的失败路径测试。

**3. [P2] 未改变配置的F6 pending流式请求在升级后无法执行**

位置：[streaming_manager.py:235](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming_manager.py:235)。

F6基线`_snapshot`保存旧`ModelConfig.model_dump()`，其中没有`fallback_models`。F7当前模型序列化包含默认`fallback_models: []`，这里直接比较原始字典，必然判定不相等。后面对旧快照缺少`recovery`的兼容分支因而不可达。

实验保持无备用的模型配置不变，将pending快照还原成F6字段形状；首次GET得到 **`failed/execution_configuration_changed`，Provider 0次、Attempt 0条**。已核对F6安全基线源码，构造形状与其保存逻辑相符。

这破坏旧pending请求的继续执行能力。现有P6测试仅保存普通Request/Attempt；未验证迁移后旧pending流的实际GET行为。

最小验证：F6创建pending→显式升级F7→不改配置→首次GET。修正建议：按快照版本或缺失字段规则规范化旧模型定义后比较，保留真实配置变更的拒绝能力；同时验证旧pending执行、取消、终态回放以及真实新增备用配置仍被识别为变更。

### 其他独立反例及核对结果

- 推导“第一次delta实际提交但ACK丢失→未发送→恢复重发”：`storage_uncertain`及存储错误停止恢复；已有真实commit/rollback证据支持，未发现穿透。
- 推导“第二/第三Attempt登记后，取消已提交但ACK未返回→Provider启动”：调用准入复用请求锁并读取PG状态；已读竞争测试支持该边界。
- 推导“增长后网络失败切备用→预算偷偷恢复initial”：共享`current_output`保留增长值，候选按完整授权预检。
- 推导“修复失败后进入网络重试/fallback”：`repair_used`优先停止；未发现新增恢复链。
- 推导“取消退避→改写此前失败Attempt”：取消和通用收口仅更新仍运行的Attempt，已结束历史保持。
- 推导“并发请求共享客户端→次数、等待域、Usage串扰”：恢复状态按执行对象持有，Usage按request_id聚合；未发现共享状态污染。

### 实际读取及证据边界

已读取项目与docs的AGENTS、review brief、F7设计/测试方案/实现导读、F6设计，以及README/contracts/architecture的相关增量；检查全部50份源码、测试、安全配置实际差分及受影响调用链，包括Gateway、StreamingExecution/Manager、Provider适配/解析、Store/Database、迁移、Middleware、订阅与路由。

已分段提取`evidence.json`中的基线、清单、差分、52组映射、Red/Green输出及最终回执。最终全量原始记录为 **1748 passed /16 skipped，exit 0**；这是执行者产生的证据，不能覆盖上述新反例。

安全基线223项、当前冻结133项均存在并匹配记录哈希；审查结束时冻结133项和受保护74项再次核对无变化。

新增实验原始材料已复核：

- `/tmp/f7_review_r1_experiments.py`及`review-r1-experiments.log`、receipt。
- `/tmp/f7_review_r1_failure_deadline.py`及`review-r1-failure-deadline.log`、receipt。

本人仅进行只读检查和哈希核对，**未修改文件，未运行测试、应用、迁移或API**；只读是任务约束，并非工具强制限制。未读取真实凭据、密码、`.env`、`.venv`或缓存。未重跑历史全量，也未验证付费供应商、生产容量、多实例或远端停止计费；这些不作为本轮阻断项。

**VERDICT: BLOCKED — P0: 0，P1: 0，P2: 3，P3: 0。**
