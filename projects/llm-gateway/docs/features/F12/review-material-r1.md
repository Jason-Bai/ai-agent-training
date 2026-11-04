# F12第1轮独立审查材料

本文件为执行者提供的材料索引，不预设审查结论。设计、测试和实现导读均须独立核实。审查者不继承实现对话；本项尚未有完整代码审查，第1轮计入最多三轮。

## 1. 用户需求与确认来源

来源为本聊天的用户消息；未另导出完整原始聊天，以下注明原文摘录与执行者整理，不伪造缺失导出。

- 用户原文：“你去看一下litellm是如何做的”。执行者查询官方普通模型组及Auto Router文档后解释两层差别。
- 用户原文：“这两个我都要做，一个一个来”。随后执行者提出F12模型管理/模型组路由、F13任务自动路由依次交付，F12保持必填model作为组入口，用户回复“确认”。
- 执行者提供三层配置、有序候选、逐端点预算、恢复/账务/Trace及健康接口的完整推荐，用户回复“确认”；F12完整design.md落地并说明错误、迁移及Trace后，用户再次回复“确认”。28组test-plan.md单独提出后，用户回复“确认”，授权实施。
- 执行者整理的用户持续约束：单实例单worker、内部Agent、本机或隔离网络；保持统一对外入口，Chat/Responses仅是Provider协议；Mock跑确定流程与故障，真实验证真实表现；不因费用跳过真实或历史测试；AGENTS修改须先单独确认，本项两份AGENTS不改；不以Git为项目流程前提。

完整已确认设计和测试规则在同目录[design.md](design.md)、[test-plan.md](test-plan.md)。用户对单项讨论的认可没有被当成提前实现授权；两项完整确认后才进入代码。

## 2. 本项范围及排除项

F12：YAML管理Provider连接、Endpoint能力/限制、ModelGroup调用策略；必填model选择组；按启停、真实请求能力、输出/专属Prompt/上下文预算筛选并按列表顺序选择。F7执行冻结计划，原请求全程共用F11名额；F9每次按实际端点取价；F8补路由解释。普通、结构化、流式、续传、取消、探测及历史记录兼容以设计为准。

F13任务语义分类/LLM选型另行设计，未实现。F12没有动态管理/模型列表接口、自动熔断、权重/随机/最低成本路由、新Provider协议、多实例或上游分组限额。不能用这些排除项掩盖当前范围中的请求隔离/终态/费用缺陷。

## 3. 基线与真实差分

本项目没有Git。实施前保存358份安全文件及原2265节点清单，源码基线实际为已验收F11之后的工作区；F12设计/测试文档在当时已确认并存在。基线路径由本机`/tmp/llmgw-f12-work-path`指向目录的`baseline/`及`baseline-manifest.json`提供；后续会以本目录安全归档提供稳定读取，不把文件列表当差分。

本目录`evidence-review-source-manifest-r1.json`是本轮候选源码/测试/安全配置/文档摘要；`evidence-review-changes-r1.json`及`.diff`是逐内容对比真实基线的新增/修改/删除记录。新增文件完整内容在当前项目；已有文件按差分及影响链读取。`evidence-review-baseline-r1.tar.gz`为对应安全旧文件，不含凭据、.env、密码或数据库数据。原始最终回归的212份源码/测试/安全配置见`evidence-full-source-manifest.json`，其摘要在全量及审查前均未变化。

运行gateway.yaml结构由执行者按已确认迁移步骤升级，原Provider/model、数据库、价格及凭据绑定保持；原始运行配置私有副本不进入审查归档，仅提供`evidence-runtime-config-migration.json`。审查配置只读安全gateway.example.yaml、credentials.example.yaml及冻结索引明确列出的示例，勿读取实际gateway.yaml、credentials.yaml、postgres.password、.env或其测试临时副本。

## 4. 阅读范围

先读两份AGENTS、完整设计/测试方案、[代码实现导读](implementation-summary.md)，再按真实差分及调用链核对：

