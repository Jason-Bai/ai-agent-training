# F1：Claude Code 原始独立审查报告

> 历史快照说明：本报告保留首次 F1 审查原文。2026-10-31 用户确认源码直接放到 `src/`，报告中的旧目录路径对应迁移前快照；迁移后的审查与验证见 [测试方案](test-plan.md#2026-10-31-目录重构验证)。

> 完成状态：正常退出 0，is_error=false，stop_reason=end_turn。直接调用本机 Claude Code，沿用既有模型/连接配置，进程级 CLAUDE_CODE_INLINE_TOOLS=false，工具限 Read。
> 实际读取 19 个文件，涵盖全部 16 个源码/测试文件及设计、测试方案和 pyproject.toml；源文件审查前后 SHA-256 均一致。
> 源码/测试摘要指纹：`09ae4d4777b3b915194f7224fe78e5db5fca1d6c4a8a1a2c5ea4dcbebd98c00e`。这是静态审查；以下为工具原文，建议的核实和处理见 test-plan.md，不把所有建议自动当作已确认缺陷。

（接上，继续完成审查报告）

**HTTP 信封与 request_id**（续）
- 404/405 同样带 `X-Request-ID` 且无模型调用 Trace（`main.py:26-27` 仅对 `POST` + 精确路径 `/v1/llm` 建立 trace；`redirect_slashes=False` 使 `/v1/llm/` 为 404）。调用方自带 `X-Request-ID` 被覆盖忽略（`main.py:34-35`）。符合契约。

**校验与资源错误**
- `contracts.py:6-35`：`extra="forbid", strict=True` 覆盖顶层、`prompt`、消息三层；`model=123` 等类型错误因 strict 模式拒绝、不自动转换；`content` 为 1–20,000 字符（Pydantic 按码点计数，测试覆盖中文）+ 去空白非空校验（`contracts.py:24-29`），原文不裁剪（validator 返回原值）。
- 结构不合法统一 `422/invalid_request`（`api/errors.py:19-21`，固定消息"请求参数不合法"，丢弃 exc 详情）；结构合法后由 `gateway.py:17-19` 返回 `400/unknown_model` 或 `unknown_prompt_template`，均发生在 `start_attempt`（`gateway.py:20`）之前，Provider 调用 0 次、尝试 0 条。符合设计表的入口检查顺序。

**Provider 接缝**
- `providers/base.py:6-12` 的 `complete(model, messages) → text + optional usage` 与设计一致；`mock.py:13-17` 记录参数/次数、按配置抛出受控错误，不访问网络。`gateway.py:22-33` 只调用一次、无重试；`ProviderCallError` → 尝试记为 failed/provider_error 并抛给 502 处理器；调用中未预期异常（`except Exception`）→ 尝试记为 internal_error、不伪装成供应商故障；`CancelledError` 属 `BaseException`，不被捕获，直接传播。符合契约。

**Trace 准确性与隐私**
- 请求/尝试两级分离（`trace.py:9-27`）；请求级字段含可空模型/模板名（校验前失败为 null，`gateway.py:14-16` 只在验证后写入）；尝试级含序号、`provider=mock`、`actual_model=null`、状态与错误码，不含消息、回答、Usage、密钥或堆栈（测试 `test_trace_does_not_retain_prompt_or_output` 验证）。
- 收口只在 HTTP 边界一次：`main.py:49-52` 在完整响应体发出后（`response_completed`）以 `state["error_code"]` 收口；错误处理器只写 state（`api/errors.py:11`），Gateway 只记尝试，无双记账。`finish_request` 二次收口抛 `RuntimeError`（`trace.py:48-50`），单元测试覆盖。
- 中断：`CancelledError` 穿透 gateway 与中间件（均只捕 `Exception`），`finally` 因响应未完成而不收口 → 请求与尝试保持 running，未伪装成功。与"F6 前保持未确认"的契约及 F1-14 一致，验证正确。

**隔离与启动**
- 每个请求独立 scope/state、独立 trace、独立 `GatewayService`（`api/dependencies.py:6-7`）；共享的仅是注入的 Provider 实例与 TraceStore（设计如此）。交错请求测试证明消息不串扰、终态各写一次。冒烟测试以 `--fd` 单 worker 真实回环 HTTP 启动并清理子进程（`test_http_server.py:13-54`），`trust_env=False` 不触碰外部代理/服务。

## 发现

**P0：无。P1：无。P2：无。** 未发现违反已确认契约或影响验收的问题。

**P3（5 项，不阻断，给出处理建议）：**

1. `src/llm_gateway/main.py:43-48` — 若异常发生在 `http.response.start` 已发出之后（F1 的 JSON 单发路径不可达，未来 F5 流式才可能出现），中间件重新抛出，外层 Starlette ServerErrorMiddleware 将输出不带四字段信封的纯文本 500（trace 保持 running，至少不误记成功）。建议在 F5 设计时明确该窗口的处理。
2. `src/llm_gateway/api/errors.py:31-37` — 除 404/405 外的 `HTTPException` 状态码统一映射为通用 `http_error`。F1 只触发 404/405，无契约违背；后续新增路由使用其他 4xx 时应补具体码。
3. `src/llm_gateway/service/trace.py:30-35` — TraceStore 无上限、只增不删；与"首版本地单实例、F8 持久化"的范围一致，长时间运行时内存会持续增长。F8 迁移时处理即可。
4. `tests/smoke/test_http_server.py:25` — 回环客户端硬编码 `timeout=0.4`，慢机器上合法响应可能超时而偶发失败；不影响产品代码，建议放宽或改为按 deadline 计算。
5. `src/llm_gateway/service/gateway.py:17-19` — 模板解析先于模型检查；当模板与模型同时未注册时稳定返回 `unknown_prompt_template`。设计只分别定义两类 400，未规定优先级，当前行为确定但未文档化；建议在 F2 记录该优先级，避免后续调用方误判。

## 未验证项与限制

- 本审查为只读，未重跑测试；"70 passed" 结论来自已记录证据，且测试数量构成（5/64/1）、Red 证据与文件内容一致。
- 中断路径按契约保持 running（未确认），**不是成功**，取消收口归 F6。
- 真实 Provider、模型对 System Prompt 的实际遵守效果、F2–F8、多实例/对外开放均未验证；OpenAPI 文档页按设计豁免信封。
- 无请求体大小上限，仅适用"本机或隔离开发网络"的已确认边界，对外开放（1.7）前需评估。

---

**VERDICT: APPROVED** — P0: 0, P1: 0, P2: 0, P3: 5（均不阻断，处理建议如上）。已确认的 F1 契约在源码与测试层面全部落实，未发现需返工或返需求的问题。


---

## 2026-10-31 目录重构：Claude Code 原始报告

> 本次重构第 1 轮：直接调用本机 Claude Code，只允许 Read，正常退出 0，`is_error=false`、`stop_reason=end_turn`。全部 16 个源码/测试文件均在实际读取范围内，审查前后 SHA-256 一致。
> 源码/测试清单摘要指纹（按相对路径排序的 SHA-256 映射 JSON）：`66b33e1d8ac1ea3fcdeebbe66bdebba3e865d06f3a400f515d923e1428cbf97e`。以下保留原始结论，P3 核实与处置见 [测试方案](test-plan.md#2026-10-31-目录重构验证)。

全部核对完成。以下为独立审查报告。

---

# F1 目录重构独立审查报告

**审查方式**：只读。逐行读取变更 diff、全部 11 个源码文件、5 个测试文件及 12 份相关文档，并对旧路径做了 5 处存在性探测（`src/llm_gateway/`、`src/llm_gateway/api/`、`main.py`、`contracts.py`、`service/trace.py`）。**未运行任何测试**：70 passed（迁移前 0.56s / 迁移后 0.61s，含独立进程真实 HTTP 冒烟）及 README 启动参数验证均来自执行者证据（`docs/features/F1/test-plan.md:68-75`），本审查只核对其一致性、测试构成与代码现状是否吻合。

## 需求逐项核对

| 需求 | 结论 |
| --- | --- |
| 内容直接移入 `src/`，不保留目录或兼容层 | ✅ 5 处旧路径探测全部不存在；diff 无新增文件，无 `__init__.py` 重导出 |
| 源码/测试导入与 monkeypatch 去前缀 | ✅ 16 个文件逐行核对，仅 8 个源码文件改 import 行、5 个测试文件改 import/monkeypatch/uvicorn 字符串；`contracts.py`、`errors.py`、`trace.py` 本无内部导入，为纯移动（故不出现在内容 diff 中，与现状一致） |
| 同步 Uvicorn 入口 `main:app`、README、F1 设计、进度 | ✅ `README.md:38` 为 `uv run uvicorn --app-dir src main:app ... --workers 1`；`README.md:83-106` 目录树改 `src/` 根并加加载说明；`design.md:29` 改 `src/main.py` 且 `design.md:147-149` 新增重构小节；`progress.md:25` 决策 #3 与 `progress.md:46-48` 重构记录同步，链接锚点存在 |
| 业务行为、HTTP 契约、测试断言保持原样 | ✅ 全部 src 正文与 diff 对照仅 import 行变化；行为细节与原始报告描述一致（如 `gateway.py:17-19` 模板先于模型检查、`gateway.py` 只捕 `Exception` 不吞 `CancelledError`、`main.py:49-52` 收口条件，行号均未变）；70 测试无新增/删除用例、无断言修改，构成（5 unit / 64 integration / 1 smoke）与既有记录一致 |
| 不引入新功能、打包、路由或 Provider | ✅ `pyproject.toml` 零变更（无 build-system、无新依赖）；`routes.py` 仅 `POST /v1/llm`；`providers/` 仅 base/mock |
| 原始审查报告保留历史路径并加快照说明 | ✅ `review.md:3` 新增快照说明并链接 `test-plan.md#2026-10-31-目录重构验证`；正文旧路径（`review.md:35/36/37/39` 的 `src/llm_gateway/...`）原样保留 |

## 命名冲突与入口核对

- 顶层模块 `main`/`api`/`contracts`/`errors`/`providers`/`service` 与标准库及全部声明依赖（fastapi、pydantic、uvicorn、pytest、pytest-asyncio、httpx、httpx2）无同名冲突；`src/errors.py` 与 `src/api/errors.py` 分属不同包，无遮蔽。
- `python -m uvicorn` 走 `uvicorn/__main__.py`，其内部 `uvicorn.main` 是包内模块，不受顶层 `main` 影响；`--app-dir src` 与 pytest `pythonpath = ["src"]` 均把 `src` 优先插入 sys.path，解析确定。执行者已用独立子进程按 README 原命令（不提供 `PYTHONPATH`）验证回环 HTTP 返回 200、`MOCK_REPLY`、四字段与请求 ID（`test-plan.md:74`）。
- `tests/smoke/test_http_server.py:20-21` 的 `main:app` 与 `PYTHONPATH=root/src` 自洽；`tests/integration/test_http.py:9` 的 `from conftest import changed` 为既有写法，未受本次变更影响。

## 文档残留与链接检查

我读过的全部文档（README、AGENTS.md、docs/AGENTS.md、architecture、contracts、test-plan、progress、F1 design/test-plan/review、references/claude-code-review、BUG-001）中，除 `review.md` 刻意保留的历史路径外，无 `llm_gateway` 旧模块引用；新增链接（`review.md:3`、`progress.md:48` 指向的 `#2026-10-31-目录重构验证` 锚点）均存在且相对路径正确。

## 发现

**P0：无。P1：无。P2：无。** 未发现违反已确认需求的问题。

**P3（1 项，不阻断）：**

1. `docs/features/F1/design.md:133` — “README、架构、契约和进度同步：待实现后核对。”是 F1 验收前的遗留表述，与 F1 已验收、本次重构已同步文档的事实不符。该行不在本次 diff 内（变更前已存在），不违反本次需求（本次要求的同步已由 `design.md:147-149`、README、progress 完成）；建议顺手改为已同步的表述。

**说明（非发现）**：`test-plan.md:75` “Claude Code 独立审查：待执行”是流程中的正常状态，本审查完成后由执行者按工作流回填结论；提供的 diff 是稍早快照，工作区 `test-plan.md:73-75` 已写入迁移后证据，审查以工作区当前状态为准。

## 未验证项

本审查只读、未重跑测试；“70 passed in 0.61s（含真实 HTTP 冒烟）”与 README 启动参数验证的证据来自执行者记录，其表述一致性与代码现状已核对，未独立执行。

---

**VERDICT: APPROVED** — P0: 0, P1: 0, P2: 0, P3: 1（非本次变更引入的文档遗留表述，不影响验收，处理结论如上）。已确认的最小目录重构需求在源码、测试与文档层面全部落实，未发现需返工或返需求的问题。
