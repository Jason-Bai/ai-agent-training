# F6：主动取消实施步骤

> 完整设计与33组测试方案均已于2026-11-02获用户确认。使用writing-plans、TDD及独立审查工作流执行；无Git操作，不修改AGENTS.md，不额外付费。

目标：POST发起及轮询取消；应用拥有取消任务，以数据库canceling提交为接受屏障，完成停止/有界清理后保存唯一终态。

## 任务与文件归属

| 任务 | 所有者 | 可修改文件 | 前置与验证 |
| --- | --- | --- | --- |
| 事务及迁移 | f6_store | src/persistence/store.py、tables.py、database.py；migrations/versions/f6_001.py；tests/postgres/test_f6_storage.py | accept_cancel返回状态或None；finish_cancel保存取消终态；先真实PG Red，再Green，保护原迁移与数据 |
| 入口、任务及恢复统一接线 | root | src/api/routes.py、contracts.py、services/streaming_manager.py、streaming.py、main.py；必要constants.py；tests/unit/test_f6_execution.py；tests/conftest.py | 先信号/清理失败测试；确认失败后实现；不让普通finish/恢复抢写canceling |
| HTTP/竞争/重放/存储故障场景 | f6_http_tests | tests/integration/test_f6_http.py、test_f6_races.py、test_f6_replay.py、test_f6_faults.py；tests/f6_cases.py | 接口404的正确Red；实现就绪后Green；用屏障/独立SQL证明时序 |
| 网络/协议/进程边界 | f6_boundary_tests | tests/smoke/test_f6_process.py；tests/integration/test_f6_protocol.py；tests/f6_boundary_support.py | 独有PG/端口/子进程，先Red；本地真实适配器；PG停启必须另约串行时段 |
| 集成、证据、导读及独审 | root | 本项目录设计/测试/证据/导读/审查材料及README/架构/契约/进度 | 各项目标与相关回归通过后必要全量及严格检查，建立33组节点映射，首轮全新只读审查Agent，最多三轮 |

存储内部契约：`accept_cancel(request_id)->str|None`原子接受/查询；`finish_cancel(trace, attempt=None, *, error_code=None)->str|None`仅取消所有者可由canceling保存canceled/failed。普通finish不得覆盖canceling，delta仅running可写。管理器协调取消任务与生成任务注册，重复轮询不创建第二个操作。

## 执行清单

- [x] 保存源码/测试/安全配置和历史文档基线，排除密钥，记录哈希及真实差分。
- [x] 写各阶段测试并保存正确Red，避免收集错误冒充缺功能。
- [x] 实现存储屏障、显式迁移并通过同场景PG测试。
- [x] 实现应用持有取消操作、停止信号、清理与结果保存，接入路由及延迟持久化/退出恢复。
- [x] HTTP、竞争、续传、协议测试Green；发现故障先复现再修复。
- [x] 串行执行PG停启与进程故障；目标用例/相关回归通过后必要全量和3.12严格检查。
- [x] 对照33组及参数化分支记录实际节点、原始回执和未验证项，完善代码导读及必要文档。
- [x] 冻结审查快照及真实差分，归档实际提示词，请全新上下文Agent三维只读审查；按工作流修复闭环。
- [x] 核对AGENTS/凭据/默认配置及历史工件未改，报告有证据的最终结论。

计划命令及测试层次沿用[test-plan.md](test-plan.md)。已通过检查只因相关变化/失败重跑，不重复启动同目的评估；共同文件仅root集成，共用资源故障串行。独立任务运行结束须自行关闭测试资源，不删除用户数据。
