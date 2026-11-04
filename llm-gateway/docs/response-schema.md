# response_schema 完整规则（F4）

> 状态：F4 已验收通过，完整规则已实现并经功能验证与独立审查。
>
> 规则标识：`response-schema-v1`。本文供调用方接入使用；实现设计见 [F4 设计](features/F4/design.md)，验收见 [测试方案](features/F4/test-plan.md)。这些平台限制不代表 JSON Schema 标准或所有供应商的限制。

## 1. 入口与职责

`POST /v1/llm` 可选提供 `response_schema`。省略时为普通文本；提供时，只有上游完整完成、JSON 解析通过、Schema 校验通过，才返回结构化成功结果。

调用方仍选择逻辑模型和受控 Prompt 模板，不提交 `response_format`、`text.format`、协议、供应商地址或密钥。F4 结构化模板为 `context_answer@1.2.0`；旧模板保持文本能力。模型或模板不支持时返回 `unsupported_capability`，不静默改用其他模式。

Gateway 验证输出协议，不验证订单事实、退款资格、业务授权或实际操作。Schema 中的字段名、说明和默认值均不能授予权限。

F4 不提供规则查询接口。开发者阅读本文及接口文档；运行时能力发现留到 1.7 与模型管理一起讨论。F5 的流式入口不支持 Schema。

## 2. 请求字段与根类型

| 输入 | 结果 |
| --- | --- |
| 省略 `response_schema` | 普通文本调用 |
| `null`、字符串、数组、布尔值 | `422 / invalid_request` |
| 空对象 `{}` | `400 / invalid_response_schema` |
| 合法且属于平台子集的 Schema 对象 | 继续能力、装配与预算检查 |
| 标准合法但不属于平台子集的 Schema | `400 / invalid_response_schema`，不忽略不支持项 |

根必须显式声明 `"type":"object"`，不能用根 `$ref` 或根 `anyOf` 代替。输出不支持数组、字符串等其他根类型；对象内部可以有多层对象、数组和标量。

可选的 `$schema` 只接受 `https://json-schema.org/draft/2020-12/schema`。未提供时仍按 Draft 2020-12 的本平台子集解释。

## 3. 支持的关键字

关键字必须位于合法的 Schema 位置，取值类型遵守 Draft 2020-12。字段名位于 `properties` 内，不因与关键字同名而被拒绝；`description`、`default` 等数据内容不能被误当作嵌套 Schema。

| 分类 | 支持内容 | 边界 |
| --- | --- | --- |
| 类型 | `type` | 单个 `object/array/string/integer/number/boolean/null`；不接受类型数组 |
| 对象 | `properties`、`required`、`additionalProperties` | `additionalProperties` 只接受布尔值，不接受 Schema 对象 |
| 数组 | `items`、`minItems`、`maxItems` | `items` 为单个 Schema 对象；不支持元组和 `prefixItems` |
| 字符串 | `minLength`、`maxLength` | 按字符长度，不是 UTF-8 字节数或 Token 数 |
| 数值 | `minimum`、`maximum`、`exclusiveMinimum`、`exclusiveMaximum` | 有限 JSON 数值；不以 Python 布尔值冒充数值 |
| 值约束 | `enum`、`const` | 按 JSON Schema 语义比较，不把字符串转换成数字 |
| 定义与引用 | `$defs`、`$ref` | 仅文档内引用，见下一节 |
| 可空 | `anyOf` | 仅两个分支：一个恰为 `{"type":"null"}`，另一个为受支持的非空结构或其引用 |
| 注解 | `title`、`description`、`default` | 标题/说明为字符串；默认值为 JSON 数据，只作注解 |
| 草案声明 | `$schema` | 根节点可选，URI 固定为上节所列值 |

Schema 校验位置不接受布尔 Schema，例如 `properties.note: true`。`additionalProperties: false` 是受支持关键字的布尔参数，不属于布尔 Schema。

首版不支持一般联合类型、`oneOf/allOf/not`、条件关键字、`pattern/format`、`patternProperties`、依赖、`$id/$anchor`、动态引用、远程引用和其他未列出的关键字。不得静默删除这些字段后继续调用。

`required` 决定字段必须出现；允许 `null` 决定字段值能否为空；`default` 不会填补缺失字段。未声明 `additionalProperties` 时，遵守标准的允许额外字段语义；Gateway 不自动补成 `false`。Schema 即使合法也可能没有可满足的输出，Gateway 不承诺预先判断所有约束组合是否有解。

