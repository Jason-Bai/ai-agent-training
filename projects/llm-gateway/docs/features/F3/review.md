# F3 Claude Code 实现独立审查

第1轮历史：原生Claude Code CLI，仅Read，沿用既有连接配置。首轮当时真实调用未执行；2026-11-01第2轮已补审真实证据并APPROVED，最终原文见本文末尾。原文的读取范围自述与P3位置均按实际足迹另行核实，修正及处置见test-plan.md 3.7，不改写工具原文。

全部指定文件与证据核对完毕。以下为 F3 实现第 1 轮只读审查最终报告。

---

# F3 实现第 1 轮独立代码审查报告（只读，原生 Claude Code）

**审查方式**：仅使用 Read 逐文件实际读取源码、测试、设计与证据；未修改任何文件、未执行 Shell、未联网、未启动额外代理；未重跑任何测试（测试由执行者运行，本报告只做静态审查与回执核对）。stitch MCP 连接失败与本审查无关（未使用且不需要）。

## 一、P0–P2 发现

**P0（密钥泄漏/越权/严重数据破坏）：0 项**

**P1（主链/关键安全边界失效）：0 项**

**P2（已确认用例或契约不满足）：0 项**

关键核对结论（依据逐文件阅读）：

| 核对项 | 结论 |
| --- | --- |
| 执行顺序 | `services/gateway.py` `_complete` 与设计 3.4 固定顺序一致（结构→模板/变量→指纹→模型→预算→超时→计数/窗口→尝试→Provider→结果验收），预算解析/超时钳制/计数失败不旁路预检，均有对应测试 |
| 总超时 | `asyncio.timeout` 包裹 Provider 调用 + 结果验收；`TimeoutError→504 provider_timeout`；`CancelledError(BaseException)` 透传，保护 F1/F2 中断语义；探测固定 10 秒真实等待（`test_f3_gateway_probe.py` 挂起替身 9≤elapsed<14） |
| 无隐藏重试 | httpx2 `retries=0`、AsyncOpenAI `max_retries=0`、协议替身按服务器收件次数断言（拒绝用例检查 0 次而不只看 attempts） |
| 密钥保护 | SecretStr 仅显式读取独立凭据文件、无环境变量回退、Mock 引用集合为空时早返回不读文件；错误消息/`str`/`repr`/`__dict__` 均不回显（多组 PRIVATE 断言测试）；`load_credentials` 与 `close` 失败包装均不回显原文；离线测试 autouse 阻止 `credentials.yaml` 打开与非回环 socket 连接 |
| 计数资源 | 固定 revision + manifest 三文件 SHA-256 启动核验，失败即拒绝启动（无字符回退）；独立黄金 236/358/515/17 与本地计数一致（goldens 测试 + pinned-resources-verification 回执） |
| 旧键迁移 | `LLM_GATEWAY_PROVIDER` 在进程环境或 .env 文件任一出现即启动失败并给出迁移提示，不静默回退（settings.py casefold 检查 + smoke 子进程验证） |
| 结果验收 | 响应结构验证先于完成分类（length+坏 usage → `provider_invalid_response`）；Usage 严格（缺失分量/布尔/字符串/负数/求和不等 → 拒绝，缺整体/None → 未知）；成功 data 精确 `{text, usage}`（model/actual_model/attempts 删除为 F3 刻意契约变更） |
| 错误映射 | 401/402/429/400/404/422/5xx/finish_reason 分类与设计 3.6 表格一致，`PROVIDER_MESSAGES` 与网关异常处理逐项对应 |
| 探测 | 仅 `/v1/models/{model}/health` 命中建 Trace（middleware 正则全匹配）；固定 basic_text@1.0.0、16 tokens/10 秒、任意非空请求体 422 且先于 Prompt 校验；成功 data 仅 `latency_ms`；`GET /healthz` 零 Provider 调用 |
| 生命周期 | lifespan 注入/加载互斥（不允许两种注入并存）；owned 客户端工厂失败逐一关闭；注入对象不被关闭；import main 无副作用（子进程验证） |
| 迁移审计 | evidence.json `migration_audit` 已核实：`original_test_functions_retained: 85`，`changed_functions` 清单逐条计数 = 28（10+1+8+1+5+3），`unchanged_historical_files: 11`。逐项审查 28 个迁移：均为契约刻意变化所需（Settings 无 provider、ModelConfig 注入、data 瘦身、合法预算字段从 extra-fields 参数化排除），指纹黄金值（`GOLDEN_HASHES`）迁移后仍断言同一哈希，未发现削弱验证的迁移 |
| TDD 回执真实性 | Red 均为正确断言失败：red-http（data 多余字段 + probe 404）、red-token-goldens（`0 == 236`）、red-contracts（`extra_forbidden` ValidationError）、config-red（79 failed DID NOT RAISE）、red-provider-results（24 failed，含 PRIVATE_SECRET 未净化真实失败）；exit=4 错误文件名回执原样保留并附 corrected 237 passed；早期未捕获退出码在记录中明确为 null，未冒充通过 |
| 最终回执 | evidence.json `final-offline-all-corrected`：exit 0，`570 passed, 4 skipped in 45.99s`，4 skipped 仅为 L01–L04；`live-missing-credentials-gate`：子 pytest `--run-live` 缺凭据 → fixture `pytest.fail("真实接入前置条件未满足…")`，1 error、无跳过、无网络，外层注明"Expected live prerequisite failure observed"，未将该负例冒充 live 通过 |

