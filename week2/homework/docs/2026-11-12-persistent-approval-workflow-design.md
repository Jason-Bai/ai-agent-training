# 退款与转账的持久化审批方案

发起人确认退款或转账后，需要审批的调用进入待办。审批人可以在另一条 CLI 命令中查看、批准或拒绝。批准后 Runtime 立即尝试执行原请求，并分别保存审批决定和执行结果。

本方案面向 [homework demo](../tool_governance_demo.py) 和 [对应测试](../test_tool_governance_demo.py)。订单侧使用已有的 `create_refund` 发起退款申请。原有 [转账治理方案](2026-11-12-transfer-governance-design.md) 继续提供转账参数、业务规则和执行超时的依据。本方案替代其中在同一次调用里等待审批的部分，也替代退款的教学默认审批。所有业务调用和审批列表调用从 `ToolRuntime.invoke()` 进入，沿用现有 `PermissionEngine.decide` 的权限判断顺序。

## 1. 身份与权限

CLI 根据 `(tenant_id, user_id)` 从程序预设的身份表查找角色、权限和允许调用的工具，构造 `ExecutionContext`。命令行中的两个 ID 用来选择模拟身份。工具参数中的租户、用户、角色或权限字段由严格参数模型拒绝。演示中 `tenant_a/u_100` 是发起人，`tenant_a/u_200` 是审批人。

| 动作 | 发起人 | 审批人 |
| --- | --- | --- |
| 发起退款或转账 | 使用 `refund:create` 或 `transfer:create` 创建自己的请求 | 无创建权限 |
| 确认 | 在发起调用中确认自己看到的参数 | 无确认权限 |
| 查看审批 | 使用 `approval:list` 查看同租户内自己发起的记录 | 使用 `approval:list` 查看同租户内他人发起、且自己有对应工具审批权限的记录 |
| 查看详情并批准或拒绝 | 无审批权限 | 使用 `approval:decide` 和对应工具的审批权限，处理同租户内他人提交的待办 |

退款需要 `refund:approve`，转账需要 `transfer:approve`。列表、详情和审批提交都由 Runtime 执行各自的访问检查。审批检查比较记录中的 `initiator_id` 与当前用户 ID，即使身份误获审批权限也会拒绝自审。直接输入其他租户、本人或无权处理的请求编号时，对外统一返回“请求不存在或不可访问”。

业务示例使用相应工具的现有权限。演示身份缺少退款权限时，CLI 只在该例子的上下文中移除 `refund:create`，由 Runtime 返回拒绝。审批人上下文只开放审批查询和人工审批所需能力。普通订单查询仍走 `get_order` 自己的白名单与 `order:read` 权限。

## 2. 发起、确认与提交待办

1. 发起人运行 `create_refund` 或 `transfer`。Runtime 校验参数，检查白名单、业务权限和模式，然后做只读业务预检。确认提示使用校验后的参数快照。
2. 发起人明确同意后，Runtime 重新校验原始参数，核对摘要与确认时一致。拒绝、超时或参数变化沿用现有确认错误，业务状态和审批目录均无新增记录。
3. 退款及金额大于 `80000` 且通过业务预检的转账进入待审批。Runtime 将确认快照写入 JSON，保存成功后返回 `ToolResult.ok=True`，并在 `content` 中给出 `status="PENDING_APPROVAL"` 和 `request_id`。调用方用状态区分提交成功与业务执行成功。此时 `REFUNDS`、`TRANSFERS` 和余额都还未发生本次操作的写入。
4. 只需确认的普通转账继续进入受控执行。比如 `50000 < amount <= 80000` 仍在预检时被教学规则拦截。

Runtime 在参数、权限和模式校验通过后，于业务预检前按租户、发起人和原 `tool_call_id` 查询已有审批记录。工具和参数摘要相同则返回已有状态，无须再次确认。工具或参数不同则拒绝复用该调用 ID，新的请求使用新的调用 ID 并重新确认。未找到记录时继续预检和确认，并在落盘前持锁再查一次，以处理并发提交。该去重规则适用于待审批记录。CLI 正常发起每次调用时生成新 ID。

