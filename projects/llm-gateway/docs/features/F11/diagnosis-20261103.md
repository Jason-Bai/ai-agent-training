# F11：上游503与真实超时诊断

日期：2026-11-03。用户授权先诊断、拿真实证据后再决定，不修改实现、运行配置、超时、次数或验收门槛。本记录是诊断，不是修复或验收通过；F11继续阻塞。

## 1. 当前结论

1. **现有503恢复链并非缺失。** Provider将上游503归为有暂时故障证据的`provider_unavailable`，Gateway可按F7有限重试；本次已有恢复专项134项通过，包括两种真实适配器连接本地HTTP替身的“第一次503、第二次200”普通/结构化/流式六例。这里的503是受控替身，不能写成真实供应商故障恢复证据。
2. **本轮真实供应商没有重现503。** 12次Gateway诊断调用中11次完成、1次流式总超时；两次绕过Gateway的原始HTTP流均完成。所有实际记录的上游HTTP状态都是200。这不撤销历史旁路503，也不说明供应商已经稳定恢复。
3. **本轮复现的是HTTP 200之后的流式停顿。** Responses流收到一条“订单”后，直到30秒期限都未交出后续内容/结束事件。Gateway发送心跳，最终提交唯一failed事件；没有切模型或延长期限。该次只有帧层观察；后加原始行/字节观察的六次流式/结构化调用均完成，仍不足以把那次停顿最终定位到供应商、网络或解析器。
4. **不能把并列的两条真实接入理解成已配置备用链。** 旧真实测试注册表的恢复次数是总3次、单模型2次，但两模型`fallback_models`均为空；本次复用原注册表，没有补备用。因此可观察同模型重试，不能验证备用切换。
5. **尚无证据支持立即新增一套503重试机制。** 原5项失败不能统归一个原因，全部根因尚未查明。没有修改代码或豁免失败；四档容量、600秒持续运行和最终完整回归仍待完成。

## 2. 已执行范围与真实结果

测试环境使用自建配置、独立随机端口/子进程及独立Compose PostgreSQL；不连接维护者的业务数据库。真实凭据仅由原Provider加载路径读取，运行YAML和凭据未替换。各批次串行发起真实请求，不制造高并发压力；两层名额配置20/5。

| 编号 | 请求 | 结果 | 客户端耗时约 |
| --- | --- | --- | --- |
| D01 | 主侧健康探测 | completed，1次Attempt | 7.81秒 |
| D02 | 备侧健康探测 | completed，1次Attempt | 4.60秒 |
| D03 | 主侧流式 | completed，1次Attempt | 3.89秒 |
| D04 | 备侧结构化 | completed，1次Attempt | 2.41秒 |
| D05 | 主侧普通文本 | completed，1次Attempt | 6.65秒 |
| D06 | 备侧流式 | failed/request_timeout，1次Attempt | 30.37秒 |
| D07 | D06相同上游body，直接HTTP读取Responses流 | HTTP200、收到completed及EOF | 8.09秒 |
| D08 | D03相同上游body，直接HTTP读取Chat流 | HTTP200、收到完成帧及EOF | 12.92秒 |
| D09 | 备侧流式，增加原始行观察 | completed | 7.35秒 |
| D10 | 主侧流式，增加原始行观察 | completed | 4.21秒 |
| D11 | 备侧结构化 | completed | 0.47秒 |
| D12—D14 | 最多3次串行备侧流式，增加原始字节/行观察，约定捕获首个失败即停止 | 三次均completed，没有捕获失败 | 9.82/10.15/7.82秒 |

这些是带真实调用、Trace/SQL/账务观察的诊断结果，不能替代旧F4/F5全部断言或最终`--run-live`。直接HTTP调用不经过Gateway账务；不与Gateway请求混算。

## 3. D06具体时间线

request_id：`d486b988-608c-4763-a889-c1958fc316fe`。

| 北京时间 | 原始观察 |
| --- | --- |
| 17:33:33.393 | Gateway开始执行；总期限为17:34:03.393 |
| 17:33:33.710 | 上游HTTP200响应头 |
| 17:33:34.187 | 原始Responses `response.output_text.delta`被解析为“订单” |
| 开始约0.88秒 | 调用方收到持久化的seq=1、`response.delta`、“订单” |
| 开始约15.88秒 | 调用方收到不带seq的`response.heartbeat` |
| 17:34:03.398 | 达到总期限，上游读取被取消；没有观察到completed/Usage |
| 开始约30.37秒 | 调用方收到seq=2、唯一`response.failed/request_timeout` |

Trace：`error_stage=provider`、1次Attempt、`recovery.wait_ms=0`、`recovery.stop_reason=null`、无清理错误。Usage未知，账务保持pending；没有将未知记零。三个Gateway诊断批次都核对了已提交终态、事件、账务及账户恒等式；排空后请求名额、Agent名额、生成/订阅/清理/取消任务均归零。