## 4. 引用与完整检查

`$ref` 只接受当前文档内的 JSON Pointer，且目标必须是有效 Schema 节点。例如 `#/$defs/Product`。正确处理 JSON Pointer 的 `~0`、`~1` 转义，不把普通字段数据误当成 Schema。

- 禁止 HTTP(S)、文件地址、其他文档和锚点引用；预检与校验都不访问网络或文件解析引用。
- 缺失目标、非法目标、自引用、间接循环引用均拒绝；重复使用同一个无循环定义允许。
- `$ref` 同级只允许 `title`、`description`、`default`，不接受叠加其他约束。
- 未被使用的 `$defs` 也完整检查，包括关键字、引用、循环和资源限制，不能隐藏非法定义。
- 不展开后再发给 Provider；保持 `$defs/$ref`。解析引用只用于预检、复杂度计算和本地验收。

## 5. 资源限制

| 项目 | 上限 | 口径 |
| --- | --- | --- |
| Schema 大小 | 16 KiB，即 16,384 字节 | 第 6 节规范化 JSON 的 UTF-8 字节数 |
| 原始 JSON 容器深度 | 32 | 根对象为 1；所有对象、数组都计层，含定义、注解和枚举中的容器 |
| 业务结构嵌套 | 8 | 根对象为 1，嵌套对象/数组加 1；标量、Schema 包装和可空分支不额外加层；引用按目标结构计算 |
| 原始 Schema 节点数 | 256 | 每个 Schema 校验位置计一个，含根、属性、items、anyOf 分支、定义；不是 JSON 字段数 |
| 引用展开后 Schema 节点数 | 256 | 每个引用使用位置按目标重新计算，不能仅按不同定义去重 |

恰好达到上限允许，超过任一上限返回 `invalid_response_schema`。循环和巨大展开应在有限遍历中被拒绝，不实际构造巨型展开文档。结构深度也检查独立定义；引用到字段时再按该字段所在路径计算深度。

节点计算示例：根对象含两个属性，两个属性均引用 `$defs.Address`；Address 是一个对象，内含一个字符串属性。原始节点数为 5：根 1、两个引用位置 2、定义中的对象和字符串 2。展开计数为 7：根 1、定义声明 2、两个使用位置各 2。引用位置由目标占据，不再重复增加一个引用包装节点；实际传输仍保留引用。

这些限制不替代模型窗口检查。最终 User 仍最多 20,000 字符，`context` 仍最多 10,000 字符；Chat 路径把 Schema 放进 User，可能先触发 User 长度限制，Responses 原生 Schema 不重复进入 User。

## 6. 规范化与指纹

规范化使用如下规则，不更改 Schema 业务内容：

```python
from utils.json_codec import dumps
canonical = dumps(schema, sort_keys=True)
```

普通 JSON 值的结果等同于原先约定的 Unicode/递归键排序/紧凑 `json.dumps`；用户确认十进制保真后，额外支持 Decimal 数值，不转为浮点或数字字符串。不同数值表示不承诺全部得到同一指纹。

对象键递归排序，中文保留原文，不添加多余空白；数组顺序、字符串内部空白、注解、默认值、定义和引用保留。不转换类型，不补默认值，不展开引用。

大小检查和 `schema_fingerprint` 使用同一份规范化 UTF-8 字节，指纹算法为 SHA-256；装配 User、原生 Schema 参数和计数策略也使用对应的同一份 Schema。只消除对象键顺序与输入排版差异，不识别全部语义等价形式。

`prompt_fingerprint` 继续只标识最终 messages。Responses 的原生 Schema 不在 messages 中，因此需独立记录 Schema 指纹与输出模式。没有 Schema 的请求不产生 Schema 指纹。

计 Token 时遵守模型计数策略的完整编码，不把紧凑 JSON 的独立 Token 数简单加到消息计数上。Chat 的 Schema 在 User 中计一次，同时还须计入 API json_object 格式标志；Responses 则额外计算原生 Schema 本体。本地预检计数不是供应商实际 Usage。

Schema 和输出中的有限小数/指数按精确十进制处理，例如 minimum=1.0000000000000001 时，输出1不能通过。精度保留到 Schema 规范化、API出站、完整输入计数和 HTTP parsed；default/enum/const 也适用。调用方如需同等精度，应使用精确十进制方式解析返回的 JSON 数字。实现没有修改 Python 或第三方校验器的全局设置。

