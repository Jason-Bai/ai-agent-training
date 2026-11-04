# F9 代码改动导读

以“售后 Agent 查订单 A123，第一次调用超时，备用成功”为阅读顺序。

1. **归属**。从 `src/admin.py` 的 CLI 进入 `src/api/identity.py`。管理员签发凭据，API 校验 Bearer，并将已验证的 agent_id 交给请求记录。订阅/取消在 FastAPI 实际命中的端点读取已解析 ID 并校验归属，Trace 与账务查询在 SQL 中按 Agent 限定。创建、首次订阅、取消和账务查询都经过这层。换钥匙不改变请求归属。
2. **报价**。`src/services/prices.py` 读显式价格配置。每次尝试用当前候选、输入估计和本次实际输出上限单独报价。预留包含本次截止前已配置的所有价格版本中的最高适用费率。在起止期间可能遇到不同价格且没有充分依据时进入待核实。价格生效版本、币种、时段和金额随尝试记录。
3. **预留**。`src/persistence/store.py` 的 begin_attempt 在同一事务插入 Attempt 并调用 `src/persistence/billing.py` 的 reserve。余额不足，事务回滚且上游零调用。并发共享同一账户的预算锁。F7 每次重试、切换模型和扩大输出预算都重新经过这一入口。
4. **结算**。普通、流式适配器解析独立的缓存分类并传入 AttemptTrace。费用还要求终态实报的 UTC 观测时间 billing_observed_at，入账延迟不会改变调用所处的价格时段。Store 在提交 Attempt 或流终态时调用 settle。失败有实报也能产生费用；失败无实报时不丢弃预留。重启后 recover_billing 收口未决尝试，不冒充已知用量。账本用 PostgreSQL NUMERIC 保存精确金额。
5. **核查**。`src/api/billing.py` 的 summary、list_requests、request_details 调用 `src/persistence/billing_query.py` 提供三个观察层次，Mock 单独归类。known_subtotal 只累计完整终态实报，中间观测计数仍可在 Trace 查看但在账务总览记为未知。usage_complete 与账务状态分别表达完整用量和金额能否确认。查询不会触发生成或补扣。CLI 的 resolve 只处置符合条件的单笔尝试，statement 记录供应商账期总额并核对同期间的已知成本，未归属差异保留。迁移文件 `migrations/versions/f9_001.py` 保留原 F8 行的空归属。
6. **故障定位**。401 检查凭据状态，404 检查归属，402 查看可用余额和预留，503/price_unavailable 查看绑定和生效版本。账务 pending 查看单次尝试的模型、Usage、缓存分类及价格窗口。先对比原始上游证据和 Attempt，再比较账本及查询视图。

[设计](design.md) · [测试与审查记录](test-plan.md) · [本地使用示例](../../../README.md)

终态计量使用 constants.CHAT_TERMINAL_REASONS 统一普通与流式识别，后续业务正文拒绝仍保留完整计费证据。[最终修复及验收](../../bugs/F9-B1.md)。