不能将`stop_reason=null`解释成“没有恢复逻辑”：总期限直接中断执行，不一定进入恢复决策分支；`error_code`和deadline显示这次已耗尽总时间。并且首条delta已落库，按F7不得重试或静默切模型。

帧层记录证明适配器在该次执行中只交出一条非空增量，无法排除底层读、分行或解析层的间歇性问题。D07是另一次相同body的调用，成功只能证明该请求可以完成，不能单独排除Gateway缺陷。后加原始字节/行观察的成功样本也不能替代失败现场证据。

## 4. 对原五项失败能确定什么

| 历史项 | 已有证据与恢复边界 |
| --- | --- |
| F10容量 | 失败request_id为`b46ff4cf-9477-412f-9cb9-d3edb869de93`。原始上游只留初始元信息和“订单”增量；下游seq=1已落库，30秒后request_timeout；无Usage，账务pending、资源归零。这与本轮D06形态相似，但不是同一请求。 |
| F5备用流 | 原JUnit记录request_timeout；没有该次完整上游时间线，不能拿D06代替其全部根因。 |
| F4备用结构化 | 原JUnit记录30秒左右request_timeout；该次没有完整上游逐Attempt回执。本轮D04/D11完成，不足以解释历史超时。 |
| F4主/备健康探测 | 原测试得到Gateway HTTP503/provider_unavailable。健康探测按既定设计只调用指定模型一次，不重试、不切备用；该表现不能证明普通调用的恢复失效。原两次未存原始HTTP状态，不能把Gateway503直接等同为两次上游HTTP503。 |

历史[旁路503](evidence-direct-upstream.json)确证当次绕过Gateway仍收到上游HTTP503；这是单次上游故障证据。原失败中的所有Attempt是否都收到503、是否每次按预期重试，归档不足，不能补造。

## 5. 代码核查与受控实验

- [错误归一化](../../../src/providers/base.py)：保留安全的上游HTTP状态/暂时故障/Retry-After提示。
- [恢复决策](../../../src/services/recovery.py)：503可重试；总3次、单模型2次，候选耗尽时由执行器检查备用。
- [普通执行器](../../../src/services/gateway.py)：单次期限取模型上限和请求剩余时间较小值；总期限耗尽直接终止。当前模型单次上限120秒，请求总期限30秒，因此第一尝试可能用完全部30秒，不保证有重试时间。
- [流式执行器](../../../src/services/streaming.py)：首条delta落库后不恢复；总期限不因心跳、重试或备用而延长。
- [真实测试注册表](../../../tests/live/test_real_providers.py)：恢复策略使用默认3/2；两个模型均未配置fallback链。两种接入仍共用同一DeepSeek host/credential，不能宣称供应商故障隔离。
- 健康探测在Gateway中覆盖为1/1，语义是验证指定模型当时能否响应，不用恢复后的结果掩盖该接入状态。

执行已有测试，未新增或修改断言：

```shell
uv run --python 3.12 python -W error -m pytest \
  tests/unit/test_f7_recovery.py tests/integration/test_f7_protocol.py \
  -o junit_family=xunit1 \
  --junitxml=/tmp/llmgw-f11-diagnostic-recovery.xml -q
```

结果：**134 passed /5.11秒，0失败/错误/跳过**。其中真实适配器访问本地HTTP替身的六例验证两协议×普通/结构化/流式的503→成功，核对两次请求、`initial/network_retry`、PG已提交Attempt及结果。它们证明受控503可沿现有链恢复；不证明真实供应商503一定恢复。

## 6. 工件、保护与下一步

- [诊断索引及实际命令/返回码](evidence-diagnosis-20261103.json)：14次真实调用，分开12次Gateway和2次旁路；保存每次request_id、Trace、原始上游状态、排空观察及证据限制。
- [原始工件归档](evidence-diagnosis-20261103.tar.gz)：53文件，含实际执行脚本、临时bootstrap、原始帧/行/字节记录、逐请求输入/响应/Trace、有效测试策略、来源摘要和文件哈希；没有打包config目录、凭据、密码、Bearer或symlink。
- [受控恢复JUnit](evidence-diagnosis-20261103-recovery.xml.gz)。
- 196份源码、测试、安全YAML、迁移及两份AGENTS的执行前后摘要一致；本次仅新增诊断文档/工件和更新F11测试记录链接。未覆盖或重写原审查报告、历史测试工件、实际运行绑定和Key。

后续需要捕获**失败现场的原始字节/行时间线**，再区分上游/网络停顿与适配器读取问题。若重现自然503，还需同步保存每次Attempt状态、等待和最终停止原因，才能判是否违反F7契约。没有复现的原因保持未确定，不继续无上限调用或用多次成功覆盖失败。

当前不新增恢复组件、不修改健康探测语义、不放宽超时或验收。若证据表明已有恢复未按契约执行，再走Bug路线；若要增加独立供应商或持续故障避让，另按模型管理/路由范围讨论。
