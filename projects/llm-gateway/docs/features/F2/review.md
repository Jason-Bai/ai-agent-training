# F2 Claude Code 独立代码审查取证

2026-10-31，第 1 轮；执行者未指定模型或修改认证/配置。实际使用原生 Claude Code CLI，仅开放 Read，不经 acpx 或替代审查 Agent。

```sh
CLAUDE_CODE_INLINE_TOOLS=false claude --print --tools Read --allowedTools Read --no-session-persistence --output-format stream-json --verbose < review-prompt.txt > review-round1.jsonl
```

进程退出码 0；最终结果 subtype=success、is_error=false、stop_reason=end_turn；VERDICT: APPROVED。实际 38 次 Read 均返回且无工具错误，其中覆盖全部 26 份源码/测试；另读取设计、测试方案、diff、证据、跨功能文档与依赖。完整文件哈希与读取路径保存在 [evidence.json](evidence.json) 的 reviews 中，收口前逐项比对源码/测试/依赖均未变化。

审查仅做静态只读核对，没有代执行测试。测试实际回执由执行者保存在 evidence.json；最终全量 223 passed in 1.53s。审查原文中的 P3-1 版本推断已被本机 Python 3.12.13 最小实测反证；P3-2 是既有依赖观察，保留范围外。处置与验收见 [测试方案 3.7](test-plan.md#37-独立审查)。以下原始报告完整保留，不把审查推断改写成执行者事实。

---

所有待读文件已核对完毕（diff、设计、测试方案、证据、全部源码与测试、文档与锁文件）。以下是第 1 轮独立只读审查报告。

---

# F2 第 1 轮独立只读代码审查报告

审查方式：仅 Read，未修改文件、未执行命令、未读取实际 `.env`/凭据。以下关于测试执行结果（223 通过、Red/Green 回执、受保护文件哈希、原 assert AST 保留）均来自执行者 evidence.json 与文档证据，我未独立运行测试，特此声明。

## 一、重点核对结论（均通过）

| 审核重点 | 结论与依据 |
| --- | --- |
| 类型/模板初始化校验 | `_validate_template`（`services/prompts.py:63-85`）覆盖：五个属性非空白字符串、版本三段数字正则、变量名 `[A-Za-z_][A-Za-z0-9_]*` 且禁用 `message`、`max_chars` 为正 `int`（`type() is not int` 排除 bool）、占位符集合恰好等于 `{message}∪变量键`（`is_valid()` 先于 `get_identifiers()` 短路，`${}`/属性/索引/格式/悬空 `$` 均拒绝）、Few-shot 非空白字符串对。重复键在覆盖前拒绝（`prompts.py:96-98`）。注册发生在 `create_app` 内、Provider 工厂之前（`main.py:18-19`），有工厂 spy 测试证明。 |
| 只读深层字段 | `PromptTemplate`/`VariableSpec`/`FewShot`/`PromptRegistry` 均 frozen；`__post_init__` 将 `variable_specs` 转 `MappingProxyType`、`few_shots` 转 tuple，外部容器修改不影响资产（`test_prompts.py:120-140` 逐层断言）。 |
| 变量预算与顺序 | 调用链完全符合设计 3.4 固定顺序：结构 422（Pydantic strict）→ 模板查找 → 保留键 `message` → 缺变量 → 多变量 → 空白/单变量预算 → 渲染总长 → 指纹 → 模型检查 → 尝试/调用（`gateway.py:14-24`）。组合错误 5 例（`test_error_precedence`）均按最先失败阶段断言。 |
| 临界值 | context 10,000/10,001、最终 User 20,000/20,001 两侧均测（ASCII+中文/码点）；模板固定文字 73 字符与测试中 `total - 74` 偏移及 test-plan 2.1 的 73 声明三者吻合；原 UserMessage 上限 20,000 未变，`test_valid_raw_user_can_exceed_rendered_budget` 正确区分原始字段预算与渲染总长；无截断（逐字符消息相等断言）。 |
| 消息黄金与 SHA | `tests/prompt_cases.py` 资产为设计 3.3 正文的独立副本（我逐字符比对 design/prompts.py/prompt_cases 三处正文一致），黄金哈希为固定常量，与 test-plan 2.1 表三值逐一相同；`expected_context_messages` 独立重述装配结果，非从实现复制自比。编码规则（`ensure_ascii=False, sort_keys, separators`、UTF-8、SHA-256 小写 64 位）与设计 3.5 一致。 |
| 原 API 兼容 | `Message` 增 assistant 仅用于内部/Provider；入参仍走 `UserMessage(Literal["user"])`（`contracts.py:21-23`），外部 system/assistant 仍 422（原 `test_http.py:70` 用例保留）。成功体仍为精确五字段 data + 四字段信封。原 46 个测试函数、装饰器在 diff 中零删除（smoke 仅追加、app_factory 仅增探针），原 assert 逐条仍成立（我逐个重核了 test_http/test_core/test_settings/test_request_context/test_trace_integrity 的语义）。 |
| 应用/并发隔离 | 注册表每应用一次、请求不重建（`__init__` 计数 monkeypatch 验证）；注入注册表应用本地互不污染；导入无注册器副作用（子进程探针）。并发测试用 Event 强制反序进入且两种进入顺序下 `calls[0]` 恒为 second，按内容+request_id 关联，确定性成立；evidence 中 `pre-review-all` 偶发失败与 `red-test-correlation` 稳定复现、修正后通过的回执链完整，未隐藏失败。 |
| 错误前后指纹生命周期 | 422/未知模板/缺变量/多余/空白/预算/总长/渲染异常/哈希异常 → null 指纹、零调用零尝试；unknown_model → 已生成指纹零调用；Provider 受控/内部故障 → 保留指纹+1 失败尝试；CancelledError（BaseException，不被 `except Exception` 吞掉）→ running 未确认、指纹保留，与设计 3.5 及 F1 中断边界一致。 |
| 文档一致性 | README/contracts/architecture/progress/AGENTS/docs-AGENTS/test-plan/模板回填的状态表述全部为“已实现、本地 223 通过、待独立审查”，F3 范围明确分离（仅字符预算、无 Token/真实 Provider 声明）；无范围外占位、新依赖（仅 stdlib `string.Template`/`hashlib`/`json`）、管理接口或配置项。 |

## 二、38 组映射核对

F2-01 至 F2-38 逐条映射到实际存在的测试函数与参数化变体（我按测试文件逐一核对函数名与参数展开）：全部有实质断言（消息逐字相等、调用/尝试计数、指纹黄金、私有关键词不回显），不存在“仅状态码”式覆盖；单元黄金（F2-24/25/26/27/28/33/34/35）与 HTTP 黄金（F2-01/02/03/…）分层互补，指纹黄金为独立常量非自比。数量核算吻合：92 unit + 128 integration + 3 smoke = 223，原 46 函数 + 新 108 展开。

## 三、发现问题

### P0：无
### P1：无
### P2：无

### P3-1（本次引入，测试可移植性，非阻断）
`tests/unit/test_prompts.py:51` 与 `:71`：`assert template.variable_specs == {...}` 对 `types.MappingProxyType` 与 dict 使用 `==`。CPython 3.13 起 mappingproxy 才实现与映射的结构相等比较；3.12 及更早无 `tp_richcompare`（且 `dict.__eq__` 对非 dict 返回 NotImplemented），该比较恒为 False，两条断言在 3.12 解释器下必失败。而 `pyproject.toml:5` 与 `uv.lock:3` 声明 `requires-python = ">=3.12"`。证据回执全绿说明当前实际解释器为 3.13+，产品代码不做 mappingproxy 相等比较，不受影响。
建议处置：改为 `dict(template.variable_specs) == {...}`（最小改动），或核实本机解释器版本后将 `requires-python` 收紧为 `>=3.13` 并记录。若项目明确承诺支持 3.12 运行测试套件，此项应升级为 P2；我无法执行命令验证版本边界（依据为对 CPython 变更的认知），建议执行者用一条 `python -c "from types import MappingProxyType; print(MappingProxyType({'a':1})=={'a':1})"` 核实。

### P3-2（原有、范围外，仅记录）
`pyproject.toml:18` 存在 `"httpx2>=2.16.0"` 依赖，本次 diff 未改动（F1-O1 遗留），与 F2 无依赖关系；建议后续维护依赖时核实该包名是否为笔误，本次不作处理。

## 四、明确排除为阻断项的范围

真实模型语义、抗注入效果、F3 Provider/Token 精确预算、管理平台能力，均未作为审查发现，符合用户约定。

## 五、说明

- “原 115 场景保留/受保护文件哈希不变/测试真实通过”为执行者证据（evidence.json 回执链完整且与代码、文档相互印证），我未独立运行。
- 轻微观察（不构成发现）：`GatewayService`/`build_messages` 的 registry=None 兜底会构造默认注册表（`gateway.py:12`、`prompts.py:120`），HTTP 路径始终注入应用注册表且有计数测试保护，无行为影响。

**VERDICT: APPROVED**（P0=0，P1=0，P2=0，P3=2 项已记录处置建议；其中 P3-1 建议在核实解释器版本后按上面两种方式之一闭环，不影响本项验收）。