## 3. JSON 文件与状态

路径为相对 demo 文件定位的 `homework/store/approvals/<request_id>.json`，从不同工作目录启动时使用同一个存储位置。请求编号由 Runtime 根据租户、发起人和原调用 ID 生成安全的固定格式标识，查询时验证编号格式。每份记录保留以下内容。

| 字段 | 用途 |
| --- | --- |
| `request_id`、`tool_call_id`、`tool_name` | 定位待办并关联原业务调用 |
| `tenant_id`、`initiator_id` | 约束查询、审批和恢复执行的身份 |
| `arguments`、`args_digest` | 保存确认过的规范化参数，恢复执行前用参数模型及现有摘要算法校验 |
| `confirmation_id`、`confirmed_at` | 关联发起人的确认 |
| `approval_status`、`approver_id`、`decided_at`、`decision_call_id` | 保存审批决定和独立审批调用的关联 |
| `execution_status`、`started_at`、`finished_at`、`error_code`、`result` | 保存执行进度和脱敏后的业务结果 |

退款金额采用参数模型导出的 JSON 表示，恢复时由模型解析。执行用的确认参数保存在本地存储中，列表只投影展示所需字段。JSON 写入用同目录临时文件加原子替换。持久化失败时返回存储错误，待办提交成功的返回以文件写入成功为前提。

| 事件 | 审批状态 | 执行状态 |
| --- | --- | --- |
| 确认并保存 | `PENDING_APPROVAL` | `NOT_STARTED` |
| 审批拒绝 | `REJECTED` | `NOT_STARTED` |
| 审批批准并取得执行机会 | `APPROVED` | `RUNNING` |
| 执行正常结束 | `APPROVED` | `SUCCEEDED` |
| 权限、参数、业务预检或明确的执行拒绝 | `APPROVED` | `FAILED` |
| 写入超时、中断或无法确认是否产生副作用 | `APPROVED` | `UNKNOWN` |

CLI 每条命令启动独立进程，进程内的 `asyncio.Lock` 无法保护同一 JSON。因此，同一个请求使用独立锁文件和非阻塞的 POSIX 文件锁（`flock`）。提交阶段持锁去重与写入。审批阶段持锁读取当前状态，批准时把 `APPROVED/RUNNING` 一次写入并持锁到执行结果落盘。锁正被占用时，Runtime 检查访问权限后返回“处理中”。若上一次进程中断，后续审批命令取得锁并发现遗留的 `RUNNING` 时，将它转为 `UNKNOWN`，保留请求以便核对。已获批准的请求均只取得一次执行机会。

## 4. 审批列表和人工审批

- `list_approvals` 是只读工具，参数仅有可选的 `status`。默认 `PENDING_APPROVAL`，也可按 `APPROVED`、`REJECTED` 或 `ALL` 查询。Runtime 从当前身份决定范围，按确认时间倒序返回。列表字段包括请求编号、工具类型、发起人、确认时间、审批状态、执行状态和脱敏业务摘要。审批人看不到自己的记录，发起人看不到同租户其他人的记录。
- `decide_approval` 接收 `request_id` 和 `decision`，决定只能是 `approve` 或 `reject`。人工 CLI 用 Runtime 的详情查询检查可见范围，向审批人展示确切的已确认参数。审批人明确确认决定后，CLI 才构造审批 `ToolCall` 并调用 `invoke()`。Runtime 在锁内重新检查访问范围。待审请求可以作出决定，重复提交返回当前状态，已作出的决定保持原值。
- 人工审批入口使用 CLI 为审批操作构造的可信上下文。`decide_approval` 只在该上下文的允许列表中，模型工具发现和模型工具调用上下文均无此能力。审批工具按工具名检查对应的审批权限，审批决定只引用请求编号，业务参数来自存储中的确认快照。
- `decide_approval` 使用 Runtime 的参数、权限与模式检查，再进入内部审批编排。审批编排负责状态写入与恢复执行。原业务工具的超时只包住实际 handler 调用，审批结果的落盘在该时限之外完成。

