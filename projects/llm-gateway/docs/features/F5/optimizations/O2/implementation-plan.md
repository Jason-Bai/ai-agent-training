# F5-O2 实施步骤

2026-11-02：设计与32组测试方案均已由用户确认；两条真实接入调用已授权。实现采用现有三表，不新增迁移、重试或取消接口。

1. 固定当前源码/测试/安全配置快照，串行执行历史全量基线。验证：保存原始输出及退出码。
2. 存储增量（Store 执行者）：先真实 PG 测试失败，再实现 pending 创建、条件认领、分页读取，保持旧终态事务。文件：`src/persistence/store.py`、`tests/postgres/test_f5o2_storage.py`。
3. 订阅读取（Subscription 执行者）：独立单元测试先行，实现有界页、心跳、独立发送期限、断开清理。文件：`src/services/subscriptions.py`、`tests/test_f5o2_subscriptions.py`。与步骤2在基线完成后并行。
4. 主集成者：先写创建/订阅 HTTP 测试；实现 manager 持有生成任务，复用 StreamingExecution 的 Provider 消费，接线路由、Middleware、生命周期及公共常量。公共文件只由主集成者修改。验证：零调用、20并发仅一次、快照、状态和 TTFT。
5. 集成稳定后，独立进程/TCP/PG测试验证首段、续传、发送故障、未知提交和停启恢复；数据库停启套件串行。历史测试只适配明确的 POST→202/GET 契约变化。
6. 串行全量及3.12关键回归；两条显式真实 Chat/Responses 调用各一次，临时配置，默认 Mock 保持。按实际结果映射32组，不以历史通过冒充本项。
7. 同步代码导读和相关文档，保存真实差分及证据；原生 Claude Code 三维独审，最多3轮，修复后针对性复测。通过后直接报告，不另设例行开始/验收许可。

共享接口：Store.create_pending / get_stream / claim_stream / read_events；订阅只读取PG，生成只写业务事件。主集成者管理 active generation 与 HTTP 两类生命周期，避免创建响应故障覆盖生成状态。

验收：见同目录 test-plan.md。临时基线/原始日志由 `/tmp/llm-gateway-f5o2-work-path` 定位，交付证据需归档至本目录；临时路径不代替最终回执。
