# F7 实施步骤

> 执行者：使用writing-plans、test-driven-development及独立任务分工；设计和52组测试方案已于2026-11-02分别确认。按项目工作流，不操作Git，不修改AGENTS，不付费调用。

目标：共享有限恢复策略，逐Attempt记录且唯一Request终态；保持流式提交/取消屏障。
技术：既有FastAPI/Pydantic/asyncio/SQLAlchemy/PostgreSQL/Alembic，无新增服务。

## 1. 文件归属与依赖

- 配置/策略执行者：src/services/models.py、src/services/recovery.py，tests/unit/test_f7_recovery.py。输出RecoveryPolicy及Registry.recovery，纯错误/等待决策；不改Gateway/Store。
- 协议执行者：src/errors.py、src/providers/，tests/integration/test_f7_protocol.py。输出安全retry_after_seconds元数据及可恢复故障证据；SDK调用仍单次；不改公共ProviderStreamEvent字段而未协调。
- 集成者：contracts、Gateway、Streaming/Manager、trace、Store/tables/迁移、其余测试、配置示例及文档。共同入口/迁移仅集成者修改。
- 上述两项接口可独立进行；集成待接口稳定后接线。故障套件及涉及PG停启/宿主端口的测试串行，无并行故障套件。

## 2. TDD步骤

- [x] 建立安全基线与原始日志目录，不复制实际凭据。
- [x] K/R/T先写配置和纯策略失败断言，运行正确Red；实现最小配置及共享计数/恢复/等待策略，运行同例Green。
- [x] V/T先写实际适配器+本地服务/传输故障Red；实现安全上游证据、Retry-After解析，核对实际发送次数及Green。
- [x] A/P先写initial预算和Attempt独立结束Red；增加严格协议、每Attempt快照/调用证据、显式f7迁移及finish_attempt，再同例Green。旧迁移不改。
- [x] R/J/G普通HTTP测试Red；冻结候选/原始输入，统一预算与截止，执行候选重装/修复/增长，验证Usage与唯一终态Green。
- [x] S/C流式恢复和重复启动取消屏障Red；接线共享策略、逐次清理保存、非空delta提交屏障，每次启动共享F6准入，运行Green。
- [x] P真实PG提交/回滚丢ACK、迁移、关停/崩溃；N独立TCP心跳/断连重连，按测试方案串行验证。
- [x] 建52组到实际pytest节点的覆盖映射；运行目标严格回归及必要全量。旧功能一次失败预期仅通过测试内显式policy=1隔离，不弱化F7默认测试。
- [x] 更新README/contracts/architecture/progress及implementation-summary，进行一次提交前自检。
- [x] 冻结源码、真实diff、证据及实际审查提示词；全新上下文独立Agent三维审查，最多3轮，问题核实后修复并复测。

## 3. 执行和成功标准

工作目录projects/llm-gateway。首先执行 uv run --python 3.12 pytest tests/unit/test_f7_recovery.py -q 等目标入口，Red须为正确行为缺失而非导入失败；每次关键输出保存在/tmp/llmgw-f7-work-path指向目录，最终归档脱敏证据。

稳定后执行测试方案3.2中的collect、分层测试、python -W error -m pytest -k 'f5 or f6 or f7' -q及pytest -q。全部52组/分支、实际副作用、迁移与旧回归满足且独立审查无阻断才报告验收；未执行保持未验证。