## 5. 批准后执行与审计

1. 拒绝时持锁保存审批人、决定时间、审批调用 ID 和 `REJECTED`，然后返回结果。
2. 批准时持锁保存审批决定及 `RUNNING`。只有该次写入成功才进入执行。Runtime 根据记录中的租户和发起人，从当前预设身份表重新构造发起人的上下文，再检查原工具、权限和参数摘要。发起人权限失效或已确认的参数无法验证时，记录执行失败。
3. Runtime 沿用原 `tool_call_id`，在原工具的业务锁内复检订单状态、可退金额或转出余额，再调用现有的限时执行、脱敏和结果归一化逻辑。恢复路径从已确认且已批准的记录进入这个内部执行阶段，原发起调用无需重新确认。审批人的上下文用于做审批决定，业务操作仍归属于原发起人。
4. 将执行结果写回同一 JSON，然后释放锁。普通退款可以记录 `APPROVED/SUCCEEDED`。现有大额转账超时例子记录 `APPROVED/UNKNOWN` 与 `TIMEOUT_UNKNOWN`。如果执行后的状态写入失败，保留先前落盘的 `RUNNING` 并返回“执行结果待核对”。下次有权审批的命令取得锁后将其标为 `UNKNOWN`。JSON 同时保留原调用 ID、审批调用 ID 和两个角色，便于不同 CLI 进程之间关联确认、审批和执行。运行时审计记录使用相同关联字段，并继续脱敏输出。

本地 JSON 持久化的是待办、决定和执行结果。现有 `ORDERS`、`REFUNDS`、`ACCOUNTS`、`TRANSFERS` 仍是进程内的模拟数据。审批命令打印当次执行结果和当次模拟业务状态，之后的列表命令从 JSON 查看历史。不同请求在独立 CLI 进程中的业务余额不共享。

## 6. 单个 `main()` 与例子

入口定义为无参数的 `async def main()`，由 `asyncio.run(main())` 启动，每次运行一个例子。`main()` 内部解析命令行，取得三个位置参数 `tenant_id`、`user_id`、`example` 和该例子所需的具名选项，再合并默认值。`--help` 列出例子名称及中文描述。入口校验身份、例子名和参数名，然后构造对应的 `ExecutionContext` 与 `ToolCall`。缺少必填选项或传入其他例子的选项时，CLI 提示用法并退出。参数值、权限与业务条件仍由 Runtime 和工具参数模型校验。退款金额按字符串传给现有模型，转账金额按数值传入。

| `example` | 工具 | 最小可传参数与默认值 | 预期行为 |
| --- | --- | --- | --- |
| `get-order` | `get_order` | `order_id` 默认 `ord_1001` | 查询并脱敏 |
| `refund-invalid` | `create_refund` | 固定数据，含额外参数 `approved` | `INVALID_ARGUMENT` |
| `refund-no-permission` | `create_refund` | 固定数据，该例移除发起人的退款权限 | `PERMISSION_DENIED` |
| `refund-plan` | `create_refund` | 固定数据和 `PLAN` 模式 | `PLAN_MODE_DENIED` |
| `refund-dont-ask` | `create_refund` | 固定数据和 `DONT_ASK` 模式 | `DONT_ASK_DENIED` |
| `shell-denied` | `run_shell` | 固定命令 `whoami` | `DENY_RULE`，危险命令匹配样例由对应测试覆盖 |
| `transfer-invalid` | `transfer` | 固定数据，含额外参数 `approved` | `INVALID_ARGUMENT` |
| `transfer-limit` | `transfer` | 固定金额 `60000` | `EXCEED_LIMIT` |
| `transfer-normal` | `transfer` | `from_account` 默认 `ACC-A-123456`，`to_account` 默认 `ACC-A-654321`，`amount` 默认 `1000` | 按默认金额确认后直接执行 |
| `refund-submit` | `create_refund` | `order_id` 默认 `ord_1001`，`amount` 默认 `399.00`，`reason` 默认“退款商品存在质量问题” | 确认后返回待审批编号 |
| `transfer-large` | `transfer` | `from_account` 默认 `ACC-A-123456`，`to_account` 默认 `ACC-A-888888`，`amount` 默认 `90000` | 按默认金额确认后返回待审批编号 |
| `approvals-list` | `list_approvals` | `status` 默认 `PENDING_APPROVAL` | 按当前身份查询 |
| `approval-decide` | `decide_approval` | `request_id`、`decision` 均必填 | 展示详情并确认决定，批准后尝试执行 |