- Registry/Router：`src/services/models.py`、新增`routing.py`、`token_budget.py`、`prompts.py`、`validation.py`、`config/gateway.example.yaml`。
- 编排/恢复/资源：`gateway.py`、`recovery.py`、`streaming.py`、`streaming_manager.py`、`concurrency.py`、`subscriptions.py`、`main.py`、`providers/factory.py`、HTTP/API/中间件。
- 持久化/费用/诊断：`src/persistence/store.py`、`trace_query.py`、`billing.py`、`services/prices.py`、`services/trace.py`、`trace_contracts.py`及必要相邻表/事务代码。没有新增数据库迁移。
- 新增F12测试、`tests/registry_helpers.py`、既有测试/夹具迁移、真实Provider/共用步骤增强，以及`upstream_observation.py`只读取证变化。全部变更在真实差分中，不仅看新增文件。

必要时读相邻旧功能设计/测试以核对已有不变量；130份历史Feature文档摘要未改。严格区分测试侧夹具拆分与生产解析：后者必须拒绝旧models/fallback_models及旧恢复字段。

## 5. 执行证据（执行者产生，审查者未重跑）

| 证据 | 实际内容 |
| --- | --- |
| evidence-full.json / evidence-full.log / evidence-full.xml.gz | 最终完整2497通过，含42live，0失败/错误/跳过/筛除；pytest1984.98秒、外层2017.57秒，退出码0。Python3.12、严格警告、JUnit xunit1，完整命令原样记录。 |
| evidence-node-mapping.json / evidence-test-coverage.json | 原2265节点全保留、新增232；T01—T28实际执行映射及复用范围。映射不单独证明正确性。 |
| evidence-real-routing.log / .xml.gz | 新增真实过滤/混合切换8例通过65.52秒；最终全量再次执行。混合首选503为本地受控，不是供应商事故。 |
| evidence-capacity-summary.json / evidence-capacity.json.gz | 1/5/10/20各60秒，选择20并发持续600秒；合计15886请求完成，0失败，固定事实质量异常0，所有终态/事件/账务/余额核对；最终请求/Agent/生成/订阅/清理/取消归零。两条真实协议共用DeepSeek，不是独立供应商SLA或正式账单对账。 |
| evidence-raw.tar.gz / evidence-raw-manifest.json | 35976份原始安全上游/观察/容量工件及逐文件摘要；凭据、密码、.env、配置副本、symlink排除；真实API Key值泄漏检查未发现匹配。可用只读脚本抽查/核对，不一次输出巨型JSON。 |
| evidence-development.tar.gz | 43份开发原始日志及辅助映射；保留配置/首业务Red、精确小数恢复Red、元数据回归、旧夹具迁移失败、两次真实setup错误，不将它们改写成通过。最终结果独立归档。 |
| evidence-verification.json | 最终XML、源码/测试未变、私有文件保持及130历史文档摘要核对的执行者回执；不是审查结论。 |

开发中首Red使用历史fixture附加未来enabled字段，暴露主选停用仍被调用；最终fixture改成真正三层定义，原backup/zero-primary断言保留。精度Red确认默认退避Decimal('0.1')与float直接比较误报配置变化，恢复时将当前配置置于同一精确JSON数值域；不降低Schema/金额精度。相关原始回执均在开发包。

部分旧用例前提按已确认F12变化调整：超预算边界测试隔离单端点、跳过码细分、故障注入移实际Router owner、旧pending保守失败；原nodeid和业务/零副作用断言保留，具体映射可核查。请独立判断这些调整是否合理，是否有测试掩盖缺陷，不因上述解释默认接受。

## 6. 审查约束与结论

只读、不修改文件、不运行测试/产品或付费调用；允许只读脚本核对摘要及解析JSON/XML/压缩证据。通用工具仍有写入/执行能力，只读是任务约束，不宣称工具强制限制。需要最小复现实验请交执行者运行并回传原始回执后再复核。

依据项目三维审查模板独立检查需求、实现一致性、系统正确性，主动提出已有测试之外的反例/故障时序。等级按当前实际影响P0—P3；P0—P2及关键证据缺口阻断，普通未来范围限制不自动阻断。允许未发现问题，不凑数量；不得因2497通过预设APPROVED。

直接返回原始报告，不写报告文件；执行者原样归档。报告含三维覆盖、独立反例、具体文件/行及可验证问题、已读/未读/未重跑来源和APPROVED/BLOCKED理由。执行者在审查前后核对固定候选摘要，后续处置/修复另记录，不覆盖原报告。