## 二、P3（轻微，不阻断，记录处置建议）

1. **src/main.py:31-32** — 注入注册表中 `model_id` 与计数资源不匹配时抛裸 `ValueError`（消息含"计数资源"）而非 `ConfigurationError`。启动期 fail-fast、Provider 未构造，行为正确；`tests/unit/test_f3_app_resources.py:15` 明确断言 ValueError，属有意实现。**处置**：保持现状，或在后续统一启动错误类型时再归类。
2. **src/services/models.py（read_yaml/UniqueKeyLoader 错误包装）** — 重复键 YAML 错误被折叠为通用安全消息，丢失具体键名/行号定位信息。安全优先（不回显）符合契约，`test_yaml_invalid_or_duplicate_is_safe` 只断言不回显。**处置**：可选在安全消息中附加去敏后的位置信息；非本轮要求。
3. **src/api/errors.py（ProviderCallError 处理器）** — `PROVIDER_MESSAGES[exc.code]` 直接下标，若出现未映射 code 将 KeyError→500 而非预期的 502。当前所有入口 code 均来自固定映射常量，不可达。**处置**：可选 `.get` 兜底为 `provider_error`；非本轮要求，不扩范围。
4. **docs/features/F3/evidence.json** — provider_agent_runs 部分早期条目 `"note": ""` 为空，与执行索引中"早期未捕获退出码明确为 null"的诚实标注对应但正文未落全。记录已原样保留、未冒充通过。**处置**：归档时把 note 补齐即可，非代码问题。

以上 P3 均不影响验收判定，处置结论建议记录在 test-plan。

## 三、读取范围

**已完整 Read**（含行级核对）：

- 项目约定：`AGENTS.md`、`docs/AGENTS.md`、`docs/references/claude-code-review.md`
- 设计与证据：`docs/features/F3/design.md`（全文）、`docs/features/F3/test-plan.md`（§3.3/§3.5/§3.6 及 §3.4 历史标注）、`docs/features/F3/evidence.json`（652 行中约 580 行逐字读取，覆盖全部 runs 条目边界、exit code、最终回执与 migration_audit；仅 provider `red-protocol` 失败输出正文约 35 行（513–547）未逐字读全，两端 phase/exit 与 Green 复测已核）、临时 `f3-source-tests.diff`（完整 2736 行）、临时 `review-evidence-index.md`
- 跨功能文档：`README.md`、`docs/contracts.md`、`docs/architecture.md`、`docs/progress.md`
- 配置/资源：`pyproject.toml`、`config/gateway.yaml`（默认 Mock）、`config/credentials.example.yaml`、`.env.example`、`.gitignore`、`assets/tokenizers/deepseek-v4.1-flash/manifest.json`
- 源码（全部实质性文件）：`src/main.py`、`src/contracts.py`、`src/settings.py`、`src/errors.py`、`src/api/routes.py`、`src/api/errors.py`、`src/middleware/request_context.py`、`src/services/gateway.py`、`src/services/models.py`、`src/services/credentials.py`、`src/services/token_budget.py`、`src/services/prompts.py`、`src/services/trace.py`、`src/providers/base.py`、`src/providers/mock.py`、`src/providers/deepseek.py`、`src/providers/openai_compatible.py`
- 测试（27 文件）：`tests/conftest.py`、`tests/f3_cases.py`、`tests/prompt_cases.py`、`tests/fixtures/f3_token_goldens.json`；unit 12 个（test_core / test_f3_app_resources / test_f3_config / test_f3_config_boundaries / test_f3_contracts / test_f3_provider_results / test_f3_result_boundaries / test_f3_token_budget / test_prompts / test_request_context / test_settings / test_app_factory）；integration 4 个（test_f3_gateway / test_http / test_f2_prompts / test_trace_integrity）；protocol 3 个（test_providers / test_f3_gateway_probe / test_f3_prompt_wire）；smoke 2 个（conftest / test_http_server）；live 1 个（test_real_providers）

