继续F12第2轮完整复核（同一审查者，累计最多三轮）。项目根为/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway。保留第1轮任务全部只读、安全、三维审查及不得运行实验/付费调用约束，不预设修复成立或通过结论。

用户要求汇报进度并指出耗时过长，执行者将本轮静态复核与修复后的完整回归并行，减少串行等待。重要：完整回归正在执行，目前尚无最终结果；请先核对代码、定向证据和影响链，不提前判APPROVED。静态部分完成后向执行者发消息并等待最终回归证据，收到实际回执再完成本轮报告；这属于本次第2轮的证据补齐，不另开新一轮或重复审查。

本轮变更和固定版本：docs/features/F12/evidence-review-source-manifest-r2.json固定213份源码/测试/安全配置；evidence-review-changes-r2.json/.diff为相对第1轮的实际源码/测试差分，evidence-review-baseline-r2.tar.gz保存相关第1轮旧内容并核对原哈希。只有src/services/routing.py、tests/unit/test_f12_recovery.py和新增tests/integration/test_f12_review_boundaries.py改变；其余第1轮源码/测试证据可按哈希复用。AGENTS与设计未改。当前test-plan.md第6节和implementation-summary.md说明处置，请核对而非默认接受。第1轮原始报告review-r1.md完整保留。

两项处置：探测固定超时不被端点上限截短后继续调用，首个启用端点无法承接则invalid_timeout，不切换；普通生成仍取端点上限与剩余时间较小值。共享合法文本仍缓存并保留早期已确认指纹，捕获prompt_too_long后仅标记缓存不可用，等render阶段正式排除，不能抢先覆盖启停/能力/输出阶段错误。请重点判断这一最小缓存修复是否满足阶段、诊断和原有已知元信息要求，而不只检查新增断言。

实际运行者为执行者root。修复前最小复现见evidence-review-probe-timeout-repro.py/.json及evidence-review-render-stage-repro.py/.json。新增11个边界节点先正确失败：evidence-review-r1-red.log及-receipt.json为7 failed/4 passed/13 deselected，4.73秒，退出1；修复后原场景与既有路由/恢复合计78通过，7.80秒、退出0，见evidence-review-r1-green.log及-receipt.json。包含普通/流式HTTP、PG请求/Attempt/事件/账务、接收方错误信封及零Provider副作用。检查用例质量及断言黄金来源，不把数量当证明。

修复后完整--run-live覆盖2508节点（原2497加11），包括全部42个真实节点、四档和600秒容量，不跳过或筛除。运行中的临时目录由/tmp/llmgw-f12-work-path指向，full-r2.log只读可查看，禁止读取其中配置/凭据或导入执行产品。最终执行者将提供evidence-full-r2.json/.log/.xml.gz、evidence-full-r2-source-manifest.json、evidence-verification-r2.json、evidence-capacity-r2及原始安全工件；这些文件在实际执行结束前不存在，不补造。首轮2497证据依然只证明首轮候选版本，不能冒充修复后全量。

请复核两项Findings闭环及完整影响链，独立推导组合反例；未变覆盖列明复用依据。如需实验由执行者运行，审查者不改文件、不运行测试/产品/实验或真实调用，不读取实际配置、密钥、.env、.venv和缓存。待关键实际证据齐全后按同一模板直接返回原始报告，含三维覆盖、Findings、反例、未验证项、实际读取/证据来源、VERDICT及P0—P3数量，不写报告文件。P0—P2及关键证据缺口仍阻断，允许未发现新问题。
