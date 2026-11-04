# F13-O1：统一配置与入口整理——代码实现导读

> 状态：验收通过；同一首轮独立审查 APPROVED。对应[设计](design.md)、[测试及证据](test-plan.md)。2026-11-04 更新。

## 1. 行为变化

价格由独立 prices.yaml 迁入 gateway.yaml.pricing。完整百炼三模型、路由和价格统一在 gateway.example.yaml 中；重复示例移除，凭据及数据库密码仍在独立本地文件。旧价格源不回读，缺价格仍按原 price_unavailable 处理，不当成免费。

main.py 从 167 行整理为 70 行，保留工厂签名、依赖注入、组件构造、app.state、路由和 Middleware 接线。生命周期和持久化补偿各有明确归属，原业务规则不改变。README 以运行项目为主，历史状态和验收证据继续在 progress/features。

## 2. 核心调用链

`create_app → validate_classifier_prompts → TokenBudget → ProviderClients / Database / PriceBook / Store → PersistenceReconciler / StreamingManager → FastAPI 注册`

启动：Database.start → Store.recover → 周期恢复任务 → ProviderClients.open → 提供服务。
关闭：StreamingManager.close → ProviderClients 退出 → 停止周期任务 → 有界最终恢复/补偿 → Database.close。补偿不重新调用模型。

## 3. 阅读顺序

| 顺序 | 文件与函数 | 关注点 |
| --- | --- | --- |
| 1 | [main.py](../../../../../src/main.py) `create_app` | 每个组件怎样构造、共享哪些集合和回调、请求依赖如何取得 |
| 2 | [lifecycle.py](../../../../../src/lifecycle.py) `application_lifespan` | 正常/异常启动、恢复任务取消、关闭顺序和最终补偿时限 |
| 3 | [reconciliation.py](../../../../../src/persistence/reconciliation.py) `PersistenceReconciler` | 延迟登记、活跃/取消保护、提交核对、旧项不能删除新项 |
| 4 | [classification.py](../../../../../src/services/classification.py) `validate_classifier_prompts` | 分类器角色和变量协议的启动检查，不负责实际调用 |
| 5 | [prices.py](../../../../../src/services/prices.py) `PriceBook.load` | 从统一分区取价格；内部仍用原 Catalog、Decimal、版本和快照 |
| 6 | [models.py](../../../../../src/services/models.py) `load_registry` | 接纳 pricing 分区；端点/组校验仍由注册表负责 |
| 7 | [gateway.example.yaml](../../../../../config/gateway.example.yaml) | 连接、端点、组、自动入口与价格的完整配置关系 |
| 8 | [新配置测试](../../../../../tests/unit/test_f13o1_configuration.py)、[生命周期测试](../../../../../tests/unit/test_f13o1_lifecycle.py)、[补偿测试](../../../../../tests/unit/test_f13o1_reconciliation.py) | 正确 Red/Green、旧行为特征和受控交错如何验证 |

## 4. 关键取舍

文件统一不等于职责混合。PriceBook 校验价格和报价，ModelRegistry 校验模型与组，lifecycle 只编排已有组件，Store 继续负责事务与落库规则，没有新增通用容器或运行模式。

每个应用有自己的 active/generations/pending。补偿会跳过仍执行或取消清理中的请求；流创建提交不确定时先查数据库；处理期间若同一 ID 登记了新项，旧处理只删除自己那一项。数据库不可用时保留待重试，取消不能被错误吞掉。

测试侧所有价格构造迁移到 pricing；历史普通测试仍使用真实 YAML 加载器加载隔离文件。只有 live 标记且显式 --run-live 的用例允许读取真实接入配置；ENV 不决定这个选择。

## 5. 定位方法

新克隆启动失败先看 gateway/凭据/密码文件和迁移版本。价格非法查看 PriceBook.load；报价缺失看 pricing.bindings、模型名、生效版本，不去旧 prices.yaml 排查。

请求运行失败按 X-Request-ID 查询 Trace；原错误码、Attempt、Usage 和账务规则仍沿用已有接口。数据库暂时失败后的核对看 PersistenceReconciler；进程启动恢复或退出次序看 lifecycle；上游故障仍在 Provider 与尝试执行器处理。

## 6. 当前验证及边界

旧 main 的 13 个特征测试先通过，拆分后相同断言通过；新增关闭/异常/取消场景后 17 项通过。价格配置及原计价回归 201 项通过，包含正确失败后修复的读取分区和 live 隔离测试。额外分类启动检查 5 项已执行通过。

完整批次2964通过/8个测试迁移失败，104个live全通过；修正测试后相关1561项通过，原8失败均已通过，分批覆盖2972个唯一节点，不能称一次全绿。README真实进程22步通过，固定容量9258笔成功。产品源码与完整批次未变，最终复核证据见测试方案与evidence.json；[同一首轮独审APPROVED](review-r1.md)，P0—P3均为0；既有F13容量失败及限制不改写。
