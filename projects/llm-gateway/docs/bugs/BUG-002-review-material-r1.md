# BUG-002 第1轮独立审查材料

本材料由执行者整理，所有方案、导读、通过数量均待核实；允许质疑设计本身，不预设通过结论。本项首次完整独审，不继承实现对话。

## 用户确认与来源

当前会话原话摘录：用户要求“先落地相关文档，然后我们按照BUG-002 测试隔离 → 并发治理 → 模型管理与路由挨个推进”；随后提出开发时用Mock、再用真实模型复测公共功能，ENV与测试范围关系需清楚。经解释，用户对“公共功能用例共用、两种Provider都执行，特殊故障仍允许Mock/受控上游稳定制造”回答“是的”。

最新实际提问原文：“请确认更新后的 BUG-002 方案：修复测试对本地配置的依赖，并让普通、结构化、流式、续传、取消共用验收步骤，在 Mock 和真实 Provider 下执行；保留故障专项及历史单元测试，按文末9组测试完成TDD、全量回归和独审。”用户明确回答：“确认，按更新方案执行（推荐）”。

1.6已经收尾，BUG-002作为非阻断遗留再单独修复。默认开发模型绑定、凭据及AGENTS.md不得擅自改动；本项不实现并发治理或Router。部署仍为本机/隔离网络、单实例单worker。以上为可得原话摘录和执行者范围转述，没有完整会话导出文件，不假称原始完整转录可用。

## 需求、实现与测试方案

必读 /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/bugs/BUG-002-offline-config-isolation.md（保留历史，文末2026-11-03整体9组已确认方案及执行结果）、BUG-002-implementation-plan.md、BUG-002-implementation-summary.md、BUG-002-acceptance-report.md；跨功能docs/test-plan.md新增共用原则，docs/1.7-plan.md描述主题顺序和边界。AGENTS及docs/AGENTS约束保持原样。

修复测试消费维护者config/gateway.yaml的耦合，用测试自建注册表/临时YAML；生产load_registry真实解析保留，默认目录/默认文件名通过真实模型源码的隔离副本验证。离线模型YAML/Key/PW拒读与网络边界，临时配置/假凭据/回环允许；收集阶段观察器含符号链接反例。ENV不自动选择Provider或跳过主链，Mock和真实应用可都为ENV=test。

公共API：tests/bug002_scenarios.py里的3个scenario函数承担五类场景，10个Mock节点与原10个真实节点直接复用；原额外CLI真实节点及F10容量保留。Mock在测试bootstrap注入typed脚本，使用模拟文本/Usage，并记录每request_id调用/关闭；真实仍经原适配器/TCP/原始上游文本对照。不能将Mock协议配置名当作真实网络证明，不应削弱业务事实、Schema/Usage/唯一Attempt/序号/终态/账目断言。

新增完整回归2086节点，原2039节点保留、新47，真实节点仍28。开发目标时使用Mock/受控协议，随后真实公共11例，再完整--run-live；纯逻辑不用真实远端，真实路径两种协议同属DeepSeek，非两家供应商。

## 基线、实际差分及冻结

没有Git，使用实施前安全快照 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/base；原始清单 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/base-manifest.json，审查冻结清单 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/review-r1/manifest.json，实际累计差分 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/review-r1/changes.diff，变更摘要 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/review-r1/changes.json。共198个源码/测试/安全配置及当前关键文档。/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/full-start-v2/manifest.json为最终全量开始的冻结版本；执行期间无漂移，完成后仅更新文档/证据，源码/测试不改。

保护清单 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/protected-before.json 及 /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/bugs/BUG-002-evidence-source-check.json：生产src、迁移、AGENTS/模板/原Feature档案及运行安全配置共311份均与实施前一致。哈希只证明版本/未改，不证明正确性。只允许读取这里明确列出的安全配置快照：/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/base/config/gateway.yaml、gateway.example.yaml、prices.yaml；不读开发credentials.yaml/PW/.env。