### 6.1 失败与拒绝例子

以下例子也由同一个 `main()` 选择。权限演示使用本次调用的上下文副本移除对应权限，身份表保持真实的两类角色。固定失败参数由例子构造。参数和权限拒绝例子直接走 `runtime.invoke()`，由 Runtime 产生错误和审计。此类调用在权限校验前不展示请求详情。CLI 的未知身份、未知例子和必填选项缺失属于入口错误，输出说明并以非零状态退出。

| `example` | 身份与前提 | 工具和参数 | 预期行为 |
| --- | --- | --- | --- |
| `get-order-no-permission` | 发起人，本次移除 `order:read` | `get_order`，默认订单 | `PERMISSION_DENIED` |
| `get-order-invalid` | 发起人 | `get_order`，固定非法订单号 | `INVALID_ARGUMENT` |
| `get-order-not-found` | 发起人 | `get_order`，固定 `ord_9999` | 沿用当前 handler 的 `ValueError` 归一化，返回 `TOOL_ERROR` |
| `approval-wrong-role` | 发起人 `tenant_a/u_100` | `decide_approval`，必填 `request_id`，固定 `decision=approve` | 因角色的工具白名单缺少审批工具，返回 `TOOL_NOT_ALLOWED` |
| `approval-no-permission` | 审批人 `tenant_a/u_200`，本次移除 `approval:decide` | `decide_approval`，必填 `request_id`，固定批准 | `PERMISSION_DENIED` |
| `approval-no-business-permission` | 审批人，本次移除 `refund:approve` 和 `transfer:approve` | `decide_approval`，必填同租户他人的 `request_id`，固定批准 | `APPROVAL_NOT_ACCESSIBLE`，提示“请求不存在或不可访问” |
| `approval-cross-tenant` | `tenant_b/u_200`，预设为 tenant_b 的审批人 | `decide_approval`，必填 tenant_a 请求编号，固定批准 | `APPROVAL_NOT_ACCESSIBLE` |
| `approval-not-found` | 审批人 | `decide_approval`，必填格式正确且不存在的 `request_id`，固定批准 | `APPROVAL_NOT_ACCESSIBLE` |
| `approval-invalid` | 审批人 | `decide_approval`，固定非法编号及非法决定 | `INVALID_ARGUMENT` |
| `approvals-list-no-permission` | 任一已知身份，本次移除 `approval:list` | `list_approvals`，默认待审批 | `PERMISSION_DENIED` |
| `approvals-list-invalid` | 任一已知身份 | `list_approvals`，固定非法 `status` | `INVALID_ARGUMENT` |
| `refund-wrong-role` | 审批人 | `create_refund`，使用示例默认参数 | `TOOL_NOT_ALLOWED` |
| `refund-exceed-amount` | 发起人 | `create_refund`，默认订单 `ord_1001`，固定金额 `400.00` | `BUSINESS_RULE_DENIED` |
| `refund-order-not-found` | 发起人 | `create_refund`，固定格式合法但不存在的 `ord_9999` | `BUSINESS_RULE_DENIED` |
| `transfer-wrong-role` | 审批人 | `transfer`，普通转账默认参数 | `TOOL_NOT_ALLOWED` |
| `transfer-no-permission` | 发起人，本次移除 `transfer:create` | `transfer`，普通转账默认参数 | `PERMISSION_DENIED` |
| `transfer-insufficient-balance` | 发起人 | `transfer`，从 `ACC-A-654321` 转出 `6000` 到 `ACC-A-123456` | `INSUFFICIENT_BALANCE` |
| `transfer-account-not-found` | 发起人 | `transfer`，默认转出账户，转入 `ACC-A-000000`，金额 `1000` | `ACCOUNT_NOT_FOUND` |
| `transfer-cross-tenant` | tenant_a 发起人 | `transfer`，默认转出账户，转入 `ACC-B-111111`，金额 `1000` | `ACCOUNT_NOT_FOUND` |

