# F13-O1 实施步骤

> 2026-11-04，设计和测试方案分别获用户确认。采用 TDD，最终全新 Agent 独审；不修改 AGENTS，不初始化 Git。

## 目标与接线

统一 gateway.yaml.pricing，保持 PriceBook 金额及历史快照语义；main 仅负责组件构建和接线，将补偿与生命周期分别放入 persistence/reconciliation.py、lifecycle.py，分类启动校验放回 classification.py；README 面向项目使用者。

## 文件和资源归属

| 执行者 | 修改范围 | 依赖及限制 |
| --- | --- | --- |
| pricing Agent | services/prices.py、models.py、配置示例及本地非敏感价格迁移、既有测试价格构造、新配置测试 | 独立先 Red 后 Green；仅纯逻辑定向测试，不启停 DB、不运行 live |
| README Agent | README、architecture 中对应职责和使用说明 | 接口及目标模块已确定；不执行 DB/真实调用 |
| root | main、lifecycle、reconciliation、classification、对应特征测试及本项文档/证据 | 统一集成；独占数据库、完整回归及真实执行资源 |

本机临时基线位置由 /tmp/llmgw-f13o1-work-path 记录，含实际源码/测试/安全配置清单及哈希。未把真实凭据放入基线。

## 执行清单

- [x] 冻结现有源码和测试的真实基线。
- [x] 价格分区先取得读取与拒绝场景的正确 Red，再统一加载路径和测试配置构造；同一手算期望验证 Green。
- [x] 为旧 main 的启动/关闭顺序、补偿交错、取消/异常编写特征测试，先证明旧契约。
- [x] 最小提取 PersistenceReconciler 与 lifespan，保持同一批特征测试通过；分类校验移回已有组件。
- [x] 合并唯一完整示例、更新 README/架构及受影响路径，确认当前配置值迁移等价。
- [x] 收集全部旧/新节点并核对无丢失；定向、集成后完整 --run-live，隔离数据库及凭据，全部故障测试串行。
- [x] 在隔离副本核验 README 快速启动命令及 API 示例；不覆盖维护者配置。
- [x] 形成实现导读、真实差分和原始回执；一次自检，归档首轮实际审查提示词。
- [x] 全新上下文独立 Agent 三维只读审查，按原场景复测闭环，最多三轮；同步最终结论。

既有未变协议/质量规则不重新设计；测试失败只复测原场景与影响链。真实供应商失败按既有分层标准保留，测试未执行不能称通过。
