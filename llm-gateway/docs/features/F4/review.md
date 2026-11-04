# F4 Claude Code 第1轮独立审查原文

> 原生CLI，退出码0，is_error=false，stop_reason=end_turn，耗时713.217秒。清单56/56文件与2份指定证据均读取，无读取错误；66份源码/测试/默认配置摘要前后一致。
>
> 下方原文完整保留。P3-1 的标准解读经执行者查证未采纳，P3-2记录为必要迁移；处置依据和实际复核见 [测试方案3.9](test-plan.md#39-claude-code-独立审查与处置)。不能把审查原文中的静态推断当成实际复现事实。

# F4 独立代码审查报告（第 1 轮）

**审查方式**：完全只读静态审查，仅使用 Read 工具。我**没有运行任何测试、命令或调用真实 API**，未读取 `.env`、`config/`、`.claude/`、`credentials.yaml` 或任何真实密钥（只读取了指明的临时 `review-default-gateway.yaml` 快照，其中仅含两条 Mock 配置）。所有测试结论来自执行者回执（`review-evidence.md`）与 `docs/features/F4/test-plan.md` 记录，下述与“已通过”相关的判断均以此为前提。

## 一、重点核对结论（对照已确认契约，静态代码核验）

| 重点项 | 结论 | 依据（文件:行） |
| --- | --- | --- |
| 前置拒绝零 Provider/零 Attempt | ✅ 顺序为 Schema → 模板变量/能力 → 模型 → 装配/预算，全部在 `start_attempt` 之前 | gateway.py:43-81；test_f4_gateway.py:58-120 |
| 调用后失败恰一次 failed Attempt | ✅ validate_result/validate_output 失败均标记 Attempt failed、保留合法 Usage/上游状态 | gateway.py:94-116；base.py:134-148 |
| 错误顺序及原文本不回归 | ✅ 无 Schema 保留 F3 顺序（build_messages→指纹→模型）；旧模板/System/黄金未改 | gateway.py:59-63；prompts.py:13-34,156-213；test_prompts.py 黄金断言保留 |
| Schema 子集与图资源 | ✅ 根必须显式 object、关键字白名单、$defs 全量检查、~0/~1 转义、循环拒绝/重用允许、anyOf 仅“一 null + 一非空”、深度 32/8、节点 256/256 均按边界测试 | validation.py:101-207 |
| Decimal 全链保真 | ✅ HTTP(DecimalRequest/loads)→规范化(dumps sort_keys)→计数(deepseek_v41 dumps compact=False)→SDK 出站(content=bytes)→返回 parsed(structured_response dumps)同一 Decimal；5000 位整数回退 Decimal；超深输出用显式栈编码 | routes.py:14-34；json_codec.py；openai_compatible.py:29-35；test_f4_json_codec.py:52-54；test_f4_gateway.py:141-148 |
| 状态/拒答/截断/Usage 严格 | ✅ Chat refusal+stop→provider_refused、length 优先于 refusal；Responses failed/error 受控映射、incomplete+reason 归一、reasoning 跳过、多 message/工具/矛盾状态拒绝；Usage 严格非负+总和；空白正文 provider_invalid_response | base.py:26-131；test_f4_providers.py:62-97 |
| 不静默重试/换模型 | ✅ retries=0/max_retries=0、follow_redirects=False、trust_env=False、无 fallback 逻辑；302 不跟随 | deepseek.py:12-18；openai_compatible.py:14-17；test_providers.py:185-189 |
| 配置不兼容在客户端构造前拒绝 | ✅ api_protocol/structured_output_mode 必填、adapter–protocol 与 protocol–mode 组合在 ModelRegistry 构造即拒绝，凭据在 lifespan 前读取 | models.py:31-103；factory.py:37-41 |
| 原 System/旧模板/黄金不变 | ✅ 1.2.0 固定 System 与 design.md §3.3 逐字一致（并有测试直读 design.md 校验）；计数视图只构造虚拟字符串，不改实际 messages | prompts.py:216-244；deepseek_v41.py:39-65；test_f4_prompts.py:16-22 |
| 计数：json_object 标志 / 原生 Schema 本体 | ✅ Chat 计 User 中 Schema+`{"type":"json_object"}` 标志视图；Responses 计 Schema 本体；黄金由官方参考独立生成；无 +22 硬编码、未提高余量（安全余量仍 1024） | token_budget.py:37-45；f4_token_goldens.json；test_f4_budget.py:17-26 |
| SDK post(content=bytes) 的真实性 | ✅ 使用同一 AsyncOpenAI 实例的公开 `post`，认证/超时/异常/零重试由真实 SDK 承担；协议测试断言真实 SDK 出站路由、Bearer 头、字节中的 Decimal 原值，非替身 SDK | test_f4_providers.py:24-33,146-160；test_providers.py:96-108 |
| 敏感正文不入 Trace | ✅ Trace 只存元信息/指纹/状态，无 messages/Schema/回答/密钥/上游错误原文；哨兵测试覆盖 | trace.py；test_f4_gateway.py:240-247 |

## 二、Findings

**P0：0 项；P1：0 项；P2：0 项。**

### P3-1（静态推断，建议修复）JSON Pointer 的 `%2F` 解码顺序与 RFC 6901 不符
- **位置**：`src/services/validation.py:131-135`（`_references`）。
- **现象**：实现先对整个指针 `unquote`，再按 `/` 分割。按 RFC 6901 §6，`%2F` 解码得到的 `/` 属于 token 内字符，不应成为分隔符。因此指向键 `a/b` 的合法指针 `#/$defs/a%2Fb` 会被解码成 `$defs/a/b` 并按 3 段查找。
- **影响**：绝大多数情况下目标不存在 → `invalid_response_schema`（误拒绝，零调用，安全）；但若 Schema 同时存在键 `a/b` 与嵌套结构 `$defs.a.b`，指针会**静默解析到错误节点**，输出验收使用错误的子 Schema（可能误通过或误拒绝）。
- **契约核对**：已确认规则（response-schema.md §4）只承诺 `~0`、`~1` 转义——两者实现正确且有测试覆盖（test_f4_validation.py:65-72）；含 `/` 键名的文档化写法是 `~1`，路径安全。故不构成 P2，定级 P3。
- **最小修复**：先按 `/` 分割，再对每个 token 执行 `unquote(errors='strict')`；或保留现顺序但在 unquote 后拒绝含 `/` 的 token 内容。补一个 `#/$defs/a%2Fb` 目标缺失被拒的单元测试即可。
- **未复现**：仅静态推断，我未运行测试。

### P3-2（记录处置，无需修改）F3 历史工件版本断言的必要迁移
- **位置**：`tests/unit/test_f3_token_budget.py:24`——原 F3 测试的 `counter.version` 断言更新为 `revision + ':response-schema-v1'`。
- **说明**：这是计数规则版本有意扩展（设计 §3.5 已确认）导致的对历史工件的必要迁移；黄金编码字节与计数值断言（同文件 17-23 行、fixtures/f3_token_goldens.json 用例）未变，不属于“删断言变绿”。处置：记录，无需修改。

其余我逐一核对过的边缘点（`minLength` 等四个整数参数的元 Schema 占位、1.0 对 integer 的验收与官方测试套件一致、bool 非 integer、600 层数组显式栈编码、`DecimalRequest` 不破坏既有 body 读取、并发请求状态隔离、探测 16/10 固定预算、422/400 边界区分、错误四字段信封与 X-Request-ID 仅放响应头）均与已确认契约一致，未发现可成立的阻断项。

## 三、覆盖与验证来源

- **实际读取文件覆盖**：清单 56 份全部逐份读取（AGENTS×2、F4 设计/导读/测试方案、response-schema.md、模板×2、pyproject、源码 24 份、测试 22 份、fixtures），另加两份指定临时证据文件，共 **58/58**；同一文件无重复读取。
- **验证来源**：(a) 执行者回执 `review-evidence.md`：默认离线 **836 passed / 6 skipped**、Python 3.12 关键集 **233 passed**（弃用警告视为错误）、显式真实 **6 passed**（input_delta 全 0）、F4 静态注册表夹具 **25 passed**，与 test-plan.md §3.8 记录一致；(b) 我的静态代码审读；(c) 协议测试为真实 httpx2/AsyncOpenAI 出站回环（FAKE_CREDENTIAL_MARKER 为测试哨兵）。
- **我未重新运行的测试**：全部执行结论来自执行者回执；本报告中的“通过”均指回执所载结果，我未独立重跑。

## 四、未验证边界（不构成阻断）

- 六例真实接入仅证明当前固定模型/非思考模式与六个样本输入下计数差值为 0，不证明通用一致性或生产可靠性（项目文档已如实声明）。
- 未验证项：真实 Provider 行为超出六个样本的部分、多实例/外部开放、流式、取消、重试/fallback、持久化、能力查询 API——均为本次明确排除范围。
- 凭据文件内容、config 目录与历史工件摘要一致性：按审查约定我未读取，采信执行者回执声明。

## 五、VERDICT：**APPROVED**

P0=0，P1=0，P2=0，P3=2（P3-1 建议修复并补测试，P3-2 记录处置无需修改）。无阻断项；基于已确认契约、实际执行回执与本轮完整只读审查，F4 可进入验收报告环节。
