# F7 独立审查索引

第1轮使用全新无实现上下文Agent；第2轮由同一Agent复核。只读是任务约束，非工具强制限制。累计最多三轮。

| 轮次 | 实际输入及回执 | 原始报告与处置 |
| --- | --- | --- |
| R1 | [提示词](review-prompt-r1.md)、[调用记录](review-invocation-r1.json)、[材料](review-brief-r1.md) | [原始报告](review-r1.md)：BLOCKED，3项P2；[复现实验](review-experiments-r1.json)，[处置/回归](test-plan.md#37-第1轮独审修复与第2轮提交) |
| R2 | [提示词](review-prompt-r2.md)、[调用记录](review-invocation-r2.json)、[冻结版本/差分/最终相关回归](evidence-r2.json) | [原始报告](review-r2.md)：APPROVED，P0–P3均0；[最终结论](test-plan.md#38-最终验收与第2轮结论) |

R1前完整回归及原始基线在[evidence.json](evidence.json)。R1报告、已执行提示词及旧证据未被修复说明覆盖；最终验收结论以[test-plan](test-plan.md)为准。
