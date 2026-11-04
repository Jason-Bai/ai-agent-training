你是F5完整第1轮的独立审查者。目标是发现实现者遗漏的问题，判断需求、实现和系统边界是否成立。设计与测试可能一起理解错需求，允许质疑设计；不预设实现正确，不要求凑问题数量。

一、执行边界
项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway
事项：F5 Streaming，完整代码审查第1轮。仅本机/隔离开发网络，单实例单worker，多个内部Agent并发。
沿用原生Claude Code既有配置，仅Read。不要修改文件、运行命令、发真实API，不读取.env、credentials.yaml、.claude、.venv、缓存或任何未列出的密钥配置。下面config/gateway.yaml是安全的两条Mock默认配置，允许读取。需要实验时提出最小输入、操作及待观察项，由执行者运行回传，不能声称自己重跑了测试。

二、用户需求来源（当前聊天摘录与转述分别标明）
直接摘录：
1. 用户：'我觉得这里不对：1. 要有一个总的timeout，整体超过就报超时 2. 如果是一个正常的streaming请求，llm gateway还要负责处理过程中(暂时没有内容返回)，每N(15暂定)秒给到调用方心跳检测'。
2. 用户：'我理解finish_reason是Chat Completions的方式，你需要也考虑Responses API方式，我觉得这里要做一下抽象，无关Chat Completions或Responses API，对于Gateway它只拿到一套统一，清晰的返回结果'。
3. 用户：'认可；不能出现的就不要在请求参数里'。
4. 用户：'Generation Time：这个是否也需要记录一下，这个用现有trace是可以反推出来的？'。
5. 用户要求必须原生Claude Code审查；三维为需求正确性、实现一致性、系统正确性，原提示中的“开发分布式”按本项单实例并发边界审查。
执行者转述与成文确认：分段讨论确认的SSE事件、B起点/真实send期限、统一内部事件、Generation来源、断连清理、背压、无重连/重试和四层测试已集中在design.md。当前聊天中用户在了解两轮设计文档检查后回复“确认”，随后在“是否确认按这份测试方案推进”后再次回复“确认”；前者确认完整设计（含1秒清理宽限等成文细节），后者单独确认51组具体测试方案。没有另行保存完整逐轮聊天原文，不能将上述转述当作完整逐字聊天卷宗。
范围：独立POST /v1/llm/stream，严格一条user/受控Prompt/预算，模型supports_streaming显式严格bool；统一ProviderStreamEvent（status/delta/usage/error_code，明确远端终态来源），真增量、15秒空闲心跳、固定总期限/背压/单终态/单流清理与最小Trace，真实两条协议接入。保留普通/Schema/健康探测。F6主动取消、F7重试/fallback、F8持久化、多实例1.7及事件重放不在本项；排除项不能掩盖本项已受影响路径的问题。

三、基线与实际变更
无Git。实现前67份源码/测试/安全默认配置复制在 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/baseline；review-diff-r1.patch是该基线与当前完整变更（含新增测试）的逐行diff，2277行，不是文件清单。只允许按需要读其src/和tests/或config/gateway.yaml安全文件，不读其中缓存。
审前当前76份源码/测试/默认配置摘要：/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/review-source-before.json（evidence.json也包含）。51份资源/历史验收/本机配置只做摘要保护，不让你读取受保护密钥；检查回执表明未变。变更默认YAML仅两条模型各新增能力字段，仍Mock。已有F1—F4历史工件未改。

