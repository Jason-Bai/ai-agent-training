# F3-O1 第1轮原生Claude Code独立审查

仅Read，沿用既有连接和认证，未重跑测试；以下为原文，执行者核实及处置见test-plan.md。

全部指定文件已逐文件实际 Read 完毕（含差分全文 896 行），并补充核对了 `credentials.py`、`tests/conftest.py`、`tests/live/test_real_providers.py` 三个必要上下文文件。未读任何 `.env`、`config/*.yaml`、`tokenizer.json`、`reference_encoding.py`、`uv.lock`；全程仅用 Read，未运行任何测试或命令。

# F3-O1 第 1 轮独立只读审查报告

## 一、测试执行声明

我未重跑任何测试。以下全部测试结论来自执行者证据（`evidence.json`、`live-results.xml`、`test-plan.md`），我仅核对回执真实性、退出码与前后一致性，不据此声称重跑。

## 二、规格核对（三项交付与保留契约）

### 交付 1：代码导读模板 + F3/O1 导读及工作流
- 模板 `docs/templates/代码实现导读模板.md` 六节齐全（行为变化/调用链/阅读顺序/取舍/成功失败定位/验证边界），与 AGENTS.md 第 41 行要求一致。
- F3 与 F3-O1 两份 `implementation-summary.md` 均已产出，状态标注“待独立审查”，未提前声称验收。相对链接层级核对正确（O1 导读 `../../../../../src/...`、F3 导读 `../../../src/...` 均落到真实文件；所列 `ModelConfig.token_counter`、`TokenCounter/TokenCount`、`build_counters`、`DeepSeekV41Counter`、`TokenBudget`、`ProviderClients`、`create_app`、`GatewayService._complete` 全部存在且职责描述与源码一致）。
- 工作流已同步：`AGENTS.md`、`docs/AGENTS.md` 规则 10、README、architecture、progress 均已更新，F3-O1 单列“待独立审查”，原 F3 验收记录未改写。

### 交付 2：main 职责拆出 ProviderClients
- 旧 main 的注入归一化、凭据解析、lifespan 构造/关闭全部迁入 `src/providers/factory.py:23-65` 的 `ProviderClients`；`create_provider` 保留原签名与行为。差分核对确认旧逻辑逐条等价迁移：
  - 互斥注入（`provider`/`providers` 同给 → ValueError，factory.py:29-30，原报错文案保留）；
  - 注入集合与注册表名称精确匹配（factory.py:33-34）；
  - 注入快照只读（`dict(providers)` 拷贝 + `MappingProxyType`，factory.py:35），调用方事后清空/改写不影响；
  - 凭据只在非注入路径于 `__init__` 解析本地文件（factory.py:41，`load_credentials` 空引用集短路不读文件，credentials.py:9-10，保持“仅 Mock 不读凭据文件”契约）；
  - 工厂客户端延迟到 `open()` 构造；构造失败/退出时在 `finally` 逆序关闭所有已创建自有对象、注入对象不关闭（factory.py:44-64）；关闭异常吞掉内部细节后统一 `RuntimeError("Provider 客户端关闭失败") from None`，与原 main 可观察行为一致（原 `while owned.pop()` 逆序语义与 `reversed(owned)` 等价）。
- `main.py:31-36` 只保留装配与短 lifespan 挂载；main 不再含具体型号分支（原 `any(model.model_id != "deepseek-flash")` 检查已移除）。
- ProviderClients 不依赖 FastAPI（factory.py 无 fastapi 导入），符合“不是统一应用容器”边界。

### 交付 3：通用 TokenBudget + DeepSeek 编码资源封装
- `ModelConfig.token_counter: str`（models.py:54），`Definition` 为 `strict=True` + 非空白校验 → 缺省/空串/空白/None/bool/数字/列表/字典全部定义级拒绝（O1-C01）；供应商专属默认值与枚举已移除，属已确认的显式必填迁移。
- `TokenBudget`（token_budget.py）完全通用：`__init__` 复制策略集为只读代理；`from_models` → `build_counters` → `validate_models`；`counter_for` 按 `model.token_counter` 查表 + `supports(model)` 兼容校验；未知策略抛 `ConfigurationError("计数策略未注册")`、不兼容抛 `"计数策略与模型或运行模式不匹配"`，均在 `ProviderClients` 构造前（main.py:30-31 先于 31 行 clients 构造）拒绝，未放宽可执行配置。
- `token_counters/__init__.py` 为受控工厂字典 `{"deepseek_v41": DeepSeekV41Counter}`，无动态导入，只构造被引用策略且每标识一次（测试证明未知键先拒、未引用策略不加载资源）。
- 具体编码封装在 `deepseek_v41.py`：资源校验（manifest revision/model/mode + 三资源摘要 + 实际字节哈希，error 文案无路径无回显）、固定文本编码（`encode` 原样迁移自 F3 token_budget）、`supports` 具体化为 `model_id == deepseek-flash and thinking_mode == disabled`（deepseek_v41.py:59-60）。`DEFAULT_ASSET_DIR` 为项目根 `assets/...`（parents[3] 修正了中间迁移的少一层错误，README 链接一致）。通用层（main/gateway/token_budget/`__init__`）不含任何具体 model_id 判断，符合“不按 Provider 绑定、通用入口不判断具体型号”。
- **保留契约逐项核对**：`gateway.py` 仅第 51-53 行变更（差分确认其余未动）——先记录策略版本再计数（第 51 行在 count 之前，保留原计数异常时的记录语义）、`check` 公式 `输入+输出+余量<=窗口` 逐字保留（token_budget.py:40-42）；固定顺序 结构/Prompt/指纹→模型→输出/超时→计数/窗口→尝试→Provider→结果验收 与代码顺序一致；输出/超时错误在计数前拒绝；窗口拒绝零尝试且保留计数/版本/指纹/生效预算；HTTP 信封、错误分类、Prompt/指纹、超时边界、Trace/request_id、Middleware、api 层均不在差分内。