**按禁令未读取**：实际 `.env`、`config/credentials.yaml`、任何全局认证设置/缓存/依赖环境、`tokenizer.json` 全文、`uv.lock` 全文、已下载的 `reference_encoding.py`。计数来源仅从 manifest、源码固定摘要与黄金测试审查。

**如实标注**：evidence.json 中约 35 行 provider red-protocol 失败输出正文未逐字读取（已在上面注明）；其余指定文件全部实际读取。

## 四、未验证项

1. **L01–L04 四条真实 Provider 调用未执行**——本地凭据未就绪（用户已明确知悉）。live 缺凭据门禁负例已验证（前置失败、无网络），但**不等于真实调用通过**；真实接入的证据只能在凭据就绪后以 `uv run python -m pytest tests/live --run-live -q` 显式产生。
2. **测试由执行者运行**：本审查未重跑任何测试；570 passed/4 skipped/exit 0 及全部 Red/Green 阶段结论均为回执核对结果。
3. 真实 Provider 行为（实际网络、实际 Usage 差异、真实响应形态）与生产环境验证不属本轮范围。

## 五、CODE_VERDICT

```
CODE_VERDICT: APPROVED_OFFLINE_PENDING_LIVE
```

源码与已执行的离线证据**未发现 P0–P2 阻断项**：契约实现、密钥与边界保护、TDD 回执真实性、85/28/11 迁移审计均与已确认设计一致；4 项 P3 不阻断且均有明确处置建议。

**F3 整项验收仍不能通过**：真实接入必验项（L01–L04）因本地凭据未提供而未执行，凭据等待状态是已明确记录的前置条件、不构成代码缺陷，本判定也不得被引用为 live 已通过。凭据就绪并完成真实调用验证、R03 文档同步核对收尾及本轮 P3 处置记录后，F3 方可进入验收结论。

---

# 第2轮：真实证据补审（2026-11-01）

以下为工具原文。实际18次Read均成功、CLI退出0、is_error=false、end_turn；源码摘要未变。原文将live uvicorn称为独立进程，执行者核实：live是pytest进程内uvicorn.Server配合真实回环HTTP，独立单worker进程证据来自离线smoke；补审未重新运行测试或重算摘要。具体处置见test-plan.md 3.9。

已完成全部 18 个受控文件的逐一读取（stitch MCP 连接超时与本审查无关，未使用且不需要，与第 1 轮口径一致）。以下是 F3 第 2 轮独立审查最终报告。

---

# F3 实现第 2 轮独立代码审查报告（只读，真实证据补审）

**审查方式**：仅使用 Read 逐文件读取清单内文档、源码、测试与回执；未修改任何文件、未执行 Shell、未联网、未启动额外代理、未读取实际 `.env`/`config/credentials.yaml`/任何认证设置或环境缓存。**未重跑任何测试**：570 离线与 4 条真实用例的结论均来自执行者回执（`real-acceptance-round2-receipt.json` 与 JUnit 原始 `live-results.xml`）的交叉核对，本报告只做静态审查与元信息一致性复核。

## 一、P0–P2 发现

**P0（密钥泄漏/越权/严重数据破坏）：0 项**

**P1（主链/关键安全边界失效）：0 项**

**P2（已确认用例或契约不满足）：0 项**

关键核对结论（依据逐文件阅读）：

