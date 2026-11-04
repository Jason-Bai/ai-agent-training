# F6 第2轮独立复核材料

沿用同一`/root/f6_independent_review`，累计第2轮。继续按[首轮材料](review-brief-r1.md)的需求、部署边界、只读约束和三维报告格式审查，不重置轮次。原输入、报告、`evidence-r1.json`保持不变；本轮读取最终`evidence.json`及更新的测试方案/代码导读。无付费调用、AGENTS修改或新协议。

## 本轮变化与核实要求

| 首轮问题 | root处置与待核实点 |
| --- | --- |
| P2 接受取消已提交、ACK尚未返回时首次启动Provider | 新增`tests/integration/test_f6_admission.py`确定性回归，真提交分支Red为Provider calls1；真回滚分支保持原调用可继续。管理器`_admit_provider`复用取消接受的请求锁，锁内读实际状态并调用执行器`_start_provider`；后者让首个anext任务进入一轮调度后释放锁，不等待首段内容。请独立检查检查/启动之间是否仍有窗口，以及取消是否被首段等待阻塞 |
| P3 覆盖collection来源和数量陈旧 | 最终evidence保留historical_collection_source(87/1433)；当前source和sha指向最新final-collection.log(91个F6、1437总节点)，更正说明；映射追加端口、迟到终态与2个启动窗口分支，逐节点对照，不以旧摘要冒充当前 |
| P3 README当前使用说明否认主动取消 | 当前事件/三类终态/代码导读已同步取消；保留待独审状态，AGENTS和历史验收不改 |

R1→R2实际产品/测试差分只有`src/services/streaming.py`、`src/services/streaming_manager.py`及新增`tests/integration/test_f6_admission.py`，见evidence.round1_to_round2_delta。原R1源码由已保存真实差分重建并与R1摘要核对，非虚构基线。其他相关源/测试摘要可与evidence-r1比较复用；当前增加一个测试文件，最终安全源码/测试/迁移/配置114份，另有AGENTS前后保护。

文档本轮变化为测试方案、导读、设计实现说明、README与证据来源标签，以及本轮输入/报告归档。首轮代码行号属于其冻结版本，不覆盖原始报告。

## 实际证据与版本边界

- `/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/evidence-r1.json`：首轮原始冻结证据，不改写。
- `/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/evidence.json`：最终命令、原始输出、两个阶段真实差分、当前摘要及覆盖映射。
- root最小实验`review-r1-start-window.log`真实失败；正式`admission-red.log`为1失败/1通过；`admission-green.log`为60严格通过，包含2新分支及旧执行/管理器。
- 首轮全量1418/16跳过，随后迟到Trace修正对应142严格，均保留各自版本，不称当前源码的单次全量。当前受影响F5/F6全链路严格回归命令为`uv run --python 3.12 python -W error -m pytest -k 'f5 or f6' -q`，实际结果见测试方案3.7与evidence的review-r2-related.log。未改的普通/Schema/Prompt等源码复用已验证全量及摘要，不机械重跑无关用例。

root按本轮源码/测试在审查前保存快照，审查后核对。通用工具仍非强制只读，本轮继续仅阅读/静态提取，不修改、不测试、不访问真实API/凭据。所有测试结果标注执行者来源；如需新实验，给root最小场景。

## 结束条件

对三维最终完整行为负责，重点复核修复差分、首轮P2的事务真提交/真回滚、首段前取消、请求超时与取消、共享客户端、延迟保存及正常流式旧行为。可复用已核实且摘要未变的覆盖，不机械重新读全部历史。

返回三维覆盖、具体反例/检查结果、P0–P3 findings、已读/复用/未读和证据边界，最后VERDICT及数量。无P0–P2且无关键证据缺口才APPROVED。需要实验先请求，不凑问题数；第三轮仍阻断按工作流交用户，本轮不添加用户批准节点。
