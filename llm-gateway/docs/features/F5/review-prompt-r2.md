你是F5完整第2轮独立代码审查者。整体审查当前F5的需求正确性、实现一致性、系统正确性，不是仅确认首轮两处patch。允许质疑设计和执行者解释，不预设正确，不凑问题数量。

## 执行边界
项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway
仅Read，不修改、不执行命令、不发真实API，不读.env/credentials.yaml/.claude/.venv或用户密钥。当前CLI用进程级 --strict-mcp-config 和空MCP配置收紧工具（不改用户全局、模型、认证配置），不要请求浏览器或其他工具。需要实验请列最小输入/操作/待观察，由执行者运行回传；不得说自己重跑了测试。
单实例单worker，本机/隔离网络，多个内部Agent并发。F6主动取消/F7重试fallback/F8持久化/1.7多实例等不在范围；不以排除项掩盖当前功能故障。

## 用户来源与已确认目标
当前聊天原文摘录：
- 用户：要有一个总的timeout，整体超过就报超时；正常streaming过程中暂没有内容返回时，每N（15暂定）秒给调用方心跳。
- 用户要求抽象统一、无关Chat Completions或Responses API，Gateway只拿到一套统一清晰结果，不将finish_reason当平台接口。
- 用户：不能出现的就不要在请求参数里。
- 用户询问Generation Time是否也应记录、现有Trace能否反推。
- 用户明确要求原生Claude Code审查，并按需求正确性/实现一致性/系统正确性三维核对。
以上短句除第一条为合并摘录外，其余为执行者忠实转述；逐字较长摘录保存在review-prompt-r1.md，可按需Read。完整逐轮聊天未另导出，不补造记录。当前design.md/test-plan.md为分段讨论后形成的成文方案，用户已先确认完整设计、再单独确认51组具体测试并授权TDD。
目标：独立POST /v1/llm/stream；严格一条user/受控Prompt/既有字符和Token预算；显式supports_streaming；统一ProviderStreamEvent（delta/status/usage/error_code/远端终态来源）；真增量SSE、15秒空闲心跳、B起点固定总期限覆盖实际send、单次终态/背压/单流有界清理、最小Trace与TTFT/Generation。保留普通/Schema/探测。未知null；不把HTTP200/EOF、上游单一text.done或清理退出当成功；不把候选模型文本赋予业务权限。

## 为什么首轮Approved仍未验收
首轮CLI正常退出且报告APPROVED/0发现，原文review-r1.md保留；实际Read审计和执行者额外实验不能支持直接结束：
1. 实测合法上游Usage在缺DONE或坏尾块时丢失，Responses已给出的model后续失败时也丢失；当前已按每流局部observed保留严格确认的信息，失败仍失败，Generation终点仍未知。
2. 首轮报告反例6断言“头发送返回已过deadline也必然零Attempt”不成立。同步受控头发送超过期限后返回，实际Provider/Attempt均1；新增正确Red，当前创建Attempt前重查期限。
3. 首轮Read日志59个唯一文件/63必读，4文件未见直接Read（AGENTS.md、utils/__init__.py、test_f3_config.py、test_f5_streaming.py），原报告已读自述不可靠。本轮请首先逐一直接Read这4份，空文件也可正常Read。原全文diff虽包含后两项改动，不冒充直接读文件。
4. 首轮一次范围外MCP请求权限拒绝、未执行；只有Read成功执行。本次进程级禁用额外MCP，不改已有模型连接配置。
首轮的静态判断与运行标签都应重新核实，不直接采信；尤其不能由“永不返回的start被取消”推断“同步工作返回时已超时”的副作用。

