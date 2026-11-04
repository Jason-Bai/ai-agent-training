请继续F13第1轮独立审查的容量验收补充复核，保留原review-r1.md的BLOCKED历史，不重置轮次。只审查不修改，不运行测试/付费调用，不读实际配置/凭据。

用户已直接回复“采用分层验收，保留失败与限制（推荐）”，问题和答复原文见docs/features/F13/artifacts/capacity-criterion-confirmation.json。请独立核实这个授权是否解除原C10唯一阻断，仍须核查新判据没有掩盖Gateway功能、期限、终态、账务或释放缺陷，不能因用户确认预设通过。

新判据和接线：tests/f13_capacity_metrics.py、tests/unit/test_f13_capacity_acceptance.py、tests/live/test_f10_capacity.py、tests/f13_capacity_followup.py。相对已验证255清单只变测试，所有src产品文件及原tests/f10_metrics.py严格证明器不变。新冻结257文件与实际差分在工作目录layered-acceptance-manifest.json、layered-acceptance-changes.diff；原255清单是trace-fix-manifest.json。工作目录由/tmp/llmgw-f13-work-path读取。

证据：layered-capacity-red.log/receipt（3个正确业务失败），layered-capacity-green.log/receipt（新20项及旧指标/严格故障证明相关共130通过），layered-capacity-reassessment.json（对原两轮六个phase共9785请求的另行复核，原failed及SHA不变，零新增付费调用）。默认收集2937只表示新增20项，不是另一轮全量。产品此前2813非live和17真实影响回归、首次合成分类及两轮容量原始证据均可按已核实的不变部分复用。

请核对tests phase原有真实SQL/价格/事件/账户/转发断言仍执行、新判据拒绝本地或不一致失败、最高已测档不称稳定档，及docs/features/F13/test-plan.md第6节的边界。报告具体通过/阻断理由、独立反例和限制；不要重写原严格失败、追认供应商事故或承诺100%稳定。当前README等状态尚未改为通过，等待你的独立结论。请以最终回复提供补充报告原文供root归档。
