第2轮完整独立审查结论：**APPROVED。首轮3项P2均已闭环，本轮未发现新增P0–P3问题。**

### 三维审查覆盖

| 维度 | 本轮核对结果 |
| --- | --- |
| 需求正确性 | 复用首轮已核实的需求与设计，重新核对三项修复。Provider单次期限、请求总期限和旧pending兼容均恢复到已确认契约，未扩大恢复次数、输出授权或功能范围。 |
| 实现一致性 | 检查全部R1→R2差分、12个新增回归节点及受影响调用链。计时调整保留完整流生成上限；失败保存超期转延迟持久化；旧快照补默认后仍拒绝真实配置变更。 |
| 系统正确性 | 重新推导取消准入、总期限、终态提交、资源清理、旧快照恢复和历史回放的竞争时序。结合未变代码与相关回归，未发现修复引入的新执行、重复事件或Attempt历史改写问题。 |

### 首轮问题复核

1. **流式模型期限包含登记和最终保存：已解决。**

   [streaming.py:398](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming.py:398)在实际启动Provider时建立固定截止点，[第357行](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming.py:357)用于完整事件读取；终态保存移到该计时之外，仍受请求总期限控制。登记慢、准入慢、最终保存慢的三条原失败场景转绿，真实生成超过模型时限仍失败。

2. **普通失败终态保存脱离总期限：已解决。**

   [gateway.py:46](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/gateway.py:46)沿用原deadline，同时约束等待与数据库事务。超期返回`504/request_timeout`并转交延迟保存；新增回归核对了响应耗时、实际取消、传入deadline、Request最终错误，以及Attempt仍保留原`provider_auth_error`，未重新调用模型。

3. **F6 pending快照被误判配置变更：已解决。**

   [streaming_manager.py:238](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming_manager.py:238)先通过`ModelConfig`补齐旧快照默认字段，再比较模型定义。新增七节点覆盖旧pending成功及失败时均单次调用、pending取消、终态回放，以及备用、预算、时限真实变更仍拒绝执行。

### 独立反例与时序复核

- **登记/准入耗时接近模型时限：** 此阶段尚未建立模型截止点，只消耗请求期限；实际启动仍检查请求剩余时间。
- **持续输出多个delta：** 模型截止点仅在启动时建立，未随delta或心跳重置；首delta提交屏障及取消检查保留。
- **模型已完成但最终保存较慢：** 保存不再误耗模型期限；请求总期限和事务终态屏障仍有效。
- **普通失败保存超期后后台补写：** 不再进入Provider，已结束Attempt保持原真实结果，Request由既有持久化所有者收口。
- **旧快照配上新默认恢复策略：** 缺少`recovery`的旧计划仍使用单次策略，不借升级获得额外尝试。
- **旧快照补默认掩盖配置变化：** 新增备用、预算或时限变化仍导致模型定义不等，不会放行新计划。

以上推导未形成新的Finding，本轮不需要追加实验。

### 证据与版本核对

已读取原始`review-r1.md`、更新后的实现导读、测试方案3.7及`evidence-r2.json`，检查三份源码和三份新增测试的完整差分，并重读相关计时、准入、保存和恢复代码。

独立核对结果：

- 当前冻结 **136份**文件全部匹配哈希，其中 **130份与R1相同**。
- 三份重建R1源码均匹配首轮记录SHA；归档差分与重建源码到当前源码的实际差分一致。
- 首轮证据文件SHA及74份受保护文件均未变化。
- 1776节点的选择集合与未选择集合互斥且完整；新增12节点均纳入本轮运行。

本轮执行者原始回执为：

`uv run --python 3.12 python -W error -m pytest -k 'f3 or f4 or f5 or f6 or f7' -q`

**1463 passed /16 skipped /297 deselected，exit 0。** 已复核原始输出及Red→Green记录。

未重跑297节点分布于输入/Prompt、普通HTTP、适配器、启动配置等十份测试文件，文件摘要均与R1一致。结合本轮三份源码的实际影响、已执行相关回归及首轮覆盖，证据复用充分；不将首轮1748通过描述为修复版全量结果，也不累加批次。

本人未修改文件，未运行测试、实验、应用、迁移或API；未读取凭据、密码、`.env`、`.venv`或缓存。只读仍为任务约束。旧快照兼容证据采用已核实的F6存储形状与真实PG，未运行旧二进制；付费供应商、生产容量、多实例及远端计费停止仍未验证，不属于本项验收声明。

**VERDICT: APPROVED — P0: 0，P1: 0，P2: 0，P3: 0。**
