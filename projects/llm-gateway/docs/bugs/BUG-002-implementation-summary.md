# BUG-002 代码实现导读

状态：**验收通过**。最终全量2086通过，含28个真实节点；[首轮独立审查](BUG-002-review-r1.md)APPROVED，P0—P3均为0。对应[已确认方案](BUG-002-offline-config-isolation.md)和[实施步骤](BUG-002-implementation-plan.md)。只修改测试/夹具，不改变产品源码或运行配置。

## 1. 问题与本次行为

原部分测试无参load_registry()读取维护者模型YAML，切成真实Provider会让固定Mock断言失败。现在测试自带ModelRegistry/临时YAML，默认路径契约仍通过实际模型模块的隔离副本验证。非显式live测试拒读维护者模型配置、真实Key和数据库密码。

普通、结构化、流式、断连续传和取消共用同一组步骤及Gateway断言，Mock和真实夹具分别提供执行条件/专属留证，ENV均可为test。纯逻辑与受控故障专项不被复制成两套远端调用。

## 2. 调用链与阅读顺序

| 文件/入口 | 作用及应核对的问题 |
| --- | --- |
| [tests/conftest.py](../../tests/conftest.py) / isolate_real_provider_access | 所有非显式live节点守卫模型YAML/Key/密码及外部网络，路径两侧归一化以覆盖维护者文件本身是符号链接。公共验收自有PG/身份不被历史装配覆盖。 |
| [tests/bug002_cases.py](../../tests/bug002_cases.py) / write_registry | 纯测试定义写临时YAML；real=True只是合法的虚假双协议定义，不读真实配置、不调用Provider。 |
| [tests/unit/test_bug002_registry.py](../../tests/unit/test_bug002_registry.py) | 复制实际src/services/models.py到临时项目、独立导入；检查默认目录、默认文件名、忽略环境YAML和非法值拒绝，不mock生产解析。 |
| [tests/unit/test_bug002_isolation.py](../../tests/unit/test_bug002_isolation.py) | 守卫自身正负例、三类配置及符号链接子场景、收集/显式live缺Key。子进程观察收集与执行期间的维护者文件读取，未复制真实凭据。 |
| test_f3_config/test_core及三份协议消费方 | 用测试自建配置替换原维护者YAML依赖，消息/调用次数/错误/Usage断言保留；配置解析测试仍调用真正load_registry。 |
| [tests/bug002_scenarios.py](../../tests/bug002_scenarios.py) | 共用API步骤：两模型×普通/结构化/流式成功六节点、续传两节点、取消两节点；不按backend绕过业务断言。 |
| [tests/live/test_f10_acceptance.py](../../tests/live/test_f10_acceptance.py)、[Mock入口](../../tests/integration/test_bug002_common.py) | 原10个真实公共节点与新增10个Mock节点调用同一函数；原真实CLI附加节点和容量继续保留。 |
| [tests/f10_support.py](../../tests/f10_support.py) | Environment默认real保留原行为，Mock使用临时PG/配置、typed脚本和每request_id调用/关闭记录；真实仍通过原适配器/TCP/原文对照。 |
| [观察反例](../../tests/unit/test_bug002_common_observation.py) | 合成事实/Usage、来源/调用/关闭约束、剩余超时正负边界、跨Task双流清理归属反例。 |

## 3. 三个边界

公共API链：显式Mock/真实夹具 → 自有Gateway子进程与PG → 同一scenario函数 → 共同结果/事件/SQL/账务断言。Provider协议或合成数据的专属事实由Environment观察器核对，不在scenario内跳过。Mock不声称执行远端Chat/Responses协议，原始实报只由真实接入证明。

配置链：测试自建定义 → 临时gateway.yaml → 真正load_registry；默认加载路径在自有源码副本验证。离线守卫不读取真正秘密来证明拒读，raw IO探针会在文件操作前失败；本地回环和测试临时假凭据仍允许。

清理链：typed脚本每次调用捕获独立Context，观察回调绑定该上下文；finally在另一Task执行时仍归属原request_id。不临时修改共享Provider字段，关闭次数断言不放宽。

## 4. 验证及失败定位

- 原始隔离真实YAML导致默认Mock断言失败，已取得正确Red；同场景更新测试配置后Green。
- 守卫从允许维护者YAML进入raw IO到明确拒绝，反例Red/Green已取得；符号链接缺口也用反例修正。
- 首轮Mock集成把剩余预算误断言固定30，10失败；修正为0<剩余预算<=30并保留输出128后6通过/4失败，后者查明清理观察丢上下文。跨Task反例修正后Mock10通过。产品没有为测试改动。
- 配置矩阵及缺Key/默认启用边界、收集阶段符号链接观察反例14通过，消费者单元89/协议112通过，脚本/观察反例18通过；最终结果与原始回执已在[验收记录](BUG-002-acceptance-report.md)单独归档，不累加上述批次作全量。
- 当前收集2086节点，原F10全量2039节点均保留，新增47；真实节点保持28。真实公共验收11通过（含原CLI附加节点）；最终含容量全量2086通过/1770.19秒，首轮独立审查APPROVED，已验收通过。

失败时先按request_id找Mock typed记录或真实wire/最终正文，再核对已提交SQL/费用；配置问题看子进程runtime_reads及原始pytest回执。所有拒读、网络与资源归属结论按观察范围记录。真实供应商账单、长期/多实例生产证明不因本Bug扩大，F10历史证据不改写。

自检追加收集阶段符号链接读取观察器反例，原观察器漏记读取，正确Red已取得；修正观察器的路径映射后14项通过。此前全量在561 passed时主动中止、未进入真实调用，旧回执保留，不作为最终全量。以修正后的最终代码重新完整执行。
