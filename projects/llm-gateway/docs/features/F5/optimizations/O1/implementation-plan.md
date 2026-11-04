# F5-O1 实施步骤

> 设计和测试方案已于 2026-11-02 分别确认，进入实施。使用 writing-plans、test-driven-development 和 subagent-driven-development；本项目不依赖 Git 分支/提交，不另建 worktree。子任务自检不替代最终原生 Claude Code 审查。

目标：普通/结构化/流式请求可靠落库，现有 SSE 先存后发，数据库故障和重启可收口；不实现 O2/F6 接口。

## Chunk 1：数据库基础

- [x] 核对 Docker、依赖和已有配置；Docker daemon 未启动，已启动 Docker Desktop。
- [x] 按用户最新授权切回两条 Mock；保留原 Provider Key。
- [x] 建立源码/测试/配置快照和历史/凭据保护摘要；补齐经摘要验证的原依赖文件。
- [x] 保存切回 Mock 后的稳定回归基线；不把真实配置期间的失败当成功基线。
- [x] 在临时目录建立独立 PostgreSQL 测试项目、密码和端口；不占开发库。
- [x] 先验证带 database 的有效配置被旧加载器拒绝、空测试库缺表的正确 Red。
- [x] 实现 persistence/database.py、tables.py、store.py、Alembic 迁移及数据库配置加载；核对 C01–C11、F03–F06、F09。
- [x] 独立检查本块契约和代码质量，修复后再接主链。

存储接口供主链接入：prepare(trace, snapshot)、begin_attempt(trace, attempt)、append_event(trace, name, payload)、finish(trace, attempt, error_code, event)、update_diagnostics(trace)、recover()/reconcile(request_id)。所有数据库操作独立 Session/短事务，可接收更短 deadline；终态返回数据库实际状态，提交未知单独分类。

## Chunk 2：主链接入与故障

- [x] 写 HTTP→PG 缺少持久化记录的 Red；在独立连接读库，不以原内存 Trace 代替。
- [x] main.py 组装数据库、校验版本、启动恢复；gateway.py 接普通/结构化快照与 Attempt/终态，probe 保持原行为。
- [x] Middleware/异常处理保存入口拒绝最小记录；记录失败不调用 Provider，不保存 raw body。
- [x] streaming.py 接业务事件提交和完成提交标志；提交后的发送故障不覆盖成功；完成前断连仍失败。
- [x] 按 H01–H09、S01–S09、F01–F10 做 Red→Green；明确提交未知不发相反终态。
- [x] 实现仅针对已停止执行标识的运行核对，关闭恢复任务/连接有界；不误伤活跃请求。
- [x] 独立检查本块契约和代码质量，不擅自扩大到续传/取消。

## Chunk 3：验收闭环

- [x] P01–P04：真实 Gateway 子进程/TCP/强制退出和同库重启；临时数据库资源严格归属隔离。
- [x] 默认全量、协议、Python 3.12 关键回归；检查实际收集项，保留原功能意图及明确变更对照。
- [x] 显式执行 L01–L03 的 6 次有成本真实调用，数据库仍为测试库；记录原始安全回执。
- [x] 产出代码导读、证据/逐组映射，回看模板，更新 README/AGENTS/进度/架构/契约；不改原历史验收。
- [x] 冻结源码与证据，用原生 Claude Code 按实际提示词进行三维完整审查（最多三轮）；修复先复现再复测。
- [x] 验证最终源码与审查版本、测试证据一致，资源及保护文件无误，按真实结果报告结论。

证据：临时工作目录由 /tmp/llm-gateway-f5o1-work-path 指向；关键命令通过 /tmp/f5o1-run.py 记录原始输出。最终必要证据归档到本项目录，不复制密钥。
