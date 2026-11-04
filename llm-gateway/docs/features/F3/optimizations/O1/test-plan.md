# F3-O1 测试方案

> 状态：验收通过（2026-11-01）。用户已确认全部范围及显式token_counter规则，并授权实施。对应[设计](design.md)。历史F3通过不是本项实测。

## 1. 范围与通过标准

保护F3 HTTP/错误码/Prompt/指纹/错误顺序、完整计数、总超时、零/一次调用、Trace/并发及资源生命周期。仅显式token_counter和内部接口/spy位置为声明的迁移；原场景保留。当前Mock配置不修改，不立配置切换Bug。不得默认加载实际凭据或访问外部网络。

## 2. 用例

| 编号 | 确定输入/场景 | 预期及副作用/Trace | 层次 |
| --- | --- | --- | --- |
| O1-C01 | token_counter缺省、空串/空白、None/bool/数字 | 定义/配置拒绝，0客户端构造，无请求Trace | 单元 |
| O1-C02 | token_counter未知；注册策略不支持模型；不支持模式 | 启动拒绝，0Provider工厂调用 | 单元 |
| O1-C03 | 原资源缺失/三资源摘要/manifest版本模式不匹配 | 资源错误，无字符回退；原独立黄金不改 | 单元 |
| O1-C04 | 同一Provider两模型分别使用测试策略A/B，计数24/40、版本A/B | 分派按模型，各调用一次，Trace计数/版本准确 | HTTP |
| O1-C05 | 不同Provider共享同一测试策略；两个受支持测试型号 | 共享同一策略，不按Provider绑定，各目标一次 | HTTP |
| O1-C06 | 非DeepSeek测试型号+受控测试策略 | 通用流程成功，无需新增通用型号分支；不代表真实新型号支持 | 单元+HTTP |
| O1-C07 | 完整消息官方黄金236/358/515/17及编码 | 与固定官方资源完全相同，0模型调用 | 单元 |
| O1-B01 | 计数24、输出16、余量8，窗口48/47 | 48成功一次；47拒绝零调用/尝试，保留计数/指纹/版本/预算 | HTTP |
| O1-B02 | 输出超限/超时超限+抛错计数器；计数器自身异常 | 预算优先，计数未执行；计数异常安全500、未知计数不伪造、零Provider调用 | HTTP+回归 |
| O1-T01 | 两模型不同策略，Event受控逆序完成 | request_id关联结果/版本不串线，各一次；无全局策略切换 | HTTP并发 |
| O1-P01 | provider单对象/providers集合；两者同时；空或错误集合；调用方字典修改 | 合法保持身份/只读快照；冲突启动拒绝零构造 | 单元 |
| O1-P02 | 正常关闭；第二构造失败；一个close失败 | 每个已创建对象关闭一次，其他对象仍关闭，无请求记录 | 单元 |
| O1-P03 | 测试注入Provider和完整Settings/Registry/计数依赖 | 不读实际.env/凭据，不调用工厂，不关闭注入对象 | 单元 |
| O1-R01 | 原单元/HTTP/协议/子进程及中断用例回归 | 原场景/零与一次调用/消息黄金/错误码不放宽，声明内部接口迁移留对照 | 全量离线 |
| O1-L01 | 两实际适配器各普通调用、固定16/10探测（4次生成） | 显式开启才加载凭据，stop/Usage/计数版本/actual-local<=1024；不固定回复措辞或要求差值总为0 | 真实HTTP |
| O1-D01 | 模板、F3/O1导读、AGENTS/docs约定/README/架构/进度 | 文件函数/调用链与当前实现匹配，历史工件/用户配置摘要不变 | 文档 |
| O1-V01 | 原生Claude Code只读全部变更和证据 | 实际读取+exit0/is_error=false/end_turn+无P0–P2，P3处置，最多3轮 | 独立审查 |