原F10验收工件及审查报告未改写；最终BUG-002回归是新回执。文档/证据另有 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/review-r1/docs-manifest.json（生成后冻结），允许已有原始文件解析和哈希，不可运行测试/实验。

## 真实执行回执及边界

执行者root及具名子任务agents bug002_config_consumers/bug002_common_acceptance；开发回执标明来源，主执行者负责PG/进程/live/全量串行。完整命令：uv run --python 3.12 python -W error -m pytest --run-live -o junit_family=xunit1 --junitxml=/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/full-final.xml -ra -q，实际exit0，2086 passed /1770.19秒。

原始证据（均是执行者运行，审查者未重跑）：
- /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/bugs/BUG-002-full-final.xml.gz、BUG-002-full-final.log、BUG-002-evidence-final.json：2086通过/28 live/0失败错误跳过，嵌套缺Key错误/默认skip/收集读取反例属于被外层验收的预期结果，不是漏跑实际live。
- BUG-002-capacity-final.json.gz：四档66/313/630/1334，20并发600秒12544，总14887笔全部成功，原始文本对照/SQL费用/资源收口仍执行；固定短重复任务含观测开销，不称极限/SLA。
- BUG-002-mock-observations.json.gz：本轮10个Mock环境实际typed调用/结果/关闭记录；对应JUnit中request_id和provider_observation可核对。
- BUG-002-evidence-development.json：原场景真实定义副本Red1失败→同场景1通过、守卫Red/Green、consumer单元89/协议112、配置与收集反例最终14、脚本观察器18、Mock10和真实11目标回执。
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-bug002-fbnsw_6u/node-mapping-final.json：原2039节点均保留，新增47；可独立对照原F10 XML及本次最终collection。

需注意开发中的真实失败历史：初版Mock观察器误认固定30秒，使Mock10失败；修正剩余预算后6通过4失败；关闭观察使用执行时ContextVar导致跨Task清理失去归属，用双流第三Task反例修复为调用入口捕获独立Context，关闭断言不放宽。另守卫/收集记录器的符号链接缺口均有Red/Green；一轮全量在561 passed时为修观察器主动中止，未进入真实调用，未改写为完整通过。全部中间日志保留，不以通过数量替代判断。

正式供应商账单、逐请求原始Usage对SQL字段的归档快照及多实例/生产长期容量未因本测试Bug新增验证。已有SQL逐笔内部费用/账户公式核对及历史固定费用黄金都在本次全量重跑，完整范围依当前方案，不用未来归属掩盖当前行为缺陷。

## 阅读范围与输出

全部变化在真实diff清单中：tests/conftest.py、五个消费方文件、新bug002_cases/单元配置/隔离反例、f10_support、公共scenarios/Mock wrappers/观察反例、原真实F10 wrappers，以及上述文档。新增直接读源码，已有读差分和必要完整上下文。必要未改调用链：src/services/models.py、providers/base.py/mock.py/factory.py、main.py、services/gateway.py/streaming.py/streaming_manager.py、persistence账务/Store；按影响扩展，不需机械重审全部旧代码。

按 /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/templates/独立审查提示词模板.md 做需求正确性、实现一致性、系统正确性三维独立审查。重点检查测试与方案是否共同误解用户的“共用”、是否用Fixture替身掩盖真实解析/装配、收集或子进程的配置/Key/网络隔离、Context闭包并发归属/关闭、真实证据是否保留、节点映射/统计与资源释放。允许提出已有测试之外的具体反例。

只读，不修改、不运行测试/实验/服务/付费API，不读凭据/.env/.claude/.venv/缓存；通用工具并非强制只读，这是任务约束。需要复现给最小输入/步骤/预期/观察项，由root运行并回传原始回执。报告三维覆盖、关键反例及依据、P0-P3具体文件行/触发影响/验证方法/最小建议、实读和未读/未跑来源，最后VERDICT APPROVED/BLOCKED及等级数量。无P0—P2且无关键证据缺口才APPROVED，普通范围外限制不虚增缺陷；最多三轮，本次第1轮。完成返回原始完整报告，不写文件。
