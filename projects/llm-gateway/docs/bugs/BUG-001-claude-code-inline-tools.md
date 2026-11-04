# BUG-001：Claude Code 多文件读取后返回工具协议 422

> 状态：兼容调用修复已验证，三文件回归与真实 F1 完整审查均成功。此问题属于审查工具调用链，不是 Gateway 业务代码故障。

## 现象与定位

Claude Code 2.1.311 普通对话可完成，但原生多文件只读调用在 Read 设计、测试方案和依赖文件后返回 `422 ... unknown variant tool_definition, expected tool_reference`，退出码 1、is_error=true，没有完整审查结论。问题发生在工具结果之后的下一次请求，不是 Read 权限拒绝，也不是 F1 代码执行错误。

本机 Claude Code 安装文件包含 `CLAUDE_CODE_INLINE_TOOLS` 和 `inline-tools-2026-09-15` 标识；[Claude Code 上游问题记录](https://github.com/anthropics/claude-code/issues/95087)报告同类消息结构兼容问题及关闭 inline tools 的方式。不能把 `tool_definition` 简单改名为 `tool_reference`，它们承载的工具定义信息不同。

## 单变量复现与修复

保持现有模型、连接和权限不变；准备三份临时文件：要求 `subtract(7,2)==5` 的契约、错误实现 `return a+b`、对应断言。要求 Claude Code 顺序执行三次 Read，再给出缺陷位置和最终结论。

| 对照 | 进程变量 | 实际结果 |
| --- | --- | --- |
| 故障复现 | `CLAUDE_CODE_INLINE_TOOLS=true` | 完成 3 次 Read 后返回相同 422，退出码 1，is_error=true |
| 最小修复 | `CLAUDE_CODE_INLINE_TOOLS=false` | 完成 3 次 Read，正确指出 subtract.py:2 应为减法，实际 9、期望 5；最终包含 REVIEW_COMPLETE，退出码 0，is_error=false，stop_reason=end_turn |

其余命令参数相同：`claude --print --tools Read --allowedTools Read --no-session-persistence --output-format stream-json --verbose --max-turns 8`，prompt 通过 stdin 传入。修复只作用于调用进程，不关闭整个工具读取能力，不修改持久模型或认证配置。

## 验证边界与交接

- 此对照验证多次工具读取后能继续调用并形成代码审查结论，不能仅用进程启动或 Read 成功代替。
- 前后 SHA-256 对照：F1 源码、用户/项目 `settings.json` 和 `settings.local.json` 无差异；Claude Code 运行期间 `~/.claude.json` 的哈希变化，未将其笼统称为全部配置不变，也未直接改写或回滚该文件。不读取或输出凭据内容。
- 上游协议缺陷仍存在，本项目通过兼容调用规避；后续 Claude Code 升级时可以重新验证是否仍需要此变量。
- 真实 F1 复测：Claude Code 直接调用读取 19 个文件，包含全部 16 个源码/测试文件及设计、测试方案、依赖声明；正常退出 0、is_error=false、stop_reason=end_turn，最终 APPROVED，P0/P1/P2 为 0、P3 为 5。源码/测试前后 SHA-256 一致；原始结论见 [F1 审查报告](../features/F1/review.md)，非阻断建议的处理见 [F1 测试结论](../features/F1/test-plan.md)。