HTTP共同断言四字段信封、data精确字段、X-Request-ID、无私有错误/密钥回显；Provider与Trace按ID关联，不只验证状态码。测试策略只替换算法边界，不替换Gateway/预算/分派。单元资源黄金与HTTP算术测试各自证明，不能互相替代。

## 3. 执行计划与证据

环境沿用uv.lock/Python>=3.12，不新增依赖。命令：uv run python -m pytest tests/unit/test_f3o1_counters.py tests/integration/test_f3o1_dispatch.py -q；uv run python -m pytest -q；uv run python -m pytest tests/live --run-live -q -o junit_family=legacy --junitxml=docs/features/F3/optimizations/O1/live-results.xml。真实四例只运行一次，通过后不无故重复。缺凭据是未验证，不替代为Mock。

先Red确认断言失败，再最小Green；运行期/配置错误用已有错误出口。计数兼容及依赖校验先于客户端构造；按生命周期复测关闭和中断。保留全部失败回执；导入错误不计有效Red。文档/链接/资源/历史指纹核对后，原生Claude Code独审。

实际执行结果（2026-11-01）：

| 阶段 | 原始回执与结论 |
| --- | --- |
| 基线 | baseline：570 passed/4 live skipped，48.60秒，exit0 |
| 显式必填Red/Green | 缺省旧规则DID NOT RAISE，1 failed/5 passed；改必填后6 passed；后续扩充类型覆盖 |
| 通用计数Red/Green | 旧TokenBudget把映射当资源路径，4 failed；实现策略容器后11 passed |
| 模型分派Red/Green | 已注册测试策略被main具体型号分支拒绝，6 failed；迁移入口与Gateway后77相关用例通过 |
| Provider装配 | 子任务7 Red中5个正确断言失败、2个缺open接口的失败不冒充行为Red；新7 Green，原工厂/生命周期27场景迁移保留，共34通过 |
| 中间失败 | 计数文件移层后资源路径少上溯一层，7 failed，修正后99通过；全量残留一个main.create_provider监视点，1 failed/600 passed，迁移监视位置后复测通过。均保留原始回执，不算新增需求Red |
| 最终离线 | full-offline-corrected：606 passed/4 live skipped，49.93秒，exit0 |
| 真实必验 | real-four-cases：4 passed，4.65秒，exit0；本项目录live-results.xml保存元信息，未记录Key/完整Prompt/回答 |
| 文档/历史检查 | 文档/链接/符号检查通过；原156个测试函数均保留，16个函数因内部接口/spy位置迁移或资源覆盖扩展，原历史16份工件摘要不变；运行Mock YAML未变 |
| 原生Claude Code | 第1轮APPROVED，P0–P2为0、4项P3已核实记录；见review.md |

### 逐组映射与执行结果

