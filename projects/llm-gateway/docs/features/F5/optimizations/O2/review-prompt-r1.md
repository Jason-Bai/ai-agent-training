你是 F5-O2 独立审查者。目标是发现遗漏并给出有依据的三维判断；设计、实现者解释和通过的测试都需核实，不预设实现正确。

任务：审查 projects/llm-gateway 的两阶段流式调用与断点续传完整变更。部署仅本机/隔离开发网络、单实例单worker，多Agent/多订阅并发。必须使用原生Claude Code，本次为完整审查第1轮，最多3轮。仅Read，不改文件、不运行命令或真实API，不读真实凭据/.env/.claude/.venv/缓存，不调用其他工具。不得读取config/credentials.yaml或postgres.password。安全配置只读材料目录的review-safe-config.yaml。

需求来源：以下为当前对话用户原文摘录，其余为执行者结合已确认文档整理，并非完整聊天导出：
- “我赞同你的将‘创建请求’和‘订阅事件’彻底拆成两个接口，每次先 POST 获取 ID，再 GET 订阅”
- 创建路径改为POST /v1/llm/stream，订阅改为GET /v1/llm/stream/{request_id}/events。
- “我的看法是请求GET ...才开始，after_seq是可选参数”及“首次订阅可以不传after_seq，只有断点续传才需要”。
- “Gateway每100ms根据游标读取最新的信息，然后发送给调用方...调用方断开，也可以保证任务不断”，随后确认推荐方案A。
- “SQLite换成postgresql”“使用docker-compose来管理postgresql”“我建议暂时不要清理动作”。
- 重启running/canceling→failed；pending保留，三个终态互斥；取消功能单独F6。
- 不新增/v1/chat/completions或/v1/responses，对外统一入口，二者仅上游协议。
- 用户已分别确认O2完整设计、32组测试方案及主备各一次真实调用，本轮最新回复“确认”。

已确认范围（仍请检查解释是否忠实）：POST完整预检后保存pending+冻结有效输入，202四字段JSON data仅request_id/status，0Provider/Attempt/events；首次有效GET条件认领pending→running且提交确认后唯一后台生成，后续GET只读PG分页(seq>cursor ORDER BY LIMIT100)，空页才100ms等待；SSE业务id=seq，data不重复seq，心跳无id不保存。断连继续/多订阅隔离，无第二事件缓存、无长事务。B从首次认领前计，涵盖认领/Provider/保存；订阅独立15s空闲心跳、每send最多30s、无整体GET时限。实际首段发送补TTFT只一次，历史回放不改。快照消息不重渲染，当前资源不兼容则0调用失败。创建/认领/增量/终态未知提交按阶段核对，不盲重调或改相反终态。后台异常、有界停机、强退恢复保持唯一终态。普通/Structured Output/探测不回归；无F6取消、F7重试fallback、F8公开查询、F9计费、多实例、清理或额外管理API。

项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway
材料目录：/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2
无Git，本轮代码基线是实施前实际复制的baseline/和baseline-manifest.json。modified文件的逐文件真实unified diff在diff/；新增文件直接读取当前源码，不做巨大合并差分，也不要再机械重读整段同一源码。changes.json枚举全部源码/测试变化。公共文档较早基线来自设计阶段快照，源码基线均为本轮实施前；原始聊天全文不在文件系统，缺口如上注明。

先读：
1. /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/optimizations/O2/design.md
2. /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/optimizations/O2/test-plan.md（包含32组实际节点映射、执行记录与边界）
3. /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/optimizations/O2/implementation-summary.md
4. /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/changes.json、evidence-summary.md、pre-review-product.json

再沿真实调用链审查当前完整产品实现：
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/routes.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/contracts.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/middleware/request_context.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/main.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming_manager.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/subscriptions.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/sse.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/persistence/store.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/trace.py、constants.py
既有未改依赖按调用链必要读取：services/gateway.py、persistence/database.py/tables.py、api/errors.py/dependencies.py、providers/base.py/factory.py。不要扩展读所有历史Feature或无关课程。

测试审查：对照changes.json新增文件、修改测试的diff及test-plan映射，核对替身边界/真实事务/TCP/进程/副作用/原始输出。新测试优先检查faults的四阶段commit/rollback ACK丢失、delivery的五发送位置与202窗口、metrics/replay快照/历史不变、Store认领和分页、Subscription时钟/隔离、process断连续传/重启/停启数据库、live明确2条。有必要独立抽查补读其他节点；全部变更范围必须纳入判断，实际没读的部分如实标明，不以文件数量代替正确性。
修改的历史测试重点看diff/tests/{integration,protocol,smoke,live}/，确认不是删断言掩盖回归。tests/conftest.py只扩大显式独立PG夹具排除名单，默认仍禁止实际密钥/非回环网络。

执行证据：由执行者运行，不是你重跑。/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f5o2-implementation-ohd3mzo2/evidence-summary.md列实际命令/退出码/版本，raw日志同目录；最终已归档/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/optimizations/O2/evidence.json（大文件，按需要读；勿机械一次全文造成上下文浪费）。full-final.log、strict.log、live-two.log及live-two.xml为最终原始回执。protected-check.json核对106份资产/历史/默认配置无改。真实两条同为DeepSeek服务的Chat与Responses适配，不等于两供应商独立容灾；真实快模型可能先结束，断连中继续由Mock+TCP屏障另证。受控单send预算证明总订阅可超过单send期限，不冒充真实等待30秒的实验。

三个维度：
1. 需求正确性：是否满足实际需求，有无遗漏/冲突/过度实现，警惕代码与测试一起实现错误理解。
2. 实现一致性：请求/返回/字节、错误顺序/副作用/Trace、并发首次启动、游标/终态、实际SQL提交与发送之间时序。独立推导已有测试之外的反例，不能只看测试通过。
3. 系统正确性：普通/结构化/探测回归，任务与HTTP生命周期隔离，共享状态和资源构造/释放，异常收口、停机/恢复、当前单worker并发及内存/连接上限。未来功能排除不自动构成缺陷，也不能掩盖当前范围内故障。

无需凑问题数量。需要实验时提供最小输入/操作/预期/观察项，由执行者运行并回传；静态疑点不写成已复现。P0密钥/越权/严重数据破坏；P1主链或关键安全边界失效；P2当前约定场景失效；P3非阻断轻微问题。完整审查最多3轮，不能通过重开会话重置计数。

最终报告用中文：三维覆盖表及缺口；独立反例/时序与静态或已有运行证据；P0–P3 Findings含文件行/触发条件/影响/违反契约/最小验证与建议，并标已复现(执行者回执)/静态确定/待验证；实际读了什么与未读/未重跑边界；VERDICT APPROVED或BLOCKED及各等级数量。无P0–P2且无关键证据缺口才可APPROVED。不要修改文件或实施修复。

公共文档变化也在changes.json的common_documents_modified，审查其逐文件diff及必要最终上下文（README、AGENTS、architecture、contracts、progress、test-plan），核对当前接口/目录/状态声明；不机械重读历史验收段落。
