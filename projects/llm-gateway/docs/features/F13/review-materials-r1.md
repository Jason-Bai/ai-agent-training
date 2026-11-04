# F13 第1轮独立审查材料

本文件是执行者整理的材料索引，所有设计、测试和说明都须独立核实，不是预设通过结论。实际审查输入单独保存在review-prompt-r1.md。

## 用户需求及确认来源

当前对话中可见用户原文包括：

- “是；你现在是基于阿里百炼，你可以至少选3个模型”
- “是，这里你需要站在整体角度看这次模型调整”
- 完整设计、32组测试方案分别单独回复“确认”。对应落地文档为design.md、test-plan.md，批准的具体技术条款以这些已确认文档核对。
- 实测发现高精度Schema被供应商舍入后，执行者提出保留Gateway精度、舍入失败、按实际消耗记账，逐位字段由调用方声明字符串；用户明确选择“采用，保留精度并明确失败（推荐）”。该确认已加入设计。
- AGENTS修改必须另经用户确认，本次不修改；所有旧用例及真实调用都要实际执行，不因费用跳过；Mock与真实分别验证，各自保持真实证据边界；当前单实例单worker、本机或隔离网络。

早期需求讨论的部分助手逐字内容未包含在当前可见回放中，不能补造完整原文。已确认方案是后续实现依据，审查者仍应质疑遗漏、冲突及代码/测试共同误解。

## 固定基线与真实差分

项目根：`/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway`。

本轮工作目录：`/var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f13-work-eifzl4on`。只允许读取下列明确材料，禁止递归读取整个工作目录：运行中的pytest临时配置子目录可能含真实凭据。

- `baseline/`：实施前213份安全文件；`baseline-manifest.json`为摘要。
- `pre-full-snapshot/`及`pre-full-manifest.json`：本轮源码/测试/安全示例243份冻结副本与摘要。
- `pre-full-changes.diff`及`pre-full-changed-files.json`：真实已捕获前像与当前差分、51项变更索引。新增文件可直接读源文件；不用重复巨型diff和整文件。
- `baseline-gaps.json`：唯一已知缺口是原credentials.example.yaml未捕获前像；仅能审当前安全占位内容，不能将它的新文件式展示当成新增文件证明。源码/测试已修改文件均有前像。
- `collection-final-receipt.json`：原2508节点全部仍在，完整运行收集2891节点。新补充的真实分类取消测试在整套启动后才准备，不假称其包含于原2891；执行者会提供同产品冻结版本的补测和实际新增文件清单。

AGENTS两份摘要与实施前相同。项目不使用Git工作流，以捕获前像、真实差分和冻结哈希提供版本证据。

## 必读范围

1. 项目AGENTS.md、docs/AGENTS.md、docs/templates/独立审查提示词模板.md。
2. 本目录design.md、test-plan.md、implementation-plan.md、implementation-summary.md；当前README.md、docs/contracts.md、docs/architecture.md中F13增量。
3. 全部本轮源码差分和新增源码：main、models、routing/auto_routing/classification、gateway、streaming/streaming_manager、TokenBudget与Counter、prices、Provider base/factory/openai_compatible/bailian/streaming、Trace/Store/TraceQuery/DTO；以及受影响的原billing、Database、Subscriptions/API、Recovery和ProviderClients资源归属。
4. 全部新增/变更测试及安全示例，特别是共享辅助代码、真实用例、OS恢复、原容量节点扩展和Mock/真实/派生失败证据的区别。开发集可读；封存验收集在首次正式真实执行前请只核对摘要和统计，不读取其内容用于Prompt判断或提出调参。首次执行结束后再进行独立标签评价。

只读安全配置：config/gateway.example.yaml、gateway.bailian.example.yaml、prices.bailian.example.yaml、credentials.example.yaml。禁止读实际gateway.yaml、credentials.yaml、prices.yaml、postgres.password、.env、用户CSV、.venv或缓存。

## 已有运行证据

以下均为执行者产生，不是审查者重跑。下列文件在上述工作目录根，可安全读取：

- `red-config*`、`green-config.log`、`red-minimum-kind.log`、`green-minimum-kind.log`；组件`estimation-*`、`pricing-*`、`protocol-*`及auto-config/auto-planning/classification前缀日志/回执，不能机械累加各组件重叠节点。
- `metering-final*`等阶段A PG22；三模型原始最小探测及Adapter Schema摘要；真实首轮36失败原因和原始日志`stage-a-real.*`；第二批`stage-a-real-rerun.*`45过3失败；用户确认精度边界后的`stage-a-precision-confirmed.*`13通过（9真实+4配置）。
- `precision-wire-diagnosis.json`为实际出站数值精确、供应商回显1.0/回答1的直接证据。不能把供应商舍入说成Gateway丢失，也不能将拒绝当成成功输出。
- `auto-routing-red.log`、`auto-routing-first-green.log`；`auto-routing-expanded-red.log`6失败/19过及`auto-routing-expanded-green.log`50通过；`auto-concurrency.log`双Agent乱序和名额核对；`auto-integration-legacy-first.log`240历史相关通过。
- `auto-lifecycle-first.*`5过5失败及`auto-restore-null-red.log`、修正后`auto-lifecycle-rerun.*`10通过；`auto-os-restart-first.*`实际OS3通过。
- `auto-real-development-first.*`原真实3功能+开发30所在4节点失败，450.09秒。原分类30有效29正确，但有端点映射测试错误、受控模板注入不一致及后续生成截断/总到期；不能当整项通过。执行期间辅助测试文件有修改，部分堆栈行文可能来自新文件，原始响应/Trace/批次不变。此边界已记录，后续批次启动前固定源码。
- `auto-real-public-rerun.*`真实普通/Schema/流式三项通过11.57秒，`auto-real-public-rerun-sources.json`留启动前摘要；`auto-real-default-first.*`受控本地503→真实Plus降级及helper两例共3通过5.71秒，分类未知消耗保持pending，生成按真实Usage结算。
- `classifier-first-formal-freeze.json`：task_router@1.0.0代码及开发/封存样例摘要；封存集尚待完整运行首次正式执行，不能拿开发集成绩代替。规则和黄金未因开发错分调整。

## 正在执行和尚缺证据

完整命令记录于`full-r1-started.json`，当前在同一冻结版本串行执行：Python3.12、-W error、pytest --run-live，包含原节点、真实Provider、30开发/30封存和唯一混合容量（六入口×三模式，1/5/10/20各60秒及单600秒）。

可读`full-r1.log`了解进度。结束后执行者提供`full-r1-receipt.json`、`full-r1.xml`、安全提取的质量/容量原始工件及最终哈希对照；目前没有完整成功结论。关键运行证据未齐不能APPROVED。

请先做完整三维静态核对和独立反例推导，有具体疑点及时发给执行者。不要运行测试、实验、真实API或修改任何文件；需要实验给出最小输入/时序和观察项，由执行者串行安排。普通范围外限制不凑缺陷，当前范围内问题不能因已有通过数字而淡化。正式报告必须等关键运行证据到齐再给最终VERDICT；最多三轮，首轮全新上下文，后续由同一审查者复核。