四、运行证据与解释（由执行者产生，仍需核实）
最终frozen-full：1090 passed/8 live默认skip；Python3.12独立环境关键226 passed并把DeprecationWarning作为error；最新真实主Chat/备Responses各一次成功，input_delta均0，总F5云端调用4次（首轮2+实现修订后的最终2），不是SDK重试，也不是两家供应商。
这些数量不代替正确性。evidence.json保存54轮原始运行及历史说明，稍后补入文档核验；初期未按每次命令保存输入hash的事实如实注明，不能补造。最新真实后仅补同轮deadline测试，产品源码未变。frozen-full/py312-frozen/最新真实各自输入hash前后一致，前两者为当前全部76文件。
切片子任务曾只读发现nested Usage绕过、cleanup期间取消后Trace running、连续空事件饿死heartbeat等问题，执行者追加正确Red并修复；原文/计数在test-plan。它们是实施过程检查，不能替代你本轮独立审查。网络新增测试首跑Green与旧快照反证区别、collection/夹具错误均有说明。label same-turn-deadline-red实际PASS，是未复现风险的补验收，未作Red或据此修改产品。
实现者解释：自定义Response把真实ASGI send await交给执行器而非仅yield；一个pending anext跨心跳保留；终态尝试一次、清理独立1秒宽限，cleanup shield和同步finally记录；Generation区分远端terminal与网络故障合成failed。以上均是待核实解释，检查真实代码/时序，不默认成立。

五、必读材料
逐文件Read，避免大输出截断。review-diff分段读完整；evidence.json至少读取头部provenance/当前摘要与最终运行对应段，原始最新运行可直接读下列txt；无需逐条重读所有历史失败输出。源文件和新测试请完整读；大文件用offset/limit分段。请如实列未读/失败材料。
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/AGENTS.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/AGENTS.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/README.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/architecture.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/contracts.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/progress.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/design.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/test-plan.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/implementation-plan.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/implementation-summary.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/evidence.json
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/review-diff-r1.patch
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/live-release-results.xml
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/config/gateway.yaml
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/dependencies.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/errors.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/routes.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/sse.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/constants.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/contracts.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/errors.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/main.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/middleware/request_context.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/base.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/deepseek.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/factory.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/mock.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/openai_compatible.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/streaming.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/credentials.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/gateway.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/models.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/prompts.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/token_budget.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/token_counters/__init__.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/token_counters/base.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/token_counters/deepseek_v41.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/trace.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/validation.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/settings.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/utils/__init__.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/utils/json_codec.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/f3_cases.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/f4_cases.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/unit/test_f3_config.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/unit/test_f5_contracts.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/unit/test_f5_execution.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/integration/test_f5_gateway.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/protocol/test_f5_providers.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/smoke/test_f5_http_server.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/live/test_f5_streaming.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/integration/test_trace_integrity.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/integration/test_f3_gateway.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/integration/test_f4_gateway.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/protocol/test_f4_providers.py
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/baseline.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/route-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/cleanup-cancel-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/frozen-full.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/py312-frozen.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/live-release.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/pre-review-docs.txt

六、三个维度
1. 需求正确性：用户目标与当前设计/代码一致吗？是否遗漏、误解、冲突或范围外过度实现？不默认已确认设计就是正确答案。
2. 实现一致性：沿预检→HTTP/SSE→Provider→归一化→输出→Trace核对参数、原始类型/字节、次数、错误优先级、真实副作用及未知值。独立推导未被已有测试覆盖的反例/时序，检查Mock替换边界和断言是否共同掩盖问题。
3. 系统正确性：检查F1—F4回归、并发共享客户端/请求状态、资源构造释放、取消与异常传播、期限覆盖、终态竞争、缓冲/任务与证据边界。单实例仍有并发；不以未来多实例排除当前并发故障，也不要求提前做未来功能。

七、报告
- 三维覆盖表：对象/依据、结论和未覆盖/未验证项。
- 独立推导的关键反例/故障时序及检查结果，按风险选择，不凑数量。
- Findings按P0–P3：文件/行、触发、影响、违反契约、最小验证及修正。标明已复现（谁运行的回执）、静态确定（完整推导）或待验证风险；推测不能冒充复现。
- 已读/未读材料与验证来源，不将执行者的运行描述成自己重跑。
- VERDICT: APPROVED或BLOCKED；P0/P1/P2/P3数量与理由。APPROVED须三维完成、无P0–P2且关键证据齐全；允许无问题。普通范围外限制不自动阻断。
P0密钥泄漏/越权/严重数据破坏；P1主链/关键安全边界失效；P2本项约定行为不成立；P3不影响验收的轻微问题。代码/测试行数、读取文件数和通过数量不证明正确。
完成后直接返回报告，不写文件不实施修复。