## 版本、证据与材料策略
原F4基线→首轮完整diff为review-diff-r1.patch（2277行），首轮→当前增量为review-diff-r2.patch（212行，4份源码+2份测试）。由原67份基线+首轮diff重建76份首轮输入，摘要逐份匹配其审前哈希，重建快照在/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/review-r1-snapshot。当前76份摘要为review-source-before-r2.json，三个最新正式运行各自前后均一致且等于当前；51份历史/资源/本机文件保护未变，默认仍两Mock。
前轮已直接Read的未变F2/F4公共Prompt/计数/Schema/JSON工具及其回归，可依据上述内容不变证明及首轮原始Read日志复用检查，按需补读。**当前核心执行/传输/协议/契约/绑定和新测试仍须独立完整读，不能只看diff**；源/新测试名单在下面。全局审查范围不缩成两处修复，报告要明确本轮新读与复用范围。
原始运行均由执行者/切片任务产生：r2-full 1097 passed/8 live默认skip；r2-py312 独立3.12关键233 passed，DeprecationWarning作为error；r2-live 2 passed、input_delta均0，主Chat/备Responses均DeepSeek，累计F5云端调用6次（不是隐式重试）。隔离副本Red与项目验证分开。精确命令/原输出txt已列出；review-evidence-r2.md是来源索引不是审查结论。
无需重复读取巨大evidence.json的全部历史raw_runs；以确切原始txt、当前源码/测试和当前摘要为证据。若需要可Read其provenance或其他安全文件。原始报告里的项目根相对链接未重写，路径按项目根解释。

## 必读清单
独立小文件可在同一回复并列发起多个Read，以减少往返；仅Read。大文件分段读，勿巨型输出截断。请如实列未读/读取失败，不因压缩总结就将未读文件写成已读。下列每份必须实际Read（源和测试完整；diff212行完整；原始txt完整），不要因为父目录或文件名相近而省略：
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/AGENTS.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/live/test_f5_streaming.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/unit/test_f3_config.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/utils/__init__.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/AGENTS.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/design.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/test-plan.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/implementation-summary.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/review-r1.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/review-evidence-r2.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/review-diff-r2.patch
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F5/live-r2-results.xml
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/README.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/contracts.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/architecture.md
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/streaming.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/gateway.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/trace.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/sse.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/routes.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/middleware/request_context.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/streaming.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/deepseek.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/openai_compatible.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/base.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/mock.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/providers/factory.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/models.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/contracts.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/constants.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/main.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/api/dependencies.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/config/gateway.yaml
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/unit/test_f5_execution.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/protocol/test_f5_providers.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/integration/test_f5_gateway.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/unit/test_f5_contracts.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/smoke/test_f5_http_server.py
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/tests/f3_cases.py
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/review-r1-metadata.json
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/review-source-before-r2.json
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/isolated-tail-usage-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/isolated-progress-model-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/isolated-expired-start-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-full.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-py312.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-live.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-docs.txt

## 三维要求
1. 需求正确性：真实用户目标是否被遗漏/误解或被设计/测试共同歪曲，是否超范围。设计是待核实材料，不因已确认就跳过判断。
2. 实现一致性：沿预检→执行→实际send→Provider→平台事件→Trace核对字段/值/字节/错误优先级/调用次数/未知值。独立提出未被现有测试覆盖的组合或故障时序；检查Mock边界是否掩盖真实副作用。
3. 系统正确性：检查老功能兼容、单实例内并发隔离、共享客户端与单流资源、异常/取消传播、期限、终态竞争、任务/缓冲及证据能力边界。

## 输出
- 三维覆盖表、依据、结论与未覆盖/未验证。
- 独立推导反例/时序与结果；按风险选择数量，不凑数。静态推断不冒充执行者已运行（需对应实际用例/回执才可标已复现）。
- Findings P0–P3，给文件/行、触发、影响、违反契约、最小验证/修正；标已复现（谁运行）、静态确定或待验证风险。
- 实际已读/未读、新读/复用及证据来源。Read次数不证明正确，直接读取与只在patch见过应区分。
- VERDICT APPROVED或BLOCKED，P0/P1/P2/P3数量及理由。三维完整、无P0–P2且关键证据完整才APPROVED；普通范围外限制单列，不自动阻断。允许没有新问题，但不能根据测试通过或首轮报告默认批准。
P0密钥/越权/严重数据破坏；P1主链/关键安全边界；P2本项承诺不满足；P3不影响验收的轻微问题。
结束直接返回报告，不写文件或实施修复。
