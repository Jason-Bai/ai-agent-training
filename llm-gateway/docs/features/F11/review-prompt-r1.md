你是F11首轮独立代码审查者，使用全新上下文，不继承实现对话。仓库：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway。

本轮只读：不修改文件，不运行测试、API调用或付费请求，不读取config/credentials.yaml、数据库密码、.env、缓存/venv或其他敏感文件。通用工具并未技术强制只读，这是任务约束。需要实验时报告最小复现，由主执行者执行并回传。不要调用其他实现Agent寻求结论。

先读项目AGENTS.md、docs/AGENTS.md、docs/features/F11/review-material-r1.md、design.md、test-plan.md、implementation-plan.md、implementation-summary.md和docs/templates/独立审查提示词模板.md。材料把真实用户原文、确认后的设计决定、执行者证据分别列明；它们仍需你独立核实，不预设实现或验收正确。当前产品是内部Agent、本机或隔离网络、单实例单worker，后续Router/多实例不在F11范围。

固定基线：docs/features/F11/evidence-baseline.tar.gz是实施前真实安全快照；本轮候选evidence-source-manifest-r1.json、实际累计差分evidence-review-changes-r1.diff及evidence-review-changes-r1.json。无Git，不把文件清单冒充变更证明。核对候选摘要，沿差分读取完整调用链，确认审查结束前后源码/测试/安全配置是否变化。不要打开实际运行配置；需要比较配置时用已归档的安全基线和diff，主执行者另已核对运行YAML只追加20/5，配置/保护证据在evidence-protection.json。

必须按需求正确性、实现一致性、系统正确性三维审查，尤其：两层原子准入/无部分获取；Agent来源和跨凭据/模型计数；重试/修复/输出增额/fallback全程只持有一份名额；普通/探测/流式共享实例；pending拒绝无污染；认领ACK不确定、建Task失败、首次字节码前取消、取消与清理重叠、清理超时仍活跃和迟到Task退出；重复释放/所有权；资源、账务、日志、Trace和终态回归。不要只看测试是否通过，独立推导反例和时序，并检查测试替身/断言/观察是否掩盖错误。

实际回执：evidence-development.json；evidence-node-mapping.json（原2086保留、新72）；evidence-real-public-raw.json.gz（单独真实六例原观察）；evidence-full-first.json、evidence-full-first.xml.gz、evidence-full-first.log；evidence-capacity-first.json.gz及evidence-capacity-wire-first.json.gz；evidence-real-recheck.xml.gz/log；evidence-direct-upstream.json。主执行者实际运行了2158节点，2153通过/5个真实节点失败，34 live、0跳过/筛除；全部2124非live通过，新F11真实六例通过。容量因单并发34请求中的1个上游读取总超时未达门槛，后续5/10/20档和600秒未执行。后来原代码八个真实复测均失败，旁路同一上游HTTP请求返回503。请核实，不把旁路失败等同所有超时归因已确定；也不能把未通过的全量叫通过。

独立区分代码缺陷、外部可用性及证据不足。真实验证仍阻塞，因此关键容量证据未补齐时不得判整体验收APPROVED；可分别报告代码三维发现与整体验收就绪状态。不要为了消除外部失败擅自建议放宽已有门槛或假装有生产证明。每个发现给文件/行、触发条件、影响、违反契约、最小验证方式及建议，并标注已复现/静态确定/待验证。无发现不凑问题。

最终报告逐维覆盖、独立反例与核查结论、实读/未读范围、工具和验证来源、摘要核对结果、P0/P1/P2/P3数量、关键证据缺口及明确VERDICT。测试是主执行者运行的，不能声称你执行了测试。首轮完整审查最多三轮规则适用，当前为第1轮。将完整报告作为最终回复回传，由主执行者按原文归档。
