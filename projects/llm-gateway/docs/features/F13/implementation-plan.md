# F13实施步骤

> 2026-11-03，整体设计与32组测试方案均获用户分别“确认”。使用writing-plans、TDD和subagent-driven-development；项目工作流优先：无Git/worktree要求，不重复申请开始许可，不按子块反复发起完整审查。累计最多三轮全新上下文代码独审，AGENTS不改。

目标：先可靠接入百炼Flash/Plus/Max并按真实Usage结算，再交付同一请求两阶段自动路由。阶段A达到标准才接线阶段B；不拿Mock或文档充当真实验收。

## 1. 文件与资源归属

| 所有者 | 可修改文件 | 任务及依赖 |
| --- | --- | --- |
| 集成者 | services/models.py、token_budget.py、routing.py、Gateway/流式、Store/Trace/迁移、配置/文档、集成及真实测试 | 先固定内部接口，负责共享入口、首Red、真实密钥本机配置及唯一串行PG/进程故障验证 |
| 计数任务 | services/token_counters/qwen_text.py、__init__.py、base.py及新增test_f13_estimation.py | 本地UTF8完整安全表示估算，kind=estimated、版本化固定/比例余量、三模型非思考匹配；沿用count()->int，TokenCount新增kind默认exact兼容旧构造，集成者负责上层传递 |
| 价格任务 | services/prices.py及新增test_f13_pricing.py | 兼容旧固定/分时价格，新增输入长度分档、快照、预留最高适用价格及按实际Usage重选档位，Decimal全程保真；独立单元测试，不操作数据库 |
| 协议任务 | providers/bailian.py、必要的openai_compatible.py/base.py/streaming.py/factory.py及新增test_f13_bailian.py | 百炼Responses专属映射，复用传输/类型；真出站字节、SSE、错误/Usage及关闭检查；不改models.py或上层编排、不读密钥或发真实调用 |

稳定接口：ProviderConfig新增adapter=bailian，仅responses；EndpointConfig新增min_output_tokens默认1，百炼配置16；既有Thinking禁用。Counter标识qwen_text_estimate_v1，Qwen预估不依赖Provider绑定。价格任务先明确自身新配置/快照接口给集成者再接运行配置。不会让不同任务同时编辑同一文件。

## 2. 阶段A：接入与计量

- [x] 保存安全源码/测试基线、原2508节点及私有文件摘要；不归档凭据/实际配置。
- [x] 首Red：真正load_registry加载合法bailian端点/组应成功，现有代码拒绝；同时验证16Token下限的业务拒绝。保存原始输出，准备/导入错误不算正确Red。
- [x] 协议、计数、分档价各自先正确Red再最小Green；独立纯逻辑/回环任务可并行，集成者统一集成。
- [x] 集成Provider/模型能力/最小输出、TokenCount kind及Trace持久化，原DeepSeek路径保持。
- [x] PG验证真实报价、跨档、缓存、预留/结算、欠额与并发锁、历史记录；原场景及相关历史回归。
- [x] 在本机安全导入百炼CSV凭据（不回显），维护者原DeepSeek/Mock绑定保持，新增百炼组；固定官方北京地址、模型限制和价格版本。
- [x] 三模型普通/Schema/流式/探测、取消/续传及每模型8类真实校准；安全留证。参数协议不兼容或校准不足则修复原场景，不提前宣布A通过。

## 3. 阶段B：自动路由

- [x] A通过后再编写auto_routes加载、0/1/多组及非法分类的业务Red。
- [x] 新分类模板task_router@1.0.0，固定规则/静态Few-shot、完整转义任务资料、动态enum Schema；构造30开发及30封存独立样例。
- [x] 本地候选组预检产出冻结计划；分类一次5秒128输出，白名单/失败表/默认Plus检查，调用方模板始终保持。
- [x] 抽取必要的单次尝试执行能力，分类及生成共用原request_id/总期限/名额；编号连续、用途隔离；分类不结束请求，F7只负责生成阶段恢复。
- [x] 迁移用途及元数据，更新Store/Trace/账务汇总；pending冻结、GET首次执行、取消与清理、重启/配置变化、只读续传均形成闭环。
- [x] M/W/P/O逐项验证B01—C09，受控交错不按完成顺序关联。
- [x] 真实Flash分类封存集满足27/30且每档8/10，三组命中及真实Plus降级；原始失败保留，质量与可用性分开记录。

## 4. 集成、交付与独审

- [x] 使用实际收集建立A01—C10节点映射，所有原节点保留；确定新增入口basename不冲突。
- [x] 混合既有DeepSeek、三百炼模型及auto入口容量，共用四档与600秒负载；原断言和路径均保留，记录实际样本与排空。
- [x] 冻结安全版本后完整执行`uv run --python 3.12 python -W error -m pytest --run-live -ra -q -o junit_family=xunit1`，不因费用跳过真实或历史用例；保留实际失败和版本边界。
- [x] 一次提交前自检、代码导读、README/架构/契约/进度及原始证据同步；不篡改已验收Feature历史。
- [x] 先归档实际提示词，首轮全新上下文Agent只读三维审查；最多三轮，同一审查者复核，缺陷先复现/修复/复测，第三轮仍阻断才交用户。
- [x] 所有确认用例和关键证据满足且无阻断项后报告验收；未执行/未知事实如实注明，不因阶段A或模型200就称整个F13通过。

开发命令按当前实际文件执行目标集及相关回归；阶段/最终命令、Red/Green、环境、退出码和差分在test-plan.md与真实工件记录，不把计划勾选当运行证据。具体数值/官方资源核对属于已确认范围的实施收敛，改变公开契约才返回需求讨论。

容量标准确认前的闭环记录：旧版完整2903通过/1容量失败、原补8通过；首次封存30/30合成标签一致。独审发现的请求级Trace问题已正确Red、最小Store修复、5项PG直接回归；修复版2813非live及17真实影响复验均通过。源码255摘要稳定，原始证据与首轮报告已归档，两个AGENTS未改。上面的容量/全量勾选仅指实际执行并留证，不表示原严格容量通过；当时整项验收尚未完成。第1轮代码遗留P0—P3为0，整体BLOCKED仅C10，新的分层验收边界等待用户确认。

最终：用户已确认分层容量标准，第1轮补充复核APPROVED、P0—P3均为0。原两轮failed继续保留，六phase9785请求的新标准复核及相关130测试通过；最新257清单仅测试变化，产品代码保持修复版。F13功能验收通过，详见test-plan第7节。上段BLOCKED为确认前历史，不代表当前状态。