### 测试策略边界
- 分派测试用 fixture 模型/策略（`test_f3o1_dispatch.py` 的 `fixture-a/fixture-b` + `Counter`），同 Provider 异策略、跨 Provider 共享策略、预算边界 48/47、Event 逆序并发均证明通用分派而非新真实型号；文档明示“测试策略不代表支持新真实型号”，未越界声明。

## 三、证据核对

| 项 | 证据 | 核对结论 |
| --- | --- | --- |
| 基线 | evidence baseline：570 passed/4 skipped，48.60s，exit 0 | 与 F3 验收数一致 |
| 最终离线 | full-offline-corrected：606 passed/4 skipped，49.93s，exit 0 | 570+36 新测试+1(LICENSE 破坏例)-1(test_f3_config 移除的枚举例)=606，算术吻合 |
| 真实四例 | real-four-cases：4 passed，4.65s，exit 0；live-results.xml 仅元信息（无 Prompt/回答/Key），`input_delta` 全 0，`counter_version`=REVISION | 与 live_metadata、xml_sha256 一致；机制核对：tests/live 在 pytest 进程内经 `uvicorn.Server`（lifespan=on）+ 回环 127.0.0.1 真实 HTTP 进入 Gateway、真实适配器调用供应商，`delta <= safety_margin` 断言（该文件不在差分内，沿用 F3） |
| Red 回执 | red-explicit-counter（DID NOT RAISE，1 failed/5 passed）、red-generic-budget（4 failed，旧 TokenBudget 把映射当资源路径）、red-model-strategy-dispatch（6 failed，旧 main 具体型号分支拒绝已注册策略）、red-provider-clients（7 failed：5 个断言失败 + 2 个缺 open 的 AttributeError） | 均为正确断言失败，非导入/拼写错误；2 个 AttributeError 未冒充行为 Red，与 test-plan 陈述一致 |
| 中间失败保留 | green-counter-goldens（7 failed，资源路径少上溯一层）、full-offline（1 failed/600 passed，残留 main.create_provider 监视点） | 原样保留回执，未冒充 TDD Red，与陈述一致 |
| 迁移审计 | original_test_functions_retained=156，changed_functions=16 项（与差分逐一吻合），16 份历史工件哈希未变，runtime_config_unchanged=true | 与“原156函数保留16迁移/覆盖扩展、原16历史工件与运行Mock YAML未改”一致 |
| 文档核验 | document-contract-verification：exit 0，10 份当前文档/103 本地链接、16 历史哈希、运行 Mock YAML | 通过 |

## 四、发现的问题

**P0：无。P1：无。P2：无。**

P3（不影响验收，记录处理结论）：
- **P3-1**（代码风格）`tests/unit/test_f3o1_counters.py:19-23`：第二个 import 块出现在首个测试之后，模块级导入位置后置。仅风格问题，无行为影响，全量通过。处理建议：合并到文件顶部。
- **P3-2**（轻微冗余）`src/services/gateway.py:51-52`：每请求对 `counter_for(model)` 执行两次（先取 version，`count()` 内部再查一次）。当前形式是为保留“先记录版本、后计数”的异常语义的刻意设计，行为正确；如需优化可单次取 counter 后复用，属可选整理。
- **P3-3**（证据呈现）`evidence.json` 的 runs 数组中 `red-explicit-counter` 排在 `baseline` 之前，与设计步骤 1“先基线”的叙述顺序不同。基线输出 570 passed 不含新测试文件，与新文件后建的事实自洽，仅 JSON 排列顺序问题，不影响真实性判断。
- **P3-4**（说明性观察）`DeepSeekV41Counter.version` 取自常量 `REVISION` 而非 `manifest["revision"]`（deepseek_v41.py:32）；两者经构造期校验必然相等，语义等价，无需修改。

## 五、结论

三项交付全部落地且与已确认设计/测试方案一致；`token_counter` 显式必填、未知策略启动拒绝、不按 Provider 绑定、原 HTTP/错误/Prompt/指纹/预算/超时/Trace/生命周期行为保留均经源码逐行与差分双重核对；Red/Green、中间失败、最终 606+4skip、真实四例的原始回执完整且互洽，未发现把文档/Mock 写成真实验证或把未运行测试写成通过的表述。测试结论来自执行者证据，我未重跑。

**CODE_VERDICT: APPROVED**（无 P0–P2 阻断项；4 项 P3 已记录处置建议，不影响验收）。
