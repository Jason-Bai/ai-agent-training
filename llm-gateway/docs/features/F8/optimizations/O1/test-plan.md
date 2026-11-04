# F8-O1 测试方案

测试方案已由用户确认。

| 场景 | 验收依据 |
| --- | --- |
| 默认普通调用 | 用订单 A123 经普通 HTTP 入口调用默认 Mock，响应、PostgreSQL Attempt、按 request_id 查询的 known_subtotal 均为输入 10、输出 1、合计 11，unknown_attempts 为空。使用与 10 不同的本地输入预估，验证两者独立。 |
| 默认流式调用 | 创建、首次订阅直至完成。增量中无 usage，唯一 completed 事件、Attempt 和 Trace 用量一致。重放终态后用量仍只累计一次。 |
| 显式结果 | 普通结果与流式终态分别覆盖自定义值和缺失值。自定义值原样入账到 Trace；缺失时单次尝试为 null、known_subtotal 为 null、unknown_attempts 为 [1]。 |
| 失败与混合尝试 | 无用量的失败不补默认值。一次失败缺失用量、一次默认 Mock 成功时，Trace 仅合计成功尝试并列出缺失的尝试编号。 |
| 基线兼容 | 逐条区分历史断言意图。检查默认正常路径的断言改为模拟样本，专测缺失的用例改成显式缺失。检查健康探测与普通结果的关联、历史缺失行的读取，保持真实适配器行为。 |

先运行目标测试记录 Red，再最小实现并用相同场景验证 Green。最终串行运行完整套件（含 --run-live），要求无失败、无跳过、无筛除并完成独立审查。真实调用和 Mock 结果分别记录。

## 执行记录

- 新增集成用例在旧实现上 3 failed / 4 passed。默认普通、默认流式与未知失败后默认备用成功均因上报缺失失败。修改后相同 7 例通过。原始回执见 [Red](evidence-red.txt) 与 [Green](evidence-green.txt)。
- 全量初跑暴露旧隐私测试直接 JSON 编码 Usage 时失败，定位后中断（326 passed / 1 failed）。两个隐私测试改为编码完整 Trace 后检查明文标记，单独复测 2 passed。第二次全量 1910 passed / 1 failed，唯一失败是子进程冒烟测试的快照写入同样需要编码 Usage。修正测试快照，再执行全量。
- 子进程冒烟原场景复测 1 passed。最终在 Python 3.12 以 `uv run --python 3.12 python -W error -m pytest --run-live -o junit_family=xunit1 --junitxml=… -ra -q` 不加筛选执行，1911 passed / 572.44s，0 failed、0 error、0 skipped、0 deselected。其中 16 项真实接入用例，Chat / Responses 使用同一真实供应商。见[原始 JUnit 回执](evidence-all-live.xml)。
- [第 1 轮独立审查](review-r1.md) APPROVED。审查前后 155 项安全清单及真实差分一致。P0–P2 均为零。P3 为本项测试在预估值两侧都缺失时仍可能通过。执行者通过临时 pytest 插件在 observe 返回后额外断言已保存值为正整数且不等于 10，复跑本项 7 例均取得 236，见[运行回执](evidence-estimate.txt)。当前行为已经核实；本项永久测试的该条断言仍有此局限，保留为非阻断测试改进。验收通过。