| 核对项 | 结论 |
| --- | --- |
| 四例是实际 HTTP + 真实双适配器 | `tests/live/test_real_providers.py:32-58` live_server 用真实 uvicorn 单 worker 进程 + 回环 `httpx2.AsyncClient` 进入路由（非 TestClient）；`real_registry()` 绑定 `deepseek-main`(adapter=deepseek)/`openai-main`(adapter=openai_compatible)，均指向 `https://api.deepseek.com`；无 Provider 注入时 `src/main.py:41` 走 `load_credentials` 构造真实客户端。四例 = 主/备 × 普通/探测，各 1 次尝试 |
| 成功/Trace/stop/Usage/生效预算断言 | live 测试第 81–105 行逐一断言：200+四字段信封、`trace.status=completed`、attempts==1、`finish_reason=="stop"`、`actual_model` 非空、`usage is not None`、adapter/operation 身份、`counter_version`/指纹/输入计数>0；L01 生效预算 (64,30)、L02 默认 (1024,30)、两探测 (16,10)（对应 `src/services/gateway.py:43-50` 默认值解析与 `probe` 固定 16/10） |
| 输入计数差异断言 | 第 95–96 行 `delta = actual_input − local_count`，方向与 2.6 一致，仅当实际大于本地时以 `safety_margin_tokens`(1024) 上界约束，不为负差设限——与设计 2.6 完全一致 |
| 四例无隐藏重试/fallback | 每例 attempts==1（Trace 级）+ `generation_attempts: 4` 回执声明；`src/providers/deepseek.py:15` retries=0/trust_env=False/follow_redirects=False、`src/providers/openai_compatible.py:14` max_retries=0；Gateway 无 fallback 编排 |
| 离线隔离（凭据就绪后重验） | 回执第二段：`uv run python -m pytest -q --tb=short` → **570 passed, 4 skipped in 46.82s, exit 0**；4 skip 仅为默认模式下的 L01–L04。`tests/conftest.py:74-113` autouse 隔离在凭据文件**已存在**时仍拦截其读取（Path.open 断言）与非回环连接——570 通过即证明无任何离线用例触及真实凭据或外部网络 |
| 原始 XML 与回执一致 | 逐字段比对 `live-results.xml` 与回执 `real_provider_acceptance.cases`：4 组 request_id/provider/adapter/actual_model/counter_version/local/input_delta/input/output/total/latency_ms 全部一致；suite `time="4.433"` 与 "4 passed in 4.43s" 吻合；errors=0 failures=0 skipped=0；test-plan 3.8 表（两位小数舍入）与两处一致 |
| Usage 合法性交叉印证 | 四例 total=input+output 精确成立（358+13=371、515+13=528、237+1=238、237+1=238），与 `src/providers/base.py:40-48` 严格 Usage 归一及总数校验路径吻合 |
| 黄金对齐佐证 | L01 local=358、L02 local=515 恰为设计 3.3 独立黄金"context 1.0/context 1.1"的固定值；两探测 local=237 为探测固定消息的自有计数（非黄金 basic 的 236，内容不同，无矛盾）。四例 `counter_version` 均为固定 revision `314113f8…04d6` |
| 差值 0 未夸大成普遍一致性 | design 3.3"未证明全部输入一致"、test-plan 3.8"**四次差值为0仅证明本次输入与当前云端一致**"、2.6 L 组"四例成功也不推出所有输入…一致"、progress"通用一致性与极限能力未实测"、回执 scope 行"not proof of universal token-count alignment or production reliability"——口径一致且保守 |
| 首轮后源码/测试/默认配置不变 | 回执中 `source_hashes_first_review_after` 与 `source_hashes_now` 共 52 项 SHA-256 逐键相同（`source_unchanged: true`）；`no_source_or_tests_changed`、`no_user_runtime_configuration_modified` 为 true；与 test-plan 3.8"源码/测试/默认配置未变"及 live fixture `Settings(env="test", _env_file=None)` 不读用户 `.env` 相符 |
| F1/F2 历史工件不改 | 回执 `historical_11_hashes_verified` 覆盖 F1/F1-O1/F2 共 11 份历史工件；文档核验回执"11 historical hashes unchanged; default 2 Mock mappings; credential ignored"——凭据文件被核验脚本显式忽略，不读不录 |
| 无提前宣称验收 | AGENTS.md、README、contracts.md、progress.md（F3=待独立审查）、test-plan 3.3 表均标注"待第2轮真实证据补审/暂不称验收"，无文档审查冒充实现审查 |
| 首轮 P3 处置复核 | 四项处置均合理（见下节），与当前源码一致：main.py:31-32 ValueError 启动即拒、Provider 构造前失败（`create_app` 内、lifespan 前）属实；无回显安全文案符合契约；未知 code 按"内部编程错误→internal_error"与设计 3.6 兜底行一致，从 `base.py` 固定映射集不可达；空 note 解释属如实报告范畴 |
| 设计门槛补齐 | 设计 §4 要求"两条真实路径各一次普通调用和一次主动探测 + 合法 Usage/计数差异/资源版本 + 离线分层 + 无 P0–P2 独立审查"——L01–L04 四例通过、离线 570 通过、第 1 轮 P0–P2=0、本轮补审无阻断，门槛全部满足 |

