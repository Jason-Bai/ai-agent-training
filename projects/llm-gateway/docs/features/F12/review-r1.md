F12 第 1 轮完整独立审查结论：**BLOCKED**。发现两项 P2，均由静态分析提出，再由执行者使用 Mock 最小复现确认。2497 项完整回归通过属实，但未覆盖这两个组合边界。

## 1. 三维覆盖

| 维度 | 审查对象与依据 | 结论 | 未验证边界 |
| --- | --- | --- | --- |
| 需求正确性 | 用户原文摘录及执行者整理、F12 设计/测试方案/实施计划/导读、当前接口增量 | 模型组与 F13 任务分类分开，三层配置、必填 model、有序候选及排除项总体合理；探测固定预算和渲染阶段有两处未兑现 | 没有完整聊天导出，无法独立核实每条“确认”的完整上下文；未将整理内容冒充用户逐字原文 |
| 实现一致性 | Registry、Router、逐端点消息/计数、普通及流式候选、快照恢复、Trace、测试迁移和真实协议断言 | 核心计划被实际执行器使用，备用采用自身消息、计数和实际接入；两项确定缺陷见 Findings | 本审查不重跑测试；已有通过数量不能证明未测组合成立 |
| 系统正确性 | F7 次数/期限/等待域，F11 原请求名额，流式认领/取消/提交边界，F9 逐 Attempt 报价与结算，Store/历史 Trace/Provider 生命周期 | 未发现本轮其他确定的系统阻断项；相关配置检查包含已排除端点，历史读取不依赖重新路由，请求恢复状态为局部对象 | 单实例单 worker；两条真实协议共用 DeepSeek；没有独立供应商容灾、多实例或正式供应商账单证明 |

## 2. Findings

### F12-R1-01 — P2：探测未拒绝不支持固定超时预算的首个启用端点

**状态：静态确定；执行者已复现，审查者已核对原始回执。**

位置：[routing.py:161](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/routing.py:161)，相关执行路径为 [gateway.py:114](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/gateway.py:114) 及 `_ordinary_attempts`。

触发条件：组允许固定探测预算，首个启用端点 `max_timeout_seconds=1`，小于固定探测超时 10 秒。

Router 检查了组超时、端点输出上限和上下文，却没有检查探测固定超时能否被该端点承接。执行器随后按端点上限截短为 1 秒，实际调用 Provider，甚至可返回探测成功。

执行者回执确认：

- `primary_calls=1`，`backup_calls=0`。
- 实际传给 Provider 的 `timeout_seconds=1`。
- 产生一个 completed Attempt，探测返回成功。
- 名额最终归零。

这违反 F12 设计 3.1“固定探测文本及 Token/超时预算并检查组/端点限制；限制不满足直接报错”，也违反 F3 设计 3.7“模型上限不允许 16/10 时调用前明确拒绝”。影响是本应零调用拒绝的探测发生真实调用；真实接入下还可能产生费用。

最小验证：沿现有探测测试，将首端点限制设为 `max_timeout_seconds=1`，调用 `probe`；应在调用前返回明确预算错误，主备调用、Attempt 和费用预留均为零。

最小修正：仅对探测选出的首个启用端点检查固定超时预算，不更换端点；普通生成继续沿用每次尝试取端点上限与剩余期限较小值的规则。补充小于、等于、大于固定探测超时的边界断言。

原始证据：[复现脚本](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F12/evidence-review-probe-timeout-repro.py)、[执行回执](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F12/evidence-review-probe-timeout-repro.json)。运行者为执行者 root，Mock、无网络、退出码 0；不是审查者重跑。

### F12-R1-02 — P2：文本请求提前渲染，绕过约定的路由阶段和诊断

**状态：静态确定；执行者已复现，审查者已核对原始回执。**

位置：[routing.py:112](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/src/services/routing.py:112)，相关路由阶段位于同文件 159–188 行。

触发条件：不带 Schema，使用合法的 `context_answer@1.1.0`，context 和 user 各 10,000 字符，各自字段合法，但合并渲染超过 User 字符上限。

`schema is None` 时，代码在查组、检查启停/能力/输出之前调用 `build_messages`。超限直接抛出 `prompt_too_long`，无法进入后面的 `stage(render, ...)`。

执行者复现两个变体：

| 条件 | 实际结果 | 按已确认设计应有结果 |
| --- | --- | --- |
| 两端点全部停用 | `prompt_too_long`、routing=null、error_stage=null | 启停阶段耗尽：`no_available_endpoint`，routing 解释停用原因 |
| 两端点正常启用 | `prompt_too_long`、routing=null、error_stage=null | 渲染阶段耗尽：`prompt_too_long`，候选记录对应排除原因、selected=null、error_stage=routing |

两种变体调用和 Attempt 均为零。问题没有造成额外调用，但公开错误优先级及持久化诊断不符合 F12 设计 2.3、3.2、3.3 和当前 contracts 增量；普通文本与结构化路径表现也不一致。现有 `test_all_rendered_prompts_too_long_fail_before_counting` 只核对错误码和未计数，没有核对阶段及 routing，因此没有发现该问题。