| 编号 | 对应测试/证据 | 结果 |
| --- | --- | --- |
| O1-C01 | unit/test_f3o1_counters.py::test_counter_reference_is_explicit_nonblank_string | 通过 |
| O1-C02 | 同文件unknown_or_incompatible、invalid_binding_stops_before_provider_preparation、injected_budget_also_validates、unknown_production_strategy | 通过；未知标识从字段枚举迁至启动注册校验，未放行执行 |
| O1-C03 | unit/test_f3_token_budget.py资源破坏；test_f3o1_counters.py valid_manifest_with_incompatible_metadata | 通过，含三资源摘要及有效JSON错误元信息 |
| O1-C04/C05/C06 | integration/test_f3o1_dispatch.py::test_counter_choice_follows_model_not_provider；unit only_referenced_strategy_is_loaded_once | 通过，同Provider不同策略/跨Provider共享/受支持测试型号 |
| O1-C07 | unit/test_f3_token_budget.py::test_counter_matches_independent_official_encoding | 通过，期望文件/编码/数量未改 |
| O1-B01 | integration/test_f3o1_dispatch.py::test_strategy_budget_preserves_count_before_rejection | 通过，48允许/47拒绝，Trace元信息与零调用对应 |
| O1-B02 | integration/test_f3_gateway.py原预算优先级与计数异常用例 | 全量通过，未知计数保持null |
| O1-T01 | integration/test_f3o1_dispatch.py::test_interleaved_requests_keep_model_counter_version_and_result | 通过，Event逆序完成、按ID关联 |
| O1-P01 | unit/test_f3o1_provider_clients.py注入快照/互斥/集合；原app/resources用例 | 通过，注入身份及只读快照保留 |
| O1-P02 | unit/test_f3_app_resources.py及test_f3o1_provider_clients.py资源关闭/部分构造 | 通过，所有已创建自有对象得到释放 |
| O1-P03 | unit/test_f3_app_resources.py::test_explicit_dependencies_do_not_load_env_registry_or_credentials | 通过，显式依赖不读用户文件、不关闭注入对象 |
| O1-R01 | 606离线测试，migration_audit清单 | 通过；156原测试函数保留，16处迁移/覆盖扩展有据 |
| O1-L01 | tests/live/test_real_providers.py四个参数变体及live-results.xml | 通过，四次input_delta均0，本次对齐不保证通用一致性 |
| O1-D01 | 导读/本地链接/符号与历史摘要实际核验 | 通过，最终核验回执单独保存 |
| O1-V01 | [原生Claude Code第1轮原文](review.md)及evidence implementation_review | APPROVED，P0–P2为0 |

迁移说明：原test_model_cross_constraints中unknown token_counter不再由Pydantic枚举拒绝，移到启动测试证明零Provider准备；没有删除该行为。黄金测试改测DeepSeekV41Counter，预算算术仍测通用TokenBudget；其他主要是main工厂spy移至providers.factory、CountingFixture按统一counter注入、核心服务使用TokenBudget.from_models。原test_invalid_assets_abort_before_provider_factory仍验证重复Prompt资产先于客户端构造被拒绝。

模板闭环：现有设计/测试模板已覆盖职责、已确认迁移与证据，本次新增专用代码导读模板，项目工作流与docs约定要求每项交付并随代码修复同步。不为配置切换另立Bug或扩大测试隔离范围；原F3验收工件不重写。

### 最终独立审查与验收（2026-11-01）

[Claude Code第1轮原文](review.md)由原生CLI、既有连接和认证、Read工具实际产生；exit0、is_error=false、stop_reason=end_turn，**CODE_VERDICT: APPROVED**。42次Read中41次成功、1次evidence范围超过工具上限后缩小重读；31个指定文件均成功读取，另读3个上下文文件及完整差分。原始回执按范围抽查，不声称全文读取；审查没有重跑测试，测试结论来自执行者证据核对。源码/测试/Mock配置51份摘要审查前后一致。

| P3 | 核实与处置 |
| --- | --- |
| 测试模块分段import | 纯风格项，记录保留当前已验证快照；后续修改此文件时可统一整理 |
| counter_for重复查找 | 保留“先记录版本、后计数”的异常语义；查找只读且廉价，不为消除此项增加抽象 |
| runs排列顺序 | runner在命令完成后追加，基线先启动，首组Red更早结束；生产代码首次修改在基线结束之后。保留原始顺序，不重排/补造时间 |
| 版本取常量而非manifest | 构造时两者严格相等，语义一致，保留 |

最终 **F3-O1验收通过**：原156测试函数保留；606离线通过/4默认live跳过，显式真实四例4通过；黄金/窗口/分派/Trace/生命周期及文档规则均有证据，原16份历史工件和当前Mock YAML未改。真实四次差值0仅证明本次组合，未验证极限窗口、所有模型/输入或生产可靠性。本项独审共1轮，无新增模型/Provider协议或自动恢复行为。

三项交付对应[本项导读](implementation-summary.md)、[F3导读](../../implementation-summary.md)与[通用模板](../../../../templates/代码实现导读模板.md)。本轮通过实际交付验证了模板可覆盖当前导读，不声称后续Feature质量已提高。