## 二、P3（不阻断，记录处置建议）

**首轮 4 项 P3 处置复核：全部合理，维持原判。**

1. `src/main.py:31-32` 计数资源/模型失配裸 `ValueError`——实测确认发生在客户端构造之前、启动即拒，保留合理。
2. YAML 错误折叠丢失定位——"不回显未知键/原文"优先，符合设计 3.1 安全边界，保留合理。
3. ProviderCallError 未知 code 直接下标——处置修正了位置并论证"内部编程错误应显式暴露为 internal_error，不加静默兜底"，与设计 3.6"Gateway 自身未预期错误→500/internal_error"一致，合理。
4. evidence.json 早期空 note——保留原始回执并补核实解释、不伪造退出码，符合项目"不把未运行写成通过"，合理。

**本轮新增 2 项 P3（均为文档一致性，不影响验收）：**

1. **docs/features/F3/test-plan.md §3.6 F3-R03 行**：引用首轮文档核验"9文档/85本地链接"，而 2026-11-01 最新核验回执为 86 个本地链接（首轮后文档新增了指向 3.8 等章节的链接）。两值对应不同时点，不构成缺陷。**处置**：在 3.8 或 R03 行注明最新核验值为 86，避免读者误判链接失效。
2. **docs/features/F3/design.md 头部"状态：开发中"与 test-plan.md 头部"状态：执行中"**：实现与离线/真实验证均已完成、当前等待本审查结论，按 docs/AGENTS.md 状态词表应为"待独立审查"（progress.md 已正确标注）。**处置**：本报告返回后更新验收状态时，顺手把两处头部状态改为与 progress 一致。

## 三、本次实际读取范围

逐文件完整 Read 共 18 个：项目约定 `AGENTS.md`、`docs/AGENTS.md`、`docs/references/claude-code-review.md`；F3 受控文档 `docs/features/F3/design.md`、`test-plan.md`、`review.md`（第 1 轮原文）、`live-results.xml`（仅元信息，无正文/凭据）；跨功能 `README.md`、`docs/contracts.md`、`docs/progress.md`；源码 `src/services/gateway.py`、`src/providers/deepseek.py`、`src/providers/openai_compatible.py`、`src/providers/base.py`、`src/main.py`；测试 `tests/live/test_real_providers.py`、`tests/conftest.py`；临时回执 `/var/folders/…/llm-gateway-f3-uv1kfqqu/real-acceptance-round2-receipt.json`。

**按禁令未读取**：实际 `.env`、`config/credentials.yaml`、任何认证设置/环境缓存、`evidence.json` 全文、其余首轮已审且哈希未变的源码/测试。完整代码结论基于第 1 轮审查 + 52 份相同摘要 + 本轮上述调用路径重读。

## 四、未验证项与极限窗口（如实标注）

1. **测试由执行者运行，本审查未重跑**：570 passed/4 skipped 与 4 条真实通过的结论均为回执与 JUnit 原始文件核对结果；SHA-256 等哈希值因禁止执行 Shell 未能独立重算，其"首轮后一致"依据回执内两映射逐键相同及文档核验脚本输出。
2. **真实凭据文件内容未读取**（按禁令），真实请求的账户、网络、模型组合可用性证据仅覆盖 2026-11-01 当次四例。
3. **极限窗口（1M 上下文）、生产可靠性、所有输入计数一致**均未验证且不在 F3 承诺内——docs 已按此口径标注，未夸大。
4. 真实响应正文与 Prompt 内容未记录、未读取，符合"只含元信息"边界。

## 五、CODE_VERDICT

```
CODE_VERDICT: APPROVED
```

新增真实证据（四条实际 HTTP + 双真实适配器各普通+探测，成功/Trace/stop/Usage/生效预算/输入计数差异断言齐全，四例差值 0 且如实限定范围）与设计 §4 验收边界完全对应；凭据就绪后的默认 570 离线回归证明隔离依旧有效；原始 XML 与回执元信息逐字段一致；52 份摘要首轮后未变、11 份 F1/F2 历史工件及用户配置未改；首轮 4 项 P3 处置合理，本轮新增 2 项 P3 不阻断。F3 第 2 轮（整项第 2/3 轮）无 P0–P2。

执行者可在本报告返回后按项目约定更新验收状态（docs/progress、AGENTS.md、F3 两文档头部及 R03 链接数注记），本轮仅审查、未做任何修改。
