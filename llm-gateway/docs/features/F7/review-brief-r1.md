# F7 独立审查材料：第1轮

## 来源与用户约束

本文件为执行者整理的审查输入，允许质疑设计、测试和实现。完整历史对话不在仓库，不补造逐字记录。当前对话原文摘录：用户询问“单次尝试默认最多10秒”的含义；随后认可取模型配置单次上限和请求剩余时间较小值，并分别对完整设计及独立测试方案回复“确认”。此前明确F6验收通过后开始F7。其余范围以已确认design/test-plan及项目工作流为材料来源，不将执行者转述冒充用户逐字原文。

用户约束：文档先行、两道确认后TDD、Mock真实协议统一、Provider只单次调用、首内容后不得静默恢复、单实例单worker/隔离开发网络、使用全新无实现上下文Agent独立审查、最多三轮、不得无确认修改AGENTS、不使用Git、不自动付费调用或读取实际凭据。

## 本轮材料和真实变更

项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway。

- 必读design.md、test-plan.md、implementation-summary.md及README/contracts/architecture的F7增量；F6取消边界可读../F6/design.md及相关源代码，不必机械重复旧巨大证据。
- baseline、逐文件哈希、全部源码/测试/安全配置实际diff、52组到节点映射、命令和原始输出见同目录evidence.json。基线为本次改动前复制的F6已验收文件，非Git；新增文件diff基线为空。
- 运行证据由根执行者及实现Agent产生，审查者未重跑。默认live跳过与本地SDK/PG/TCP证明分开，不声称真实供应商/多实例/生产验证。
- 完整回归与后续差分若版本不同，按evidence中的实际快照、delta及最终受影响回归判断，不把重叠批次相加。
- 早期失败、主动中断及调试日志保留；临时同名lifespan调试日志覆盖的历史缺口已在test-plan3.5说明，后跑证据不代替被覆盖原件。

## 审查范围

contracts输入，models/recovery配置及纯决策，Gateway普通恢复及探测，StreamingExecution/Manager执行与每次取消准入，Provider安全错误和两协议适配，Store/Table/migration，以及main/Middleware延迟保存。全部变化及相关调用链均需三维核对；测试同样是待审材料。

特别区分Request最终结果、每个Attempt真实结果、PG提交、发送及实际Provider进入。权限及业务动作仍在Agent/应用侧；不扩展Router、跨请求限流、计费或多实例。正常明确未执行的范围外能力不自动阻断，当前范围内缺陷不能以未来Feature掩盖。

只读取安全源文件、测试、上述文档和config/gateway.example.yaml；可读证据中明确列出的安全基线/差分。不得读取credentials.yaml、postgres.password、.env、.venv或缓存。记录工具真实能力：只读为任务指令，非工具强制限制。

需要实验时提出具体最小输入/时序/待观察项，由根执行者执行并回传；审查者不修改、不运行测试、迁移、应用或API。没有关键验收证据不得Approved，允许直接静态确定问题，也允许无问题。

返回三维覆盖、独立反例/时序推导、P0–P3 Findings（文件行号/触发/影响/契约/最小验证及修正）、实际读取和未覆盖/未重跑项、VERDICT及分级数。P0–P2阻断，P3应有处置；后续同一Agent复核，总共最多三轮。
