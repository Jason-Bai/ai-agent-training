# F5-O2 累计第2轮：独立审查材料

你是全新上下文审查者，没有参与实现。用户本次明确同意将“必须Claude Code”改为“全新、不继承实现对话的审查Agent”，并应用于本项。历史第1轮已执行，此轮累计2/最多3；更换工具不重置。只读是任务约束，当前工具能力没有平台级只读封锁，请如实区分。

项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway
材料目录：/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2
范围：本机/隔离开发网络，单实例单worker，多个内部Agent及订阅并发。POST /v1/llm/stream保存pending+快照、202返回ID且不调用Provider；首次有效GET /v1/llm/stream/{request_id}/events才唯一启动。after_seq可省略为0，PG按seq分页，空页100ms等待、页上限100，SSE业务id=seq，心跳无id；断连继续生成，多订阅隔离；生成B总期限与订阅15秒空闲/单send30秒分离；快照与当前资源匹配、真实发送TTFT只一次；各阶段未知提交、停机/重启、安全错误/已知Usage保留。普通/Structured Output/探测不回归。
排除F6取消/F7重试fallback/F8公开查询/F9计费、多实例、清理和新增外部Chat/Responses接口。不要以未来能力缺失阻断本项，也不得忽略当前受影响行为。

用户原文摘录，来自当前对话，并非完整聊天导出：
- “每次先POST获取ID，再GET订阅”；“请求GET才开始，after_seq是可选参数”。
- “首次订阅可以不传after_seq，只有断点续传才需要”。
- “SQLite换成postgresql”“使用docker-compose来管理postgresql”“暂时不要清理动作”。
- 重启running/canceling→failed；pending保留；终态互斥；取消单独F6。
- 最新审查方案选择：“改用全新审查Agent，并更新工作流（推荐）”。
具体已确认设计与32组验收要求在下面文档，不按实现者推测补充需求。

审查依据（这是材料清单，不是预设通过结论）：
1. 项目AGENTS.md、docs/AGENTS.md及docs/templates/独立审查提示词模板.md：当前工作流/三维/等级/三轮规则；旧Claude模板只作为可选工具保留，历史验收事实不应被改写。
2. /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/optimizations/O2/design.md：已确认业务边界与契约；/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/optimizations/O2/test-plan.md：前两节为32组验收方案，后续为执行者产生的原始回执及版本边界。公开代码导读implementation-summary.md也须与实际代码相符，但不能作为正确性的证明。不提供实现者私有推理或预设通过判断。
3. /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/changes-current.json：全部源码/测试变更及本次工作流文档。/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/baseline/是真实实施前代码复制；/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/diff-current/为对该基线的逐文件差分；新增文件直接读取当前代码。另有/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/delta-r2/三份文件对第1轮版本的实际差分，原版本由Read结果/真实patch还原并与原摘要匹配；不能把这份差分当作完整范围。
4. /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/pre-review-product-r2.json：当前101份源码/测试/迁移/安全配置摘要。config只允许读/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/review-safe-config.yaml（默认Mock），禁止读取真实credentials.yaml/postgres.password/.env/.claude/.venv及缓存；不访问网络/API，不运行pytest或任何修改/实验命令。可用rg/cat/sed或Python解析本地证据，属于只读查阅。
5. 原始证据：/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/full-revised.log及full-revised.xml，/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/recovery-fixes-green.log，/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/strict.log，/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/live-two.log及live-two.xml。可用Python只读解析XML，避免一次输出整份巨型单行。/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/verification-summary.json是解析摘要，不代替必要的原文核对。/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/optimizations/O2/evidence.json归档原始输出，较大，按需读取。

执行回执来自执行者，不是你重跑：修订后全量1330passed/16live skipped，exit0；本轮相关严格41passed，exit0；首轮宽范围严格352passed是较早版本。真实两次Chat/Responses各1，采集于本轮三个源码修正之前，两条同DeepSeek服务，不是两家独立供应商；其后Provider/预算/事件消费实现未改，未追加付费样本。不同版本结论不得混用。已确认32组仍须对照真实断言/真实PG/TCP/副作用，不能以通过数替代行为判断。

请自行沿当前完整调用链审查，使用必要上下文而非机械重复全仓读取：routes/contracts → gateway/main/middleware → streaming_manager/streaming → Store/Database → subscriptions/sse，以及对应测试。特别查看新tests/unit/test_f5o2_openapi.py与tests/integration/test_f5o2_recovery_claim.py、之前的故障/进程/订阅测试；这些测试本身也待核对，独立提出未覆盖反例。旧Feature对本次入口变化的迁移不能删除原副作用与错误/Usage断言。

三维：需求正确性；实现一致性（实际字节、状态、错误、实际Provider/SQL副作用）；系统正确性（并发隔离、所有权/资源生命周期、老功能回归、停机恢复）。是否有P0-P2由你独立判断，不继承旧审查结论。没有必要为凑数量提出问题。普通未验证/范围外事项如实说明，关键验收证据缺口应阻断。

直接返回中文原始报告，不写文件：三维覆盖与缺口；独立反例/时序及静态或既有运行证据；P0-P3 findings（文件行、触发、影响、违约点、最小复现/建议，区分静态确定/执行者已复现/待验证）；实际阅读与未重跑边界；VERDICT APPROVED/BLOCKED及等级计数。需要实验请给出最小步骤由执行者运行。你的工具仍有执行/写入能力，但本任务只允许只读查阅；报告中列实际使用的只读命令种类，不声称平台已强制限制。
