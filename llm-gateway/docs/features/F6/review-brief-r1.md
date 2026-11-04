# F6 第1轮独立审查材料

## 任务与边界

项目根：`/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway`。

本项F6主动取消，累计第1轮完整代码审查。仅单实例单worker、本机或隔离开发网络、内部Agent；无多实例、计费、重试/fallback、整个Agent任务取消、普通调用主动取消或新增Provider协议。默认两条Mock保持。

首轮使用全新Agent、`fork_turns="none"`，不继承实现聊天。只读、不修改文件、不运行测试/实验/付费API；不读取credentials.yaml、postgres.password、.env、.claude、.venv、缓存。通用工具仍可能有写/执行能力，只读为任务约束，不能宣称工具层强制只读。需要实验请发最小输入/操作/待观察项给root，由执行者在当前授权范围内运行后回传。

## 用户需求及来源

以下原文摘录来自本聊天，提供给审查者核对，不是实现者私有推理：

- “取消是一个完整的功能，我的建议调用方只需要轮询调用POST /v1/llm/stream/{request_id}/cancel，直到拿到终态(canceled,failed,completed里的某一个)，内部必须要从canceling开始，清理工作，返回终态”。
- “DeepSeek是chat completions方式，OpenAICompatible是Respones方式，两个都需要支持”。
- “我的理解取消是不需要知道是chat completions还是responses的把”。

确认记录（执行者转述，原聊天连续回复）：用户先确认轮询协议，再确认完整设计，再单独确认33组测试方案并授权实施。实现前发现测试方案误写第三条OpenAICompatible Chat路径，用户以上原文澄清实际两条接入；没有授权新增第三条协议。其他已确认边界见设计第5节及测试方案3.5。

用户另要求修改任一AGENTS.md必须先获其明确确认；本次未获修改授权。根/文档AGENTS保持原样，其过时进度段不作为F6当前实现的依据，状态以F6文档为准。无需Git操作。

不具备外部完整聊天导出工件；以上原文及确认摘录来源于当前聊天，审查者不能声称独立读取了未提供的聊天导出。设计、测试、导读和执行者结论均是待核实材料，允许质疑。

## 材料与真实差分

先读本项：

- `/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/design.md`
- `/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/test-plan.md`
- `/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/implementation-summary.md`
- `/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F6/evidence.json`
- 审查规范：`/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/templates/独立审查提示词模板.md`。

evidence.json保存实现前安全文件哈希、最终验证源码/测试哈希、21份新增/修改文件的真实unified diff、33组实际节点映射、原始执行输出及失败处置。先读取JSON键与小型元信息，再按文件/日志字段分段提取，不要一次打印全部大JSON。新增源码可直接读文件，已有文件以差分+必要上下文为主。文件清单/哈希只能证明范围与未变，不能证明正确性。

基线是实施前安全快照，不是Git提交。未提供凭据基线；禁止通过读取真实凭据补齐。执行者会保存本轮审查前后快照并核对。验证过程中产品/测试冻结；文档状态更新不改变产品。原F1–F5验收Markdown及AGENTS/默认配置保护摘要在evidence里，历史完整JSON证据没有实施前哈希，不夸大为完整历史工件均经过前后哈希比较。

重点调用链：

1. `src/api/routes.py`、`src/contracts.py`：参数、HTTP四字段、202/200和原ID、OpenAPI。
2. `src/services/streaming_manager.py`：Cancellation、cancel、_start_cancel、_cancel_operation、subscribe、_generate、_task_done、reconcile、close。
3. `src/services/streaming.py`：run_background、run、_emit、_work、_cleanup；主动取消与普通退出/失败的区分、有界清理与残留任务跟踪。
4. `src/persistence/store.py`：accept_cancel/finish_cancel与append_event/finish/begin_attempt/recover；`tables.py`、`database.py`、`migrations/versions/f6_001.py`。
5. `src/main.py`：flush_pending/生命周期；安全相邻上下文`middleware/request_context.py`、`services/subscriptions.py`、`api/sse.py`、两实际Provider和factory/models。
6. 新F6测试、`tests/f6_cases.py`、`tests/conftest.py`、`tests/postgres_support.py`及F5-O1迁移断言的最小修改。旧F5/F5-O1/F5-O2相关回归源文件可按影响补读。

当前文档：README、docs/architecture.md、contracts.md、progress.md及F6文档；AGENTS不修改，不要求本次擅自整理其旧Feature进度。

## 证据边界及已知故障处置

所有测试由root/命名实现Agent执行，审查者未重跑；命令、实际数量、版本和原始输出以test-plan/evidence为准。

已执行：完整1418通过/16 live跳过；随后root自检复现并修正迟到清理覆盖内存终态的问题，仅执行器取消分支及新增回归有差异（原/新摘要和真实差分可核实）；最终相关严格142通过，包含全部89个F6节点及旧执行/管理器/停库分支。当前1435节点收集成功。请核对版本边界，不把1418说成修正后同一版本全量。

- Red包括缺取消路由404、缺Store取消API、执行器取消误分类及不响应取消的清理无限等待。
- 首次全量在已知fixture隔离错误后被执行者中断：用nodeid识别F6误匹配旧JSON参数中的Unicode转义；改按测试文件名识别，旧用例组重测。离线守卫阻止读取用户数据库密码，没有读出密钥。
- 后续全量出现Docker测试PG重启bind冲突，原端口55184在本机临时区间49152–65535内。测试帮助函数改为预选可用20000–29999固定端口，保留正确Red/Green，专属停库场景串行重测；不把失败原运行写成通过。没有证据确认当时占用端口的具体进程。
- 首轮本地DeepSeek协议测试帧缺object字段，正确被解析器拒绝；修正测试帧并要求首事件是delta，重测两实际协议，无产品Provider改动。
- 付费供应商测试未执行；本地真实HTTP/SDK不是供应商证据。不证明远端停止计算/计费、生产容量或多实例。

覆盖映射区分直接验证、历史补充及尚未单独组合的分支；不要把所有组有关联节点等同于每种组合已独立验证。请评估是否有影响验收的关键证据缺口；需要补实验就提出明确场景，不能预设通过。

## 三维审查与报告

1. 需求正确性：完整取消、轮询终态、内部经过canceling、两实际接入且取消不按协议分支，是否理解错误/遗漏/过度实现。
2. 实现一致性：输入输出、提交胜者、阻止新delta、唯一终态、ACK丢失、清理失败、Usage/Trace、HTTP断连与重复轮询；独立推导已有测试外的反例。
3. 系统正确性：旧普通/Schema/流式回归、任务资源所有权、并发隔离、异常传播、DB恢复/退出、默认配置与单worker假设；未来多实例限制不直接变成本项缺陷。

报告包含三维覆盖表、具体反例时序及核对结果、P0–P3 findings（文件/行、条件、影响、违反契约、最小验证及修正）、已读/未读与验证来源、关键证据缺口及边界，最后VERDICT: APPROVED或BLOCKED和各级数量。标注静态确定/执行者已复现/待验证风险，不能冒充你重跑了测试。P0–P2阻断，P3记录处置；无缺陷可直接说明，不凑数量。最多三轮，修复后由同一审查者复核，不能换工具重置轮次。
