# 递增功能与决策进度

> 当前状态（2026-11-03）：**1.6已收尾**，F1—F10及已实施优化验收通过。最终全量2039通过，含真实28例；BUG-002在1.6收尾时作为非阻断遗留保留。[收尾记录](1.6-closeout.md)。1.7已确认交付顺序，BUG-002现已验收通过（2086通过含28个真实节点、首轮独审APPROVED），详见[递增计划](1.7-plan.md)。

> 2026-11-02阶段记录（保留历史）：F5 已验收；F5-O1 统一持久化设计与测试方案已分别确认，已验收通过：串行全量1228/关键312，原生Claude Code第2轮APPROVED，无P0–P2。下文各历史阶段的结果保留原时间与证据边界。

2026-10-31，用户另行确认“F1: 我测试过，通过了”；反馈记录见 [当前 F1 用户验收反馈](features/F1/optimizations/O1/test-plan.md#37-用户验收反馈)。该反馈发生时 F2 尚未开始；当前状态见下表。

## 当前状态

1.7当前推进：**BUG-002测试隔离 → 并发治理 → 模型管理与路由**。主题划分及顺序已确认，逐项设计和测试仍分别确认；当前BUG-002验收通过：最终全量2086通过、28个真实节点、零跳过；首轮独审APPROVED，P0—P3均为0。[验收记录](bugs/BUG-002-acceptance-report.md)。[F11并发治理设计](features/F11/design.md)已按最新确认调整为全局/Agent两层，内部重试持有原名额，[测试方案](features/F11/test-plan.md)已单独确认，已接入，已按分层标准验收通过（第2轮独立复核APPROVED）；[F12模型管理与模型组路由](features/F12/design.md)的完整设计与[28组测试方案](features/F12/test-plan.md)均已分别确认，实现及最终2508项完整回归已完成、验收通过；F13整体设计和32组测试方案均已单独确认，旧版全量已执行；Trace修复版2813项非live及17项真实复验通过，首轮独审代码无遗留问题；用户已确认分层标准，第1轮补充独审APPROVED，F13验收通过，原容量失败及限制保留。基线F1—F10的验收状态保持如下。

用户另已确认[公共功能共用用例、Mock与真实分别执行、特殊故障受控复现](test-plan.md#已确认的共用验收原则2026-11-03)；ENV与Provider/测试范围独立。BUG-002据此补充最小公共API验收层的推荐范围，连同当前修复及9组测试已获用户最新整体确认；BUG-002已完成；用户随后确认F11两层调整，更新后的本项测试方案已单独确认，F11已按新口径验收通过，F12模型组路由已验收，F13已按用户确认的分层标准验收通过。

2026-11-02用户确认推进规则：保留README既有Feature内容和顺序；先给完整推荐方案和必要备选，设计/测试分别确认，仅对关键歧义继续讨论。独立且已确认的任务可并行，公共接线统一集成、按序交付；故障测试串行。方案/提交前各一次自检，新证据或实质变更才重评影响部分；独立审查三维与累计最多三轮规则保留；用户现已确认使用全新上下文Agent，Claude Code可选。今天1.6/1.7均为单实例单worker；Chat Completions/Responses保持上游协议选择，不新增对应对外入口。F5-O2已验收：完整1330/修正相关严格41及真实两条通过，累计第2轮全新Agent独审APPROVED，无P0–P3。

| 功能 | 状态 | 证据 / 下一步 |
| --- | --- | --- |
| F0 文档先行 | 已完成 | README、AGENTS、架构、契约和测试原则已建立，F1—F10的设计、测试与验收分别留存。 |
| F1 统一入口与普通调用 | 验收通过 | [测试结论](features/F1/test-plan.md)、[原始独立审查](features/F1/review.md)：70 passed；APPROVED，P0–P2 为 0，5 项 P3 已记录处置；[工具故障修复](bugs/BUG-001-claude-code-inline-tools.md)完成单变量与真实 F1 复测 |
| F1-O1 应用组装、目录命名与环境配置优化 | 验收通过 | [独立优化设计](features/F1/optimizations/O1/design.md)已确认；[测试方案及执行证据](features/F1/optimizations/O1/test-plan.md)：115 个测试通过，Claude Code 第 1 轮 APPROVED，P0–P2 为 0、2 项 P3 已记录处置；原 F1 验收记录不变 |
| F2 受控 Prompt | 验收通过 | [设计](features/F2/design.md)及三份完整资产已确认；[测试方案与执行证据](features/F2/test-plan.md)：本地 223 个测试通过；[Claude Code 第 1 轮审查](features/F2/review.md)通过，无 P0–P2；2026-10-31 [用户验证通过](features/F2/test-plan.md#38-用户验收反馈)；F3 Provider/Token 留后续讨论 |
| F3 模型配置与适配器 | 验收通过 | [设计](features/F3/design.md)、[测试方案](features/F3/test-plan.md)：用户已明确授权实现；已落地；570离线通过、4live默认跳过；Claude Code第1轮无P0–P2；2026-11-01真实四例4 passed；Claude Code第2轮APPROVED，无P0–P2；2026-11-01 [用户验收通过](features/F3/test-plan.md#310-用户验收反馈) |
| F3-O1 应用组装、计数策略与导读 | 验收通过 | [设计](features/F3/optimizations/O1/design.md)、[测试方案](features/F3/optimizations/O1/test-plan.md)：已确认并授权，基线570通过/4默认跳过；离线606通过/4默认跳过、真实4通过，Claude Code第1轮APPROVED、无P0–P2；原F3记录保留 |
| F4 Structured Output | 验收通过 | [设计](features/F4/design.md)、[测试与第1轮Claude Code审查处置](features/F4/test-plan.md#39-claude-code-独立审查与处置)、[代码导读](features/F4/implementation-summary.md)：最终离线836/3.12关键233/最新真实六例通过；原生独审APPROVED、无P0–P2，两项P3已核实处置；66份源码/测试/配置摘要前后一致，历史工件保留 |
| F4-O1 平台常量与 JSON 工具整理 | 验收通过 | [代码导读](features/F4/optimizations/O1/implementation-summary.md)、[回归与处置](features/F4/optimizations/O1/test-plan.md)：离线878/6 live跳过、3.12关键268通过；原生Claude Code第2轮APPROVED，首轮文档发现及二轮P3已闭环，65份源码/测试/默认配置审查前后未变；2026-11-01[用户确认验收通过](features/F4/optimizations/O1/test-plan.md#38-用户验收反馈)；原 F4 验收历史保留 |
| F5 Streaming | 验收通过 | [完整设计](features/F5/design.md)已获用户确认；[逐例测试方案](features/F5/test-plan.md)随后单独确认，按[实施计划](features/F5/implementation-plan.md)实现；最终离线1099/3.12关键235/最新真实两例通过，[第2轮独审与P3闭环](features/F5/test-plan.md#310-最终验收与第2轮审查处置)：APPROVED，无P0–P2，审后仅补测试，产品代码不变；含心跳/总期限、统一事件、背压/断连清理及内部TTFT/Generation |
| F5-O1 统一持久化基础 | 验收通过 | [完整设计](features/F5/optimizations/O1/design.md)与[测试方案](features/F5/optimizations/O1/test-plan.md)已分别确认，按[实施步骤](features/F5/optimizations/O1/implementation-plan.md)执行；串行全量1228/关键312及原生Claude Code第2轮APPROVED，无P0–P2；真实六例采集版本/修订边界及原始回执见测试方案，代码导读已同步 |
| F5-O2 两阶段调用与断点续传 | 验收通过 | [设计](features/F5/optimizations/O2/design.md)与[测试方案](features/F5/optimizations/O2/test-plan.md)均确认；完整1330/修正相关严格41/真实两条通过，累计第2轮新Agent独审APPROVED；[代码导读](features/F5/optimizations/O2/implementation-summary.md) |
| F6 流式取消 | 验收通过 | [设计](features/F6/design.md)、[最终验收](features/F6/test-plan.md#38-最终验收)、[代码导读](features/F6/implementation-summary.md)；当前91个F6节点，最终受影响F5/F6严格542通过/10跳过，累计第2轮独审APPROVED、无P0–P3；全量1418的版本边界见测试方案 |
| F7 重试与 fallback | 验收通过 | [测试与验收](features/F7/test-plan.md#39-用户修订验收标准最终版本全部重跑并启用真实provider)：最终完整--run-live 1776通过、0跳过/0未选中；16真实全部通过，原297已重跑；R2 APPROVED且版本不变；[原始证据](features/F7/evidence-all-live.json)、[代码导读](features/F7/implementation-summary.md) |
| F8 Trace 完整性与诊断查询 | 验收通过 | [代码导读](features/F8/implementation-summary.md) · [全量 1904 通过，含真实 16](features/F8/test-plan.md) · [第三轮独审 APPROVED，保留 1 个 P3](features/F8/review-r3.md) |
| F8-O1 默认 Mock 模拟用量 | 验收通过 | [设计](features/F8/optimizations/O1/design.md)及[测试方案](features/F8/optimizations/O1/test-plan.md) |
| F9 用量与计费 | 验收通过 | 最终2010通过含真实16；用户重审后的[F9-B1修复及独审](bugs/F9-B1.md)关闭剩余阻断项。 |
| F10 总验收 | 验收通过 | [测试与证据](features/F10/test-plan.md)：全量2039通过，含真实28例；[报告](features/F10/acceptance-report.md)记录16935笔容量请求及20并发600秒结果；首轮全新Agent独审APPROVED，两项P3文案已闭环。 |
| F11 并发治理 | 验收通过（分层） | [两层设计](features/F11/design.md)与测试方案已确认；用户随后确认分层验收。完整2242通过/23个真实失败，全部2231非live通过；四档及600秒实际完成，有界补验6个真实治理场景通过。[第2轮独立复核APPROVED](features/F11/review-r2.md)、P0—P3均为0；原始失败、供应商600秒成功率24%、SQL/账务/资源及留证边界见[第二次诊断](features/F11/diagnosis-r2-20261103.md)，首轮BLOCKED记录保持原文。 |
| F12 模型管理与模型组路由 | 验收通过 | [代码导读](features/F12/implementation-summary.md)、[完整回归](features/F12/test-plan.md#7-修复后完整版本回归第2轮复核)：2508通过含42个live，原2265全保留；真实容量16784笔全部完成，20并发600秒通过。[第2轮独审APPROVED](features/F12/review-r2.md)，P0—P3均为0。 |
| F13 百炼三模型接入与任务自动路由 | 验收通过（分层） | [设计](features/F13/design.md)及[测试与实际结果](features/F13/test-plan.md)：旧版2903通过/1容量失败/零跳过，首次封存30/30合成标签一致；Trace缺陷已修复，修复版2813非live及17真实复验通过；[第1轮补充独审APPROVED](features/F13/review-r1-capacity-followup.md)、P0—P3均为0。用户确认分层标准，原六phase9785请求处理复核通过；新判据相关130通过。两轮严格失败及原BLOCKED报告保留，不作稳定容量承诺。 |
| F13-O1 配置示例、应用入口与项目首页整理 | 验收通过 | [代码导读](features/F13/optimizations/O1/implementation-summary.md)、[测试及证据](features/F13/optimizations/O1/test-plan.md)、[同一首轮独审APPROVED](features/F13/optimizations/O1/review-r1.md)：分批覆盖2972节点，104 live全通过，原8个测试迁移失败复测通过；README真实启动22步通过。原F13记录保持。 |

F1 与 F1-O1 已验收；F2 的需求、设计和完整资产已确认，本项测试方案确认后已开始实施并完成本地验证，本地 223 个测试及 Claude Code 第 1 轮独立审查通过，F2 已验收。每个功能先确认设计和对应测试方案，再按 TDD 最小实现；功能边界独立、能力递增。Feature 在 `docs/features/<编号>/` 保存设计和测试方案，Bug 在 `docs/bugs/` 建记录，均由本文件追踪；不以 Git 分支或提交作为前提。验收通过后报告结论，之后发现问题走 Bug 工作流。

## 已确定的跨功能决策

BUG-002离线测试配置隔离：1.6收尾时保留为非阻断遗留，未宣称修复；现已选为1.7第一项，已重新静态核对当前代码，修复及9组测试方案已按Bug流程完成，验收通过；[代码导读](bugs/BUG-002-implementation-summary.md)、[独立审查](bugs/BUG-002-review-r1.md)。[原始问题、当前方案与交接](bugs/BUG-002-offline-config-isolation.md)。2026-11-01切回Mock及570通过/4 live默认跳过仅为当时记录，不能替代配置隔离验收；原“先于F3-O1处理”的历史计划没有闭环，不继续当作当前执行顺序。

1. Gateway 管单次模型调用；Agent/Harness 管任务与业务决策；Provider 隔离供应商协议。
2. 首版仅本机或隔离开发网络，供多个内部 Agent 调用；单实例单 worker。对外开放与多实例协调留到 1.7 复议。
3. 源码直接放在 `src/`；`src/services/` 集中内部组件，`api/` 管传输，`providers/` 管外部适配；按职责命名，不机械增加 `XxxService` 类。
4. `DeepSeekProvider` 直接接入 DeepSeek；`OpenAICompatibleProvider` 通过兼容协议接入同一 DeepSeek。不能把它们称为两家真实供应商。
5. Prompt 选择必填；调用方消息只允许 `user`、`assistant`；无调用方自行提交的 System 消息。
6. 当前 `POST /v1/llm` 为普通调用；O2已将`POST /v1/llm/stream`改为202创建，`GET /v1/llm/stream/{request_id}/events`首次触发及订阅。路径决定模式，不重复设stream。流式与 response_schema 组合仍调用前拒绝。
7. 对外 SSE 使用 `response.*`。F5目标包含delta/heartbeat/completed/failed，F6再增加canceling/canceled；completed/failed/canceled互斥为终态。
8. 原F5/O1生成中断连停止上游；当前O2支持断连继续、重新订阅。F6 单独实现主动取消，不取消整个 Agent 任务；取消接受后不重试/fallback，不能转为 completed。
9. 原 SQLite 规划替换为 O1 PostgreSQL/Compose：普通与流式统一 Request/Attempt，流式存业务事件，完整有效执行输入落库，Provider 凭据不落库；入口拒绝仅元信息。未知用量不记 0，暂不清理数据。F8 再补齐 Trace。
10. 每个功能开始前讨论其精确契约和测试，采用 MockProvider TDD；真实 Provider 冒烟显式启用且与 Mock 证据分开。
11. F1 是固定模板、单一逻辑模型与 MockProvider 的最小普通调用；F2/F3 才逐步增加模板和真实模型能力，避免前后功能依赖倒置。
12. 用户只在需求设计确认、测试方案确认及第三轮代码审查仍有阻断项时介入；各 Feature 独立维护设计和测试方案，验收以实际证据和独立审查为准。
13. F1 公开逻辑名采用 `general-primary`，MockProvider 仅为本地替身；F1 只接收一条 `user` 消息，不把 Mock 身份当作真实供应商模型。完整响应与错误契约已随 F1 整体设计确认。
14. 非流式 API 使用 `success/code/data/message` 四字段响应；普通调用`request_id`只在响应头，O2创建另在data返回ID。F1 在应用边界集中处理错误；404/405 统一错误外形，但不建立模型调用 Trace。流式启动后的错误仍由 SSE 事件表达。
15. F1 的单轮文本回答助手 System Prompt 正文已确认，覆盖职责、判断依据、行为约束和输出要求，完整文本见 F1 设计。F1 只验证正确装配和传递，遵守规则的效果尚无真实模型证据。

## 已知待验证事项

- F3 已按官方文档确认模型标识改为 `deepseek-flash`，窗口/输出能力仍为文档值；固定资源/独立黄金及SDK离线路径已核验；两种真实接入四例通过、本次计数差值均0；通用一致性与极限能力未实测，见 F3 设计/测试方案。
- 课程示例的三个静态审查缺口需在相应功能中变成回归测试：SDK 认证异常可能错误 fallback、非法 JSON 错误可能无失败 Trace、未知模型测试只断言状态码未证明零调用。
- O1/O2已验收；F6已按分别确认的设计/测试方案实现并验收通过；各阶段证据以对应事项记录为准。
- 1.6 的单实例测试不证明多 worker、多 Pod 或跨实例取消可靠；1.7 复议时需要独立设计和验证。

## 2026-10-31 目录重构

用户确认去除源码项目包目录层，调整导入与启动入口，业务契约保持不变。验收通过：70 个测试、新入口 HTTP 验证和 Claude Code 独立审查通过（P0–P2 为 0，1 项过期文档表述已更新）。迁移验证与独立审查见 [F1 目录重构记录](features/F1/test-plan.md#2026-10-31-目录重构验证)。

## F1-O1 应用组装优化

ENV 与 Provider 独立、只使用同名 `.env`；目录命名、配置优先级与工厂启动已实现。本地验证与逐例结果见 [本项测试方案](features/F1/optimizations/O1/test-plan.md)，本项已通过测试与独立审查，原 F1 历史记录保持不变。

## F2 实施与验收

本轮已确认的固定 System、User 变量边界、Few-shot、错误/版本/指纹与三份资产统一收敛到 [设计](features/F2/design.md)。[测试方案](features/F2/test-plan.md)列明 38 组必测场景及原 115 个场景的回归保护；用户已确认并完成本地验证，223 个测试通过并经 Claude Code 第 1 轮独立审查通过。审查观察的核实与处置见测试方案 3.7；文档黄金摘要现已由真实装配结果比对，不把其最初预期值计算冒充当时运行。

## F3 文档阶段历史

2026-10-31，用户逐项确认配置/Provider/预算/探测/真实测试范围后授权先落文档。[设计](features/F3/design.md)与[测试方案](features/F3/test-plan.md)已成文，当时完整文本和测试矩阵待评审；当时仅文档，不改源码、测试、运行配置或 F1/F2 历史工件，不代表 TDD 或真实接入已运行。

## F3 实施进度

用户明确授权实现后已落地配置/统一Provider/完整计数预算/探测与错误归一化。完整离线570通过，4个live默认跳过；关键Python3.12子集237通过。固定资源与85个原测试函数迁移核对已完成，11份历史工件未改。Claude Code第1轮离线实现审查无P0–P2，四项P3已记录处置；2026-11-01真实四例通过、Claude Code第2轮APPROVED；实际逐组映射、Red/Green与未验证项见 [测试方案3.5–3.6](features/F3/test-plan.md#35-实施与分层执行记录2026-10-31)，2026-11-01整项验收通过，最终结论见测试方案3.9。

## F4 文档阶段历史

2026-11-01，结构化边界、Schema 子集/资源限制、受控 Prompt、Chat/Responses 接入、计数与指纹、错误/Trace、测试范围和组件/文档组织已逐项确认，并形成上表入口。当时仅文档；实施、正式六次真实调用和 F4 Claude Code 代码审查均未开始，代码导读在实现稳定后产生。模板回看与已有三次技术探测的证据边界见 [本项测试方案](features/F4/test-plan.md)，不改写原 Feature 验收。

## F5 文档阶段

2026-11-01，用户逐项认可Streaming边界与四层测试覆盖，授权先整理设计和逐例测试方案。本轮只改文档，完整文本待评审；不以单项确认代替测试方案确认。文档核验、模板回看及未验证项统一见[F5测试方案](features/F5/test-plan.md#35-模板回看与文档核验)，当前未进入Red/实现/真实调用或Claude Code代码审查。

2026-11-01，用户确认F5完整设计（含文档自检处置及工程约束）；当前进入逐例测试方案确认节点，未授权进入Red或实现。

2026-11-01，用户单独确认F5逐例测试方案，授权制定实施步骤并按TDD推进；后续以实际运行与原生Claude Code独审形成验收，不再增加例行开始确认。

F5最终验收：完整两轮原生Claude Code与执行者复核均留原始记录；首轮遗漏/错误推导已用实验闭环，第二轮仅非阻断P3补测。最终来源与边界统一见本项测试方案3.10，后续F6另行讨论。