## 7. 完整请求示例

以下为F4可用的请求示例（需模型与模板具备能力）：

```json
{
  "model": "general-primary",
  "prompt": {
    "name": "context_answer",
    "version": "1.2.0",
    "variables": {
      "context": "订单 A123 的商品为充电器，数量为 1。资料未提供备注。"
    }
  },
  "messages": [{"role": "user", "content": "提取订单号、商品和备注；备注未知时用 null。"}],
  "max_output_tokens": 128,
  "timeout_seconds": 30,
  "response_schema": {
    "type": "object",
    "properties": {
      "order_id": {"type": "string"},
      "product": {
        "type": "object",
        "properties": {
          "name": {"type": "string"},
          "quantity": {"type": "integer", "minimum": 1}
        },
        "required": ["name", "quantity"],
        "additionalProperties": false
      },
      "note": {"anyOf": [{"type": "string"}, {"type": "null"}]}
    },
    "required": ["order_id", "product", "note"],
    "additionalProperties": false
  }
}
```

嵌套商品还可提取到 `$defs.Product`，并把 product 节点替换为 `{"$ref":"#/$defs/Product"}`。Schema 中的固定值应由调用方按业务需要设计；本例不把订单 A123 写成平台内置规则。

Pydantic 的普通模型导出的 `.model_json_schema()` 可以作为输入来源，但仍须符合上述子集与资源限制；复杂联合、格式、递归模型等不能因来自 Pydantic 就绕过预检。

## 8. 返回与错误

结构化成功的 `data` 精确包含 `text`、`parsed`、`usage`：

```json
{
  "success": true,
  "code": "ok",
  "data": {
    "text": "{\"order_id\":\"A123\",\"product\":{\"name\":\"充电器\",\"quantity\":1},\"note\":null}",
    "parsed": {
      "order_id": "A123",
      "product": {"name": "充电器", "quantity": 1},
      "note": null
    },
    "usage": null
  },
  "message": "成功"
}
```

`text` 是适配器取得的原始回答文本，不重排、不用 parsed 再序列化替换；`parsed` 是通过验收的对象。Usage 只有真实取得并合法时才返回，否则为 null。普通文本的 data 保持只有 text/usage，不返回 parsed:null。请求 ID 仍只在 `X-Request-ID` 响应头。

| 阶段 / 条件 | HTTP / code | Provider 调用次数 |
| --- | --- | --- |
| Schema 字段类型错误 | `422 / invalid_request` | 0 |
| Schema 内容、支持范围、引用、资源限制错误 | `400 / invalid_response_schema` | 0 |
| 模板或模型没有所需能力 | `400 / unsupported_capability` | 0 |
| 最终 User 过长、模型预算不满足 | 原有 `prompt_too_long`、`invalid_output_budget`、`context_length_exceeded` 等 | 0 |
| 上游完整文本不是严格 JSON | `502 / invalid_json` | 1 |
| JSON 合法但不符合 Schema，含根不是对象 | `502 / schema_validation_failed` | 1 |
| 上游因输出预算耗尽而未完成 | `502 / output_truncated` | 1 |
| 上游明确拒答 / 内容过滤 | `502 / provider_refused` 或 `provider_content_filtered` | 1 |
| 非法上游响应或空白正文 | `502 / provider_invalid_response` | 1 |

严格 JSON 只接受一个值，允许外围 JSON 空白，不修复、不提取代码围栏、不忽略前后说明；拒绝重复对象键、NaN、Infinity 和尾随内容。JSON 合法的数组、标量、null 属于根结构不符，报 Schema 错。

先验收上游状态和有效正文，再解析 JSON；长度截断优先报 `output_truncated`。错误始终使用四字段信封、`data=null`，不返回半成品 parsed、上游正文或校验异常原文。F4 不进行纠错、重试、fallback 或缓存降级。

## 9. 依据与证据边界

标准语义参考 [JSON Schema Core 2020-12](https://json-schema.org/draft/2020-12/json-schema-core) 和 [Validation 2020-12](https://json-schema.org/draft/2020-12/json-schema-validation)。具体支持范围以本文平台子集为准，不能把完整标准或供应商全部能力当作本项目已支持。

真实 API 路径与计数探测依据见 [F4 设计的外部依据](features/F4/design.md#37-外部依据与技术探测)。规则文档已形成不代表代码、全部关键字组合、真实接入或安全语义已通过验收。