最小验证：使用上述输入，分别设置全部停用和正常启用，检查错误、routing、error_stage、零调用及拒绝正文不落库；普通与流式创建入口都应覆盖。

最小修正：保留前置模板/变量合法性检查，将实际文本渲染纳入约定的渲染阶段。可以缓存共享文本的渲染结果，但不能提前改变阶段耗尽错误或遗漏候选诊断。

原始证据：[复现脚本](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F12/evidence-review-render-stage-repro.py)、[执行回执](/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F12/evidence-review-render-stage-repro.json)。运行者为执行者 root，Mock、无网络、退出码 0。

## 3. 独立反例与故障时序检查

| 反例或时序 | 检查结果 |
| --- | --- |
| 探测固定 10 秒，但首端点上限只有 1 秒 | 确认缺陷 F12-R1-01 |
| 合法字段合并后超长，同时全部候选停用 | 确认缺陷 F12-R1-02 |
| Chat JSON mode 消息较长而原生 Schema 候选仍合法 | Router 逐端点组装、计数；新增测试和受控协议证据支持备用使用自身消息 |
| 首选两次瞬时错误后切换，备用价格与首选不同 | 执行候选保留实际 Provider/model；Store 按 Attempt 取价，费用测试分别断言预留与结算，未知 Usage 保持 pending |
| pending 创建后只修改被排除端点 | 配置快照覆盖全部引用端点，首次执行拒绝变更，不重新规划 |
| 同配置经过精确 JSON 存储，再首次执行 | 当前配置使用相同精确数值域比较；开发 Red 和后续测试证明曾经的 Decimal/float 误判已修复 |
| 流式首增量已入库、发送前失败，再出现可恢复错误 | `content_committed` 在事件提交后设定，恢复决策停止，不能拼接另一端点回答 |
| 多请求共享端点并交错完成 | 恢复计数、候选消息、路由说明均为请求局部状态；并发测试按请求 ID 断言，不按完成顺序关联 |
| 历史终态在关联配置删除后补读 | 订阅先处理终态，绕过 `_restore`；旧 Trace 无 routing 时投影 null |

三候选遍历等更大组合的静态检查没有发现额外确定缺陷；未将静态推导写成实际运行证明。T01—T28 映射主要按测试文件或函数集合组织，不等于表内每个变体均有独立断言，修复时应补本次暴露的具体组合。

## 4. 材料、证据与工具边界

已读取两份 AGENTS、独审模板、需求来源索引、F12 设计/测试方案/实施计划/导读；核心生产调用链完整源码及必要上下文；真实差分中的配置、API 接线、持久化、Trace、历史测试迁移与共用取证变化；全部新增 F12 测试。重点核对了 `tests/registry_helpers.py`，其转换仅在测试侧，生产 Registry 不接受旧 YAML。旧 pending 断言调整符合本次明确的保守失效设计。

核对结果：

- 本轮候选清单 **373 份**，审查期间两次哈希核对均无不一致。
- 真实基线归档中 **77 份已有变更文件**的内容摘要与差分索引一致；新增文件按当前源码读取。
- 完整 JUnit 确有 **2497 testcase，42 个 live，0 failure/error/skipped**；控制台为 `2497 passed in 1984.98s`，命令回执退出码 0。
- 新增真实路由独立批次 JUnit 为 **8 通过**；最终全量再次包含这些节点。抽查其 Trace 属性，过滤选择和受控失败后真实备用的实际端点/协议一致。
- 容量报告包含 **15886 笔**；持续阶段 20 并发、600 秒、13430 records 与 13430 verified。抽查终态、账务和排空数据；末尾请求、生成、订阅、清理、取消均归零。
- 原始安全归档 **35976 份文件**全部逐内容核对摘要与大小，无不一致；抽读上游原始回执。摘要一致只说明归档完整性，不证明业务正确。
- 开发包抽查了首业务 Red、快照精度 Red 和真实夹具 setup 失败，未把准备错误当作业务 Red。
- 运行配置迁移仅核对脱敏回执；没有读取实际配置或私有备份。

未读取完整聊天导出（未提供），未逐条语义审阅全部 35976 份原始工件，也未把供应商正式账单、多实例或生产表现列为已验证。所有测试、容量和最小复现均由执行者产生；审查者只读文件、解析已有 JSON/XML/压缩归档及核对哈希，没有运行测试、产品、实验或真实调用，没有修改文件，没有读取禁止的实际配置、凭据、`.env`、虚拟环境或缓存。

通用工具技术上仍有写入/执行能力；本轮只读是任务约束，不是工具强制隔离。

## 5. 判定

**VERDICT: BLOCKED**

**P0：0；P1：0；P2：2；P3：0。**

阻断依据为两项已经复现的契约缺陷，不是对既有全量证据真实性的否定。修复后应先以相同场景取得 Red/Green，核对公开响应、持久化诊断与零副作用，再完成相关回归及第 2 轮完整复核。
