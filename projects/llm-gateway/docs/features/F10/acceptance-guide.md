# F10 本机操作与验收手册

自动验收使用独立子进程、临时配置和测试专属 PostgreSQL；本机开发配置由维护者自行管理。以下手动命令从项目目录运行，已有账户使用新的账户名或复用原凭据。首次执行数据库迁移前确认连接的是预期的开发库。

## 1. 准备与启动

- 真实凭据在 config/credentials.yaml 中由本机维护者保存。
- 如需手动验收真实模型，由维护者将开发配置的 general-primary 指向 DeepSeekProvider/Chat，general-backup 指向 OpenAICompatibleProvider/Responses，model_id 均为 deepseek-flash。自动用例使用临时配置，不改动本机当前的Mock绑定。
- config/prices.yaml 的绑定名必须与 gateway.yaml 一致，并维护适用的价格版本。
- PostgreSQL 密码按 README 保存在本地文件。迁移和 Gateway 启动都从该配置加载。

~~~bash
docker compose up -d --wait
uv run --python 3.12 alembic upgrade head
uv run --python 3.12 uvicorn --app-dir src main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
~~~

在另一个终端进入项目目录，签发凭据并拨款。完整凭据只保存到本地变量，人工验收记录只保留操作成功状态和请求 ID。

~~~bash
export PYTHONPATH=src
uv run --python 3.12 python -m admin create-agent acceptance-a 验收Agent
GATEWAY_AGENT_TOKEN=$(uv run --python 3.12 python -m admin issue acceptance-a)
uv run --python 3.12 python -m admin deposit acceptance-a 10 --operation-id acceptance-a-initial
~~~

同一个拨款操作号及相同内容重复执行，累计拨款仍为10。后续新增拨款使用新的操作号。

## 2. 普通请求与核查

~~~bash
curl -i -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}"   -H 'Content-Type: application/json' http://127.0.0.1:8000/v1/llm   -d '{"model":"general-primary","prompt":{"name":"basic_text","version":"1.0.0"},"messages":[{"role":"user","content":"订单 A123 的商品是充电器。请简短复述。"}],"max_output_tokens":128,"timeout_seconds":30}'
~~~

从 X-Request-ID 取得本次 REQUEST_ID，检查生成状态、尝试列表和账目。响应里的供应商实报 Usage、本地输入估算和供应商费用各有独立含义。

~~~bash
curl -sS -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}" "http://127.0.0.1:8000/v1/traces/${REQUEST_ID}"
curl -sS -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}" "http://127.0.0.1:8000/v1/billing/requests/${REQUEST_ID}"
curl -sS -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}" http://127.0.0.1:8000/v1/billing/summary
~~~

替换请求逻辑模型为 general-backup，可以验证 Responses 接入。两种协议都由 Gateway 统一处理。

结构化请求可使用以下同一订单输入，检查 parsed 的 order_id 和 product：

~~~bash
curl -i -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}" \
  -H 'Content-Type: application/json' http://127.0.0.1:8000/v1/llm \
  -d '{"model":"general-primary","prompt":{"name":"context_answer","version":"1.2.0","variables":{"context":"订单 A123 的商品是充电器。"}},"messages":[{"role":"user","content":"提取订单号和商品，返回JSON。"}],"max_output_tokens":128,"timeout_seconds":30,"response_schema":{"type":"object","properties":{"order_id":{"type":"string"},"product":{"type":"string"}},"required":["order_id","product"],"additionalProperties":false}}'
~~~

## 3. 创建、订阅、续传与取消

~~~bash
curl -sS -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}"   -H 'Content-Type: application/json' http://127.0.0.1:8000/v1/llm/stream   -d '{"model":"general-primary","prompt":{"name":"basic_text","version":"1.0.0"},"messages":[{"role":"user","content":"订单 A123 的商品是充电器。请简短复述。"}],"max_output_tokens":128,"timeout_seconds":30}'
~~~

取 data.request_id 更新 REQUEST_ID。首次订阅前应为 pending，订阅才启动生成。

~~~bash
curl -N -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}" "http://127.0.0.1:8000/v1/llm/stream/${REQUEST_ID}/events"
~~~

收到一段后断开，记住最后成功处理的 id 为 LAST_SEQ，续传同一请求。已完成且没有未读事件时返回204。

~~~bash
curl -N -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}" "http://127.0.0.1:8000/v1/llm/stream/${REQUEST_ID}/events?after_seq=${LAST_SEQ}"
curl -sS -X POST -H "Authorization: Bearer ${GATEWAY_AGENT_TOKEN}" "http://127.0.0.1:8000/v1/llm/stream/${REQUEST_ID}/cancel"
~~~

取消接口202表示收口中，重复调用直到200返回canceled、failed或completed。请求可能先自然结束，按实际终态及账务证据核查。续传和重复取消都不新增模型尝试。

## 4. 人工处理

待核实记录按 request_id 和 attempt_no 定位。由管理员取得证据后使用 CLI，原因和操作号应能关联证据。以下采用占位值，按实际事实填写。

~~~bash
uv run --python 3.12 python -m admin resolve "${REQUEST_ID}" 1 settle --amount 0.008 --reason "已核实的证据编号" --operation-id "独立核费操作号"
~~~

确认未收费使用 zero；由平台承担使用 platform，省略amount。操作后重新查看单笔账务和总览，重复同一操作号不会二次扣款。平台承担后的供应商实际成本可以后续单独核实。

## 5. 自动验收与报告

~~~bash
uv run --python 3.12 python -W error -m pytest --run-live tests/live/test_f10_acceptance.py -q
uv run --python 3.12 python -W error -m pytest --run-live tests/live/test_f10_capacity.py -q
uv run --python 3.12 python -W error -m pytest --run-live -ra -q
~~~

容量场景含四个60秒派发窗口和600秒持续派发，还需等待在途请求收口。真实模型速度和故障影响实际耗时。最终全量包含该场景。报告以实际执行回执为准，真实供应商正式账单的核对状态单列。
