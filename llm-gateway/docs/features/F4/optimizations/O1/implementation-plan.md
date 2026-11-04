# F4-O1 实施步骤

> 执行者按项目工作流使用 subagent-driven-development 或 executing-plans 完成；最终独立代码审查必须使用原生 Claude Code，文档检查/子代理不替代。用户已确认设计与测试方案，可直接执行，无额外例行确认节点。

目标：提取 10 项平台常量、统一框架 HTTP 状态引用、原样迁移 JSON 工具，保持既有业务行为。沿用 FastAPI/Starlette、Pydantic、pytest、现有 JSON 工具和锁定依赖，不添加依赖或 Git 前提。

## 步骤

- [x] 1. 建立基线：保存 src/tests 的原始快照与当前 OpenAPI/消息/Schema 特征；对运行配置、固定计数资源、原 F1–F4 验收工件取摘要；`uv sync --locked` 后运行 `uv run python -m pytest -q`，保留原始命令/输出。读取现有测试，按测试方案只补尚缺的必要边界特征，在迁移前验证；无行为变化不制造 Red。
- [x] 2. 最小源码整理：新建 `src/constants.py`，明确原始/渲染字符和原始/展开 Schema 限制；消费位置为 contracts、prompts、validation、gateway。HTTP 裸状态统一替换为 `starlette.status as http_status`，避免 status 局部名遮蔽。固定探测保持 16/10，文案与 OpenAPI 字符串保持原样。
- [x] 3. JSON 迁移：原样移动 `src/json_codec.py` 到 `src/utils/json_codec.py`，新建空 `__init__.py`；调整 api/routes、validation、具体计数器、Responses 适配器和原工具测试的导入。删除旧文件，无兼容代理；比对移动前后文件摘要。
- [x] 4. 验证与导读：执行已确认测试方案中的关键集、完整离线、协议/子进程及 Python 3.12 关键路径；保存实际回执与逐组映射。静态核对常量值、消费者、HTTP 状态、有效导入、当前文档及历史摘要。同步 README、AGENTS、架构/当前链接与进度，产出本目录 implementation-summary.md；原历史验收不改写，历史导读仅在必要时修正失效的源码链接并注明迁移来源。
- [x] 5. 原生独审闭环：按三维模板归档 `review-prompt-r1.md`，列出用户确认来源、基线/差分、全部相关文件及实际证据。保持现有 CLI 配置与 Read 权限，保留读取覆盖、退出码/最终状态、原始报告及只读摘要证明。最多三轮完整审查；核实发现、最小复测，更新证据与结论后报告验收结果。

详细用例和命令以 [test-plan.md](test-plan.md) 为准；运行结果写入本项目录，不覆盖历史证据。