同一入口还覆盖以下交互和状态结果，无须新增例子名称。退款和转账的提交例子在确认时输入 `n`，得到 `CONFIRMATION_REJECTED`，输入 EOF 或等待超时得到 `CONFIRMATION_UNAVAILABLE`。审批例子选择 `--decision reject` 并明确确认，保存 `REJECTED`。审批人取消提交决定时，CLI 输出取消，JSON 保持待审。对已处理请求再次运行 `approval-decide`，返回原状态和结果，业务执行次数保持一次。

审批列表正常查询也要覆盖空列表、发起人本人记录、审批人同租户他人记录、跨租户记录被过滤和历史记录筛选。无可见记录时返回成功的空列表。自审场景作为自动化测试，在受控验收数据中使用“用户曾发起请求，当前已转为审批角色”的同一用户，验证 `APPROVAL_NOT_ACCESSIBLE`，以独立验证自审检查。该角色变更场景由测试 fixture 构造。危险 Shell 命令匹配也由自动化测试覆盖，CLI 的 `shell-denied` 演示禁用规则。演示身份表增加 tenant_b 的同名用户作为跨租户身份，角色仍只有发起人和审批人。

每个拒绝例子都核对对应调用的终态审计、业务写入次数为零，以及既有审批 JSON 无变化。输入未知例子、未知身份和缺少必填选项时，调用尚未进入 Runtime，按 CLI 入口错误验收。审批拒绝是有效决定，单独核对 JSON 从待审变为拒绝。

覆盖转账金额时，由实际金额决定业务预检和审批条件。在 `homework` 目录用已安装项目依赖的 Python 运行。第三条命令中的 `REQUEST_ID` 取自第一条命令的输出。

```bash
python tool_governance_demo.py tenant_a u_100 transfer-large --amount 85000
python tool_governance_demo.py tenant_a u_200 approvals-list
python tool_governance_demo.py tenant_a u_200 approval-decide --request-id REQUEST_ID --decision approve
python tool_governance_demo.py tenant_a u_100 approvals-list --status ALL
```

## 7. 实施顺序与验收

- [ ] 实现身份权限和确认后写入。通过 `runtime.invoke()` 验证退款与大额转账落盘、无提前写入，普通转账确认后直接执行，确认拒绝或超时不落盘。验证相同调用 ID 重复提交及参数变化。
- [ ] 实现查询与审批决定。测试发起人只看本人、审批人只看有审批权限的同租户他人记录。覆盖默认待审筛选、历史状态、猜测请求 ID、自审、跨租户和权限缺失。
- [ ] 接通批准后的执行。验证退款成功、大额转账超时、发起人权限撤销、重复批准以及两条 CLI 命令竞争同一请求。在同一 Runtime 测试中于确认和审批之间修改模拟余额或订单，验证锁内复检。模拟执行前及执行后落盘失败，另用临时文件模拟遗留的 `RUNNING`，验证状态恢复。
- [ ] 合并 CLI 入口并迁移例子。用不同进程运行提交、列表、批准和历史查询，对照 JSON 和审计核对两个调用 ID、发起人与审批人、审批状态和执行状态。检查原参数、权限、Shell 拦截和脱敏演示，最后运行 `homework` 全量测试。
- [ ] 逐项运行第 6.1 节失败例子，并验证确认拒绝、确认超时、审批拒绝、重复决定和查询为空。将同次调用等待审批的旧测试迁移为两次 `invoke()`，核对错误码、记录可见性、JSON 状态及零副作用。
