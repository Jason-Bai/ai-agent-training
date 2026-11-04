# F8-O1 代码导读

1. 从 `src/providers/mock.py` 的 `MockProvider.complete` 和 `MockProvider.stream` 读起。只有生成默认成功结果时才使用固定的 Usage（10 / 1 / 11），它是不可变的 Pydantic 值对象。显式结果、脚本或错误仍由调用者提供。两条真实适配器没有引用这个对象。
2. 通过测试查看传递过程。`tests/integration/test_f8o1_mock_usage.py` 发起 A123 普通请求或创建并订阅流，独立读取 PostgreSQL，再调用诊断 GET。样本值在测试侧独立定义。既校验 SSE 重放没有多计，也校验失败后备用 Mock 成功时仅累计已知部分。
3. 如果新建的默认请求仍显示未知，先按 request_id 核对 Attempt 的 adapter 和终态。若 Mock 返回值已有 usage，再比较 Attempt 的三个 token 列和 Trace 的 known_subtotal；历史缺失数据不会因升级而回填。Gateway 的 input_token_count 是单独的预估值。Mock 上报值只用于验收数据传递。

[已确认设计](design.md) · [测试及证据](test-plan.md)
