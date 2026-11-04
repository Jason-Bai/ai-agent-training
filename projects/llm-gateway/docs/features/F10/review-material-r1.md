# F10 第1轮独立审查材料

本文件由执行者整理，设计/说明/通过数量均待审查，不是预设通过结论。审查者不继承实现对话。

## 用户需求与确认来源

F10基于README总验收功能规划：验证调用方使用闭环、故障和账务一致性、并发/持续运行及完整历史回归；设计和测试方案分别经用户确认。原始整段讨论未归档为独立转录文件，范围以本节执行者转述及design/test-plan为可核实材料；不得假称完整原始转录可用。部署限定本机或隔离网络、单实例单worker，两内部Agent，持久化PG，不扩展多实例。

以下为当前会话可获得的用户原文摘录：
- “我补充一下，要是用真实的模型提供商，暂时不考虑费用问题；确认”。
- 模型质量补充确认：问题说明容量必须验证Gateway转发/终态/账务，若证实上游原样短答单列质量异常；用户回答“采用分层验收口径（推荐）”。早期两个单字符“订”没有原始上游，归因未知，不可回溯断言供应商故障。
- 首轮最终持续14416笔有8笔provider_unavailable，原严格零失败断言失败。提问原文：“真实持续运行中，14416次请求有8次供应商不可用，其余成功，所有请求的终态与账务均一致。F10 是否按‘Gateway 正确处理上游失败，报告真实端到端成功率’验收？若仍要求全部请求成功，则当前这轮不能通过。”用户明确回答“按正确处理验收并报告成功率（推荐）”。
- 用户此前明确AGENTS.md修改必须经其确认，本项没有修改任一AGENTS.md；默认开发Mock绑定及凭据不动。

## 已确认设计与当前验证来源

必读 /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F10/design.md、test-plan.md、implementation-plan.md、implementation-summary.md、acceptance-guide.md、acceptance-report.md。设计和测试方案仍可被质疑，检查代码与测试是否共同误解用户。新增29项，其中12项live；原2010项保持，最终完整2039 passed含真实28例、无失败/跳过/筛选，由主执行者运行，审查者未运行。命令和原始回执见 evidence-final.json、evidence-final-all.log、evidence-final-all.xml.gz。完整容量数据 evidence-final-capacity.json.gz，原始Usage独立核对 evidence-raw-audit.json，临时原始审计脚本 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/tmp.2OY5LIwhE1/build_report.py 可只读核验。

最终容量16935笔，四档60秒+最高符合标准20并发持续600秒；持续14439全部成功，其余有一次上游HTTP520，SQL/provider错误匹配且未知费用pending；实际成功率按每阶段列出。所有成功调用对照原始上游文本，不一致阻断；功能用例检查业务事实，容量质量单列。恢复配置总3次/同模型2次，无fallback_models；本轮实际每请求1次。两接入均DeepSeek，Chat/Responses是两种协议，不是两供应商；固定重复短任务和观测开销，不称生产SLA或极限。

确定性超时/畸形帧/存储故障/SIGKILL等复用历史协议/PG/smoke回归；F10新增真实CLI三种人工处置链。普通与流式预算拒绝在进入Attempt前。请求/事件/账本通过SQL和HTTP独立对照，原始使用量与账户已知合计另读留证核对；正式供应商账单/实付没有取得，明确未验证。

执行历史也需核实：首次完整2037通过1失败保存在evidence-first-full.xml.gz，未改写旧失败；一次执行命令漏xunit1导致765通过14 setup errors中止（evidence-junit-command-aborted.xml.gz），最小修复1通过后重新全量；早期观测Decimal序列化错误及Red/Green在evidence-investigation.json。不将污染运行计作容量证据。当前尚未经过F10完整独审，本次为第1轮。

## 真实差分及冻结材料

无Git基线，使用已有F9-B1安全快照 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/tmp.2OY5LIwhE1/base。真实差分 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/tmp.2OY5LIwhE1/review-r1/changes.diff，旧/新哈希 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/tmp.2OY5LIwhE1/review-r1/changes.json，源码/测试/安全配置清单 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/tmp.2OY5LIwhE1/review-r1/manifest.json。审查期间执行者不改源码，审后核对哈希。安全文档/证据另有 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/tmp.2OY5LIwhE1/review-r1/docs-manifest.json（生成后固定）。来源快照不含真实凭据或.env；只读取清单明列的gateway.yaml/prices.yaml，禁止打开credentials.yaml。

本项没有产品源码/迁移改动。新增 tests/f10_support.py、f10_metrics.py、live/test_f10_acceptance.py、live/test_f10_capacity.py、smoke/test_f10_fault_chain.py、postgres/test_f10_verification.py、unit/test_f10_metrics.py、unit/test_f10_observation.py。已有变更 tests/conftest.py（排除历史身份和数据库自动注入），README.md、docs/progress.md，及本F10目录所有新增文档/证据。新增文件直接读源码，已有文件读真实差分及必要上下文，避免一次巨型输出截断。

必要旧调用链：src/main.py、api入口、services/gateway.py及流管理/取消/恢复模块、providers/deepseek.py/openai_compatible.py/streaming.py/base.py、persistence请求/事件/账务、identity及admin；具体文件可用rg定位，按影响扩展，不要求无关范围重审。核对tests/live/test_real_providers.py、tests/postgres_support.py及使用的历史fixture，确认真实调用、隔离、观测替换点和故障来源。

## 审查输出及约束

按 /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/templates/独立审查提示词模板.md 完成需求正确性、实现一致性、系统正确性三维审查；独立推导反例，关注观察器是否掩盖失败、Provider流和原文关联、并发任务和资源释放、费用黄金来源、统计分母及证据边界。材料/源码皆待核实，不以数量或预设通过作结论。需要实验列明最小复现，由执行者运行并回传。

只审查，不修改文件，不运行测试/实验或真实付费调用，不读真实凭据/.env/.claude/.venv/缓存。通用工具权限本身未强制只读，此为任务约束，不能宣称工具层只读。返回逐维覆盖、关键反例/时序及依据、P0-P3具体文件行/影响/最小验证、实读/未读及证据来源，最后VERDICT APPROVED/BLOCKED和等级数量。缺关键证据不得APPROVED；普通范围外限制不虚增Bug。第3轮仍阻断需交用户，本次第1轮。
