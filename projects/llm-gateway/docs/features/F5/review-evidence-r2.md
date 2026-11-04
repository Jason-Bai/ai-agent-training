# F5 第2轮证据来源与当前状态

本摘要由执行者编写，是原始证据索引而非审查结论。完整第1轮CLI报告保留在review-r1.md，实际工具审计见下方元信息；不能直接采用其“零缺陷”或已读自述。

## 当前版本验证

- r2-full：1097 passed / 8 live默认跳过，72.91秒。
- r2-py312：独立临时环境233 passed，14.85秒，DeprecationWarning作为error；与全量重叠。
- r2-live：真实两例2 passed，2.57秒，Chat/Responses各1，input_delta均0；当前F5累计6次真实调用（首轮2+首轮修订后2+本次修订后2），各轮XML保留。
- 上述三次命令的76份输入均各自前后一致且等于当前R2审前摘要。首次审查完成前原76份不变；之后只修改4份源码与2份测试，完整增量212行。
- 51份受保护资源、历史验收及本机.env/credentials摘要仍未变；默认两Mock未切真实。
- r2-docs检查实际29个源码文件目录/本地链接/JSON/51组用例，原始审查报告根相对链接按项目根核对并保持原文。

## 两项执行者实测闭环

1. 已确认Usage/模型元信息丢失：项目代码不变时做离线实际Adapter→Gateway复现；在临时副本先补Chat缺失/损坏尾块2个Red，再补Responses已知model后EOF/坏块2个Red。修复每流局部observed元信息，不推断上游正常终态；追加2个真实回环到Gateway Trace的断言（首次即Green，属于补验收）。隔离协议/执行116通过后，首轮CLI完成且核对原76份不变，才落回项目6文件，相关集164通过。
2. 首轮报告反例6的零Attempt推导不成立：受控同步头发送返回晚于deadline，实测Provider.opened=1/Attempt=1。新增正确Red，创建Attempt之前显式重查deadline，复测0调用/0Attempt。与一直挂起的start被取消不是同一个时序，不能由后者推断前者。

原始输出均在下列确切txt，位置只供本次安全Read：
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/isolated-tail-usage-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/isolated-progress-model-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/isolated-expired-start-red.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/isolated-usage-deadline-green.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-fixes-green.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-full.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-py312.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-live.txt
- /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llm-gateway-f5-29boohvh/r2-docs.txt

## 首轮实际工具审计

- 原生CLI退出0、is_error=false、stop_reason=end_turn，947.31秒。
- 82个Read调用、59个唯一文件；63项必读中4项未见单独Read：AGENTS.md、空utils/__init__.py、test_f3_config.py、test_f5_streaming.py。后两项变更在全文diff可见，不冒充单独读文件。
- 只有Read成功执行；一次MCP Chrome evaluate_script请求权限拒绝、未执行，属于范围外工具请求。第二轮通过进程级strict空MCP配置收紧工具，不修改用户全局或模型/认证配置。
- 原报告自述范围/部分“静态确定”不作为事实依据。报告已完成但项目尚未验收；本轮应独立审查三个维度，不只是确认两处patch。

完整历史raw_runs与摘要保存在evidence.json；本轮不要求重复读取其中巨大中间段，以确切原始txt/当前输入摘要/当前源码及测试为依据。需要额外安全材料可按需Read，不应仅因测试数或文件数判通过。
