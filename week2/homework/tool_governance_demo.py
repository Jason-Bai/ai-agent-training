from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class StrictArgs(BaseModel):
    # 所有工具参数的共同边界：拒绝未声明字段，并清理字符串首尾空白。
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GetOrderArgs(StrictArgs):
    # 查询订单工具的参数契约：仅接受格式受限的订单标识。
    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")


class CreateRefundArgs(StrictArgs):
    # 创建退款工具的参数契约：约束订单、金额和原因，防止非法写入。
    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    reason: str = Field(min_length=4, max_length=200)


class TransferArgs(StrictArgs):
    from_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    to_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    amount: float = Field(strict=True, gt=0, le=100000, allow_inf_nan=False)


class SearchOrdersArgs(StrictArgs):
    # 订单检索工具的参数契约：限制可查询状态和分页范围。
    status: Literal["paid", "shipped", "refunded"]
    limit: int = Field(default=20, ge=1, le=50)


class RunShellArgs(StrictArgs):
    command: str = Field(min_length=1, max_length=200)


class Effect(StrEnum):
    # 工具副作用分类：用于区分读写执行、并发控制和重试边界。
    READ = "read"
    WRITE = "write"
    SHELL = "shell"


class Risk(StrEnum):
    # 工具风险分级：为审批与审计策略提供风险依据。
    LOW = "low"
    HIGH = "high"


class PermissionDecision(StrEnum):
    # 授权决策结果：表达允许、拒绝和需人工确认三种治理状态。
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


class PermissionMode(StrEnum):
    # 执行模式只能由服务端设置，模型参数无权变更。
    DEFAULT = "default"
    PLAN = "plan"
    BYPASS_PERMISSIONS = "bypassPermissions"
    DONT_ASK = "dontAsk"


@dataclass(frozen=True)
class ToolCall:
    # Harness 传入的原始调用请求，承载调用标识、工具名和模型生成的参数。
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionContext:
    # 每次调用携带的可信身份与授权上下文，不接受模型自行声明权限。
    trace_id: str
    user_id: str
    tenant_id: str
    permissions: frozenset[str]
    allowed_tools: frozenset[str]
    idempotency_key: str | None = None
    mode: PermissionMode = PermissionMode.DEFAULT
    role: Literal["initiator", "approver"] = "initiator"


@dataclass(frozen=True)
class ToolPolicy:
    # 工具治理策略：集中声明副作用、风险、权限、审批、超时与重试规则。
    effect: Effect
    risk: Risk
    permission: str
    denied: bool = False
    requires_confirmation: bool = False
    requires_approval: bool | Callable[[StrictArgs], bool] = False
    timeout_seconds: float = 3.0
    max_retries: int = 0
    idempotent: bool = False
    enabled: bool = True
    approval_permission: str | None = None


Handler = Callable[[str, StrictArgs, ExecutionContext],
                   Awaitable[dict[str, Any]]]
# 预检必须只读，允许在等待用户或资源锁之后重复执行。
Precheck = Callable[[StrictArgs, ExecutionContext], Awaitable[None]]
CanonicalTarget = Callable[[StrictArgs, ExecutionContext], str]
ConfirmationPrompt = Callable[[str], Awaitable[bool | None]]
CONFIRMATION_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    # 工具注册元数据：将模型契约、治理策略和实际处理器绑定为受控能力。
    name: str
    description: str
    parameters_model: type[StrictArgs]
    policy: ToolPolicy
    handler: Handler | None
    canonical_target: CanonicalTarget
    precheck: Precheck | None = None
    governance_action: Literal["list", "decide"] | None = None

    def to_model_tool(self) -> dict[str, Any]:
        # 仅导出公开描述和参数 Schema，供模型发现可调用的受控工具。
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_model.model_json_schema(),
            },
        }


@dataclass(frozen=True)
class PreparedCall:
    # 已通过调用前治理的内部对象，携带解析后的参数与授权结论。
    call: ToolCall
    tool: ToolDefinition
    args: StrictArgs
    decision: PermissionDecision


@dataclass(frozen=True)
class ToolResult:
    # 返回给 Harness 的安全执行结果，统一表达成功内容或标准化错误码。
    tool_call_id: str
    tool_name: str
    ok: bool
    content: Any
    error_code: str | None = None

    def to_model_payload(self) -> dict[str, Any]:
        # 工具结果是供模型参考的不可信数据，不能成为新的控制指令。
        return {
            "source": "tool",
            "tool_name": self.tool_name,
            "untrusted": True,
            "ok": self.ok,
            "error_code": self.error_code,
            "content": project_for_model(self.content),
        }


class PolicyDenied(Exception):
    # 策略拒绝异常：中断不满足白名单、参数、权限、预检或审批条件的调用。
    def __init__(self, code: str, message: str, content: Any | None = None):
        super().__init__(message)
        self.code = code
        self.content = content if content is not None else {"message": message}


class TransientToolError(Exception):
    # 瞬态依赖故障标记：驱动符合幂等边界的退避重试。
    pass


def _stable_value(value: StrictArgs | Mapping[str, Any] | Any) -> Any:
    # 递归规范化参数，消除映射顺序和 Decimal 表示差异以生成稳定摘要。
    if isinstance(value, BaseModel):
        return _stable_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _stable_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _approval_digest(tool_name: str, arguments: StrictArgs | Mapping[str, Any]) -> str:
    # 将工具名和规范化参数绑定；审批不能复用于不同金额、订单或原因。
    canonical = json.dumps(
        _stable_value(arguments),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


@dataclass
class Confirmation:
    # 消费者确认绑定当前调用，不能复用于另一笔调用或已变化的参数。
    confirmation_id: str
    call_id: str
    user_id: str
    tenant_id: str
    tool_name: str
    digest: str
    expires_at: float
    used: bool = False


class ConfirmationStore:
    def __init__(self) -> None:
        self._items: dict[str, Confirmation] = {}

    def approve(self, prepared: PreparedCall, ctx: ExecutionContext) -> str:
        confirmation_id = secrets.token_urlsafe(24)
        self._items[confirmation_id] = Confirmation(
            confirmation_id=confirmation_id,
            call_id=prepared.call.id,
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            tool_name=prepared.tool.name,
            digest=_approval_digest(prepared.tool.name, prepared.args),
            expires_at=time.monotonic() + 300,
        )
        return confirmation_id

    def consume(self, confirmation_id: str, prepared: PreparedCall, ctx: ExecutionContext) -> None:
        confirmation = self._items.get(confirmation_id)
        valid = (
            confirmation is not None
            and not confirmation.used
            and confirmation.expires_at >= time.monotonic()
            and confirmation.call_id == prepared.call.id
            and confirmation.user_id == ctx.user_id
            and confirmation.tenant_id == ctx.tenant_id
            and confirmation.tool_name == prepared.tool.name
            and confirmation.digest == _approval_digest(prepared.tool.name, prepared.args)
        )
        if not valid:
            raise PolicyDenied("CONFIRMATION_INVALID", "消费者确认已失效或不属于本次调用")
        confirmation.used = True


DEFAULT_STORE_DIR = Path(__file__).resolve().parent / "store" / "approvals"
REQUEST_ID_PATTERN = r"^apr_[0-9a-f]{64}$"


class ListApprovalsArgs(StrictArgs):
    status: Literal["PENDING_APPROVAL", "APPROVED", "REJECTED", "ALL"] = "PENDING_APPROVAL"


class DecideApprovalArgs(StrictArgs):
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    decision: Literal["approve", "reject"]


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    tool_call_id: str
    tool_name: str
    tenant_id: str
    initiator_id: str
    arguments: dict[str, Any]
    args_digest: str
    confirmation_id: str | None
    confirmed_at: float
    approval_status: Literal["PENDING_APPROVAL", "APPROVED", "REJECTED"]
    approver_id: str | None = None
    decided_at: float | None = None
    decision_call_id: str | None = None
    execution_status: Literal["NOT_STARTED", "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"]
    started_at: float | None = None
    finished_at: float | None = None
    error_code: str | None = None
    result: Any = None


def approval_request_id(tenant_id: str, user_id: str, call_id: str) -> str:
    identity = json.dumps([tenant_id, user_id, call_id], ensure_ascii=False)
    return "apr_" + hashlib.sha256(identity.encode()).hexdigest()


class ApprovalStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else DEFAULT_STORE_DIR

    def _path(self, request_id: str) -> Path:
        if not re.fullmatch(REQUEST_ID_PATTERN, request_id):
            raise PolicyDenied("INVALID_ARGUMENT", "审批编号格式无效")
        return self.directory / f"{request_id}.json"

    def read(self, request_id: str) -> dict[str, Any] | None:
        path = self._path(request_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PolicyDenied("STORAGE_ERROR", "无法读取审批记录") from exc
        try:
            record = ApprovalRecord.model_validate_json(text).model_dump()
            expected = approval_request_id(record["tenant_id"], record["initiator_id"], record["tool_call_id"])
            if record["request_id"] != request_id or expected != request_id:
                raise ValueError("request identity mismatch")
            return record
        except (ValueError, ValidationError) as exc:
            raise PolicyDenied("STORAGE_ERROR", "审批记录格式或身份绑定无效") from exc

    def write(self, record: Mapping[str, Any]) -> None:
        temporary = None
        try:
            parsed = ApprovalRecord.model_validate(record)
            path = self._path(parsed.request_id)
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(parsed.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValidationError) as exc:
            raise PolicyDenied("STORAGE_ERROR", "无法保存审批记录") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def records(self) -> list[dict[str, Any]]:
        try:
            paths = list(self.directory.glob("*.json"))
            return [record for path in paths if (record := self.read(path.stem)) is not None]
        except OSError as exc:
            raise PolicyDenied("STORAGE_ERROR", "无法列出审批记录") from exc

    @contextmanager
    def lock(self, request_id: str):
        path = self._path(request_id).with_suffix(".lock")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stream = path.open("a")
        except OSError as exc:
            raise PolicyDenied("STORAGE_ERROR", "无法创建审批锁") from exc
        with stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PolicyDenied("APPROVAL_IN_PROGRESS", "审批请求正在处理中") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def resolve_context(tenant_id: str, user_id: str, *, human: bool = False) -> ExecutionContext:
    if tenant_id not in {"tenant_a", "tenant_b"} or user_id not in {"u_100", "u_200"}:
        raise PolicyDenied("INVALID_CONTEXT", "未知的模拟身份")
    if user_id == "u_100":
        permissions = frozenset({"order:read", "refund:create", "transfer:create", "approval:list", "shell:run"})
        allowed_tools = frozenset({"get_order", "create_refund", "transfer", "list_approvals", "run_shell"})
        role = "initiator"
    else:
        permissions = frozenset({"approval:list", "approval:decide", "refund:approve", "transfer:approve"})
        allowed_tools = frozenset({"list_approvals", "decide_approval"} if human else {"list_approvals"})
        role = "approver"
    return ExecutionContext(
        trace_id=secrets.token_hex(12), tenant_id=tenant_id, user_id=user_id,
        permissions=permissions, allowed_tools=allowed_tools, role=role,
    )


class PermissionEngine:
    # 调用前授权决策组件：按白名单、业务权限和审批要求判定调用状态。
    def decide(self, tool: ToolDefinition, ctx: ExecutionContext) -> PermissionDecision:
        # 白名单和 RBAC 是确认或 bypass 都不可跳过的硬边界。
        if tool.name not in ctx.allowed_tools:
            return PermissionDecision.DENY
        if tool.policy.permission not in ctx.permissions:
            return PermissionDecision.DENY

        return PermissionDecision.ALLOW


@dataclass
class AuditSink:
    # 审计输出端口：记录调用链路、主体、风险、结果与耗时等可观测信息。
    records: list[dict[str, Any]] = field(default_factory=list)

    async def write(self, record: Mapping[str, Any]) -> None:
        # 持久化单条已脱敏的工具调用审计记录。
        self.records.append(dict(record))


# 识别令牌、密钥、密码和授权信息等敏感字段的匹配规则。
SENSITIVE_KEY = re.compile(
    r"token|secret|password|authorization|api_?key|credential|cookie|session|private_?key|access_?key",
    re.I,
)
MODEL_MAX_DEPTH = 8
MODEL_MAX_ITEMS = 50
MODEL_MAX_STRING_LENGTH = 2_000


def argument_keys(arguments: Any) -> list[str]:
    # 非法参数也可能把敏感信息放在键名里。
    return sorted(redact(str(key)) for key in arguments) if isinstance(arguments, Mapping) else []


def redact(value: Any) -> Any:
    # 审计和返回模型前均做递归脱敏，避免令牌、密码和邮箱泄露。
    if isinstance(value, Mapping):
        return {
            key: "***" if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***", value)
        return re.sub(r"\b(ACC-[A-Z]-)[0-9]{2}([0-9]{4})\b", r"\g<1>****\g<2>", text)
    return value


def project_for_model(value: Any, *, depth: int = 0) -> Any:
    # 对不可信工具结果脱敏并限制规模，防止其挤占模型上下文或伪装为控制信息。
    if depth >= MODEL_MAX_DEPTH:
        return "[内容嵌套过深，已截断]"
    if isinstance(value, Mapping):
        items = list(value.items())
        projected = {
            str(key): project_for_model(item, depth=depth + 1)
            for key, item in items[:MODEL_MAX_ITEMS]
        }
        if len(items) > MODEL_MAX_ITEMS:
            projected["_truncated"] = "[字段过多，已截断]"
        return redact(projected)
    if isinstance(value, (list, tuple)):
        projected = [project_for_model(item, depth=depth + 1)
                     for item in value[:MODEL_MAX_ITEMS]]
        if len(value) > MODEL_MAX_ITEMS:
            projected.append("[项目过多，已截断]")
        return redact(projected)
    if isinstance(value, str):
        text = value[:MODEL_MAX_STRING_LENGTH]
        if len(value) > MODEL_MAX_STRING_LENGTH:
            text += "[文本过长，已截断]"
        return redact(text)
    return redact(value)


class ToolRuntime:
    # 工具治理运行时：编排发现、前置门禁、受控执行、结果保护与审计闭环。

    def __init__(
        self,
        tools: Sequence[ToolDefinition],
        permissions: PermissionEngine,
        approvals: ApprovalStore,
        audit: AuditSink,
        *,
        confirmations: ConfirmationStore | None = None,
    ) -> None:
        # 装配工具注册表、授权、审批、审计依赖及按资源维度隔离的写锁。
        names = [tool.name for tool in tools]
        if not all(isinstance(name, str) and name for name in names) or len(names) != len(set(names)):
            raise ValueError("工具名称必须非空且唯一")
        for tool in tools:
            if not issubclass(tool.parameters_model, StrictArgs):
                raise ValueError(f"工具 {tool.name} 的参数模型必须继承 StrictArgs")
            if tool.policy.timeout_seconds <= 0 or tool.policy.max_retries < 0:
                raise ValueError(f"工具 {tool.name} 的超时或重试配置无效")
        self._tools = {tool.name: tool for tool in tools}
        self.permissions = permissions
        self.approvals = approvals
        self.context_resolver = resolve_context
        self.confirmations = confirmations if confirmations is not None else ConfirmationStore()
        self.audit = audit
        self._locks: dict[str, asyncio.Lock] = {}

    def model_tools(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        # 只向模型暴露当前上下文下可调用的工具，避免能力泄露。
        return [
            tool.to_model_tool()
            for tool in self._tools.values()
            if tool.governance_action != "decide"
            and self.permissions.decide(tool, ctx) is not PermissionDecision.DENY
        ]

    def _prepare(self, call: ToolCall, ctx: ExecutionContext) -> PreparedCall:
        if not isinstance(call.id, str) or not call.id or not isinstance(call.name, str) or not call.name:
            raise PolicyDenied("INVALID_ARGUMENT", "工具调用标识和名称必须为非空字符串")
        if not isinstance(call.arguments, Mapping):
            raise PolicyDenied("INVALID_ARGUMENT", "工具参数必须为对象")
        if not isinstance(ctx.mode, PermissionMode):
            raise PolicyDenied("INVALID_CONTEXT", "执行模式无效")
        tool = self._tools.get(call.name)
        if tool is None:
            raise PolicyDenied("TOOL_NOT_ALLOWED", f"工具 {call.name} 不在本轮白名单")

        try:
            args = tool.parameters_model.model_validate(call.arguments)
        except ValidationError as exc:
            errors = [
                {"path": ".".join(str(item)
                                  for item in item["loc"]), "message": item["msg"]}
                for item in exc.errors()
            ]
            raise PolicyDenied("INVALID_ARGUMENT", "参数校验失败", errors) from exc

        if tool.policy.denied or not tool.policy.enabled:
            raise PolicyDenied("DENY_RULE", "命中 deny 规则")
        if ctx.mode is PermissionMode.PLAN and tool.policy.effect in {Effect.WRITE, Effect.SHELL}:
            raise PolicyDenied("PLAN_MODE_DENIED", "plan 模式禁止写操作和 Shell")

        decision = self.permissions.decide(tool, ctx)
        if decision is PermissionDecision.DENY:
            if tool.name not in ctx.allowed_tools:
                raise PolicyDenied("TOOL_NOT_ALLOWED",
                                   f"工具 {call.name} 不在本轮白名单")
            raise PolicyDenied("PERMISSION_DENIED",
                               f"缺少权限 {tool.policy.permission}")

        rule = tool.policy.requires_approval
        needs_approval = rule(args) if callable(rule) else rule
        if ctx.mode is PermissionMode.DONT_ASK and (
            tool.policy.requires_confirmation or needs_approval or tool.governance_action == "decide"
        ):
            raise PolicyDenied("DONT_ASK_DENIED", "当前模式禁止需要确认或审批的操作")
        return PreparedCall(call, tool, args, decision)

    @staticmethod
    def _record_result(record: Mapping[str, Any]) -> dict[str, Any]:
        return redact({
            "request_id": record["request_id"], "status": record["approval_status"],
            "approval_status": record["approval_status"], "execution_status": record["execution_status"],
            "tool_call_id": record["tool_call_id"], "decision_call_id": record["decision_call_id"],
            "tool_name": record["tool_name"], "initiator_id": record["initiator_id"],
            "approver_id": record["approver_id"], "confirmed_at": record["confirmed_at"],
            "confirmation_id": record["confirmation_id"],
            "arguments": record["arguments"], "error_code": record["error_code"], "result": record["result"],
        })

    def _can_approve(self, record: Mapping[str, Any], ctx: ExecutionContext) -> bool:
        tool = self._tools.get(record["tool_name"])
        return (
            ctx.role == "approver" and record["tenant_id"] == ctx.tenant_id
            and record["initiator_id"] != ctx.user_id and tool is not None
            and tool.policy.approval_permission is not None
            and tool.policy.approval_permission in ctx.permissions
        )

    def approval_detail(self, request_id: str, ctx: ExecutionContext) -> dict[str, Any]:
        self._prepare(ToolCall("approval-detail", "decide_approval", {
            "request_id": request_id, "decision": "approve",
        }), ctx)
        record = self.approvals.read(request_id)
        if record is not None and record["tenant_id"] == ctx.tenant_id:
            tool = self._tools.get(record["tool_name"])
            if (ctx.role == "approver" and tool is not None
                    and tool.policy.approval_permission in ctx.permissions
                    and record["initiator_id"] == ctx.user_id):
                raise PolicyDenied("SELF_APPROVAL_DENIED", "不能审批自己发起的申请")
        if record is None or not self._can_approve(record, ctx):
            raise PolicyDenied("APPROVAL_NOT_ACCESSIBLE", "请求不存在或不可访问")
        return record

    def _list_approvals(self, args: ListApprovalsArgs, ctx: ExecutionContext) -> dict[str, Any]:
        visible = []
        for record in self.approvals.records():
            if ctx.role == "initiator":
                allowed = record["tenant_id"] == ctx.tenant_id and record["initiator_id"] == ctx.user_id
            else:
                allowed = self._can_approve(record, ctx)
            if allowed and (args.status == "ALL" or record["approval_status"] == args.status):
                visible.append(self._record_result(record))
        return {"approvals": sorted(visible, key=lambda record: record["confirmed_at"], reverse=True)}

    def _existing_request(self, prepared: PreparedCall, ctx: ExecutionContext) -> dict[str, Any] | None:
        request_id = approval_request_id(ctx.tenant_id, ctx.user_id, prepared.call.id)
        record = self.approvals.read(request_id)
        if record is not None and (
            record["tool_name"] != prepared.tool.name
            or record["args_digest"] != _approval_digest(prepared.tool.name, prepared.args)
        ):
            raise PolicyDenied("CALL_ID_CONFLICT", "该调用编号已绑定其他请求参数")
        return record

    def _submit(self, prepared: PreparedCall, ctx: ExecutionContext, confirmation_id: str | None) -> dict[str, Any]:
        request_id = approval_request_id(ctx.tenant_id, ctx.user_id, prepared.call.id)
        with self.approvals.lock(request_id):
            existing = self._existing_request(prepared, ctx)
            if existing is not None:
                return self._record_result(existing)
            record = ApprovalRecord(
                request_id=request_id, tool_call_id=prepared.call.id, tool_name=prepared.tool.name,
                tenant_id=ctx.tenant_id, initiator_id=ctx.user_id,
                arguments=prepared.args.model_dump(mode="json"),
                args_digest=_approval_digest(prepared.tool.name, prepared.args),
                confirmation_id=confirmation_id, confirmed_at=time.time(),
                approval_status="PENDING_APPROVAL", execution_status="NOT_STARTED",
            ).model_dump()
            self.approvals.write(record)
            return self._record_result(record)

    async def _resume(self, record: dict[str, Any], ctx: ExecutionContext) -> None:
        original = ToolCall(record["tool_call_id"], record["tool_name"], record["arguments"])
        try:
            initiator = self.context_resolver(record["tenant_id"], record["initiator_id"])
            initiator = replace(initiator, idempotency_key=original.id, trace_id=ctx.trace_id)
            prepared = self._prepare(original, initiator)
            if prepared.tool.policy.requires_confirmation and not record["confirmation_id"]:
                raise PolicyDenied("CONFIRMATION_INVALID", "审批记录缺少发起人的确认")
            if prepared.tool.governance_action or record["args_digest"] != _approval_digest(original.name, prepared.args):
                raise PolicyDenied("INVALID_ARGUMENT", "已确认的请求参数发生变化")
            content = await self._execute(prepared, initiator)
        except PolicyDenied as exc:
            state, code, content = "FAILED", exc.code, exc.content
        except TimeoutError:
            state, code, content = "UNKNOWN", "TIMEOUT_UNKNOWN", {"message": "执行结果待核对"}
        except asyncio.CancelledError:
            record.update(execution_status="UNKNOWN", error_code="CANCELLED", finished_at=time.time())
            try:
                self.approvals.write(record)
            except (OSError, PolicyDenied):
                # 已持久化的 RUNNING 留待下次持锁恢复为 UNKNOWN。
                pass
            await self._audit_execution(record, ctx)
            raise
        except Exception:
            state, code, content = "UNKNOWN", "TOOL_ERROR", {"message": "执行结果待核对"}
        else:
            state, code = "SUCCEEDED", None
        record.update(execution_status=state, error_code=code, result=redact(content), finished_at=time.time())
        await self._audit_execution(record, ctx)

    async def _audit_execution(self, record: Mapping[str, Any], ctx: ExecutionContext) -> None:
        await self.audit.write({
            "trace_id": ctx.trace_id, "tool_call_id": record["tool_call_id"], "tool_name": record["tool_name"],
            "tenant_id": record["tenant_id"], "user_id": record["initiator_id"],
            "approver_id": ctx.user_id, "request_id": record["request_id"],
            "decision_call_id": record["decision_call_id"], "stage": "execution",
            "ok": record["execution_status"] == "SUCCEEDED", "error_code": record["error_code"],
        })

    async def _decide_approval(self, prepared: PreparedCall, ctx: ExecutionContext) -> dict[str, Any]:
        args = prepared.args
        assert isinstance(args, DecideApprovalArgs)
        self.approval_detail(args.request_id, ctx)
        with self.approvals.lock(args.request_id):
            record = self.approval_detail(args.request_id, ctx)
            if record["execution_status"] == "RUNNING":
                record.update(execution_status="UNKNOWN", error_code="EXECUTION_UNKNOWN", finished_at=time.time())
                self.approvals.write(record)
                return self._record_result(record)
            if record["approval_status"] != "PENDING_APPROVAL":
                return self._record_result(record)
            record.update(approver_id=ctx.user_id, decided_at=time.time(), decision_call_id=prepared.call.id)
            if args.decision == "reject":
                record["approval_status"] = "REJECTED"
                self.approvals.write(record)
                return self._record_result(record)
            record.update(approval_status="APPROVED", execution_status="RUNNING", started_at=time.time())
            self.approvals.write(record)
            await self._resume(record, ctx)
            try:
                self.approvals.write(record)
            except (OSError, PolicyDenied) as exc:
                raise PolicyDenied("EXECUTION_UNKNOWN", "执行结果待核对", {
                    "request_id": args.request_id, "execution_status": "UNKNOWN", "message": "执行结果待核对",
                }) from exc
            return self._record_result(record)

    @staticmethod
    def _human_prompt_message(prepared: PreparedCall, ctx: ExecutionContext) -> str:
        return json.dumps({
            "call_id": prepared.call.id, "tool_name": prepared.tool.name,
            "user_id": ctx.user_id, "tenant_id": ctx.tenant_id,
            "arguments": prepared.args.model_dump(mode="json"),
        }, ensure_ascii=False)

    async def _confirm(
        self, prepared: PreparedCall, ctx: ExecutionContext, ask_confirmation: ConfirmationPrompt | None,
    ) -> str:
        # 文本快照只包含已校验参数，回调只返回答复，不接触凭证或 Context。
        if ask_confirmation is None:
            raise PolicyDenied("CONFIRMATION_UNAVAILABLE", "当前无法取得消费者明确答复")
        shown_digest = _approval_digest(prepared.tool.name, prepared.args)
        message = self._human_prompt_message(prepared, ctx)
        try:
            async with asyncio.timeout(CONFIRMATION_TIMEOUT_SECONDS):
                answer = await ask_confirmation(message)
        except Exception as exc:
            raise PolicyDenied("CONFIRMATION_UNAVAILABLE",
                               "当前无法取得消费者明确答复") from exc
        if answer is False:
            raise PolicyDenied("CONFIRMATION_REJECTED", "消费者拒绝了本次操作")
        if answer is None:
            raise PolicyDenied("CONFIRMATION_UNAVAILABLE", "当前无法取得消费者明确答复")
        if answer is not True:
            raise PolicyDenied("CONFIRMATION_INVALID", "消费者答复必须为明确同意或拒绝")
        try:
            current_args = prepared.tool.parameters_model.model_validate(
                prepared.call.arguments)
        except ValidationError as exc:
            raise PolicyDenied("CONFIRMATION_INVALID", "确认期间调用参数发生变化") from exc
        if (
            _approval_digest(prepared.tool.name, current_args) != shown_digest
            or _approval_digest(prepared.tool.name, prepared.args) != shown_digest
        ):
            raise PolicyDenied("CONFIRMATION_INVALID", "确认期间调用参数发生变化")
        confirmation_id = self.confirmations.approve(prepared, ctx)
        self.confirmations.consume(confirmation_id, prepared, ctx)
        return confirmation_id

    async def _execute_once(self, prepared: PreparedCall, ctx: ExecutionContext) -> dict[str, Any]:
        # 在工具策略规定的时限内执行一次实际处理器调用。
        async with asyncio.timeout(prepared.tool.policy.timeout_seconds):
            assert prepared.tool.handler is not None
            return await prepared.tool.handler(prepared.call.id, prepared.args, ctx)

    async def _execute_with_recovery(self, prepared: PreparedCall, ctx: ExecutionContext) -> dict[str, Any]:
        # 只重试读操作或显式幂等的写操作，避免重复产生不可逆副作用。
        policy = prepared.tool.policy
        retries = policy.max_retries if policy.effect is Effect.READ or policy.idempotent else 0
        for attempt in range(retries + 1):
            try:
                return await self._execute_once(prepared, ctx)
            except TransientToolError:
                if attempt == retries:
                    raise
                await asyncio.sleep(0.05 * (2**attempt))
        raise AssertionError("unreachable")

    async def _execute(self, prepared: PreparedCall, ctx: ExecutionContext) -> dict[str, Any]:
        # 按读写语义调度：读操作直接执行，写操作按业务资源串行化。
        policy = prepared.tool.policy
        if policy.effect is Effect.READ:
            return await self._execute_with_recovery(prepared, ctx)

        key = prepared.tool.canonical_target(prepared.args, ctx)
        # 同一业务目标的写操作串行执行，防止并发请求造成重复或竞争。
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 等待确认和资源锁期间业务状态可能改变，取得锁后再次只读预检。
            if prepared.tool.precheck:
                await prepared.tool.precheck(prepared.args, ctx)
            return await self._execute_with_recovery(prepared, ctx)

    async def invoke(
        self, call: ToolCall, ctx: ExecutionContext, *,
        ask_confirmation: ConfirmationPrompt | None = None,
    ) -> ToolResult:
        started_at = time.monotonic()
        prepared = None
        try:
            prepared = self._prepare(call, ctx)
            if prepared.tool.governance_action == "list":
                content = self._list_approvals(prepared.args, ctx)
            elif prepared.tool.governance_action == "decide":
                content = await self._decide_approval(prepared, ctx)
            else:
                existing = self._existing_request(prepared, ctx)
                if existing is not None:
                    content = self._record_result(existing)
                else:
                    if prepared.tool.precheck:
                        await prepared.tool.precheck(prepared.args, ctx)
                    confirmation_id = None
                    if prepared.tool.policy.requires_confirmation:
                        confirmation_id = await self._confirm(prepared, ctx, ask_confirmation)
                    rule = prepared.tool.policy.requires_approval
                    needs_approval = rule(prepared.args) if callable(rule) else rule
                    if needs_approval:
                        content = self._submit(prepared, ctx, confirmation_id)
                    else:
                        await self.audit.write({
                            "trace_id": ctx.trace_id, "tool_call_id": call.id, "tool_name": call.name,
                            "tenant_id": ctx.tenant_id, "user_id": ctx.user_id, "stage": "authorized",
                            "confirmation_id": confirmation_id, "decision": prepared.decision,
                            "effect": prepared.tool.policy.effect, "risk": prepared.tool.policy.risk,
                        })
                        content = await self._execute(prepared, ctx)
            ok, code = True, None
        except PolicyDenied as exc:
            ok, code, content = False, exc.code, exc.content
        except TimeoutError:
            code = "TIMEOUT_UNKNOWN" if prepared and prepared.tool.policy.effect is Effect.WRITE else "TIMEOUT"
            ok, content = False, {"message": "工具执行超时，请核对业务结果"}
        except OSError:
            ok, code, content = False, "STORAGE_ERROR", {"message": "审批存储不可用"}
        except TransientToolError:
            ok, code, content = False, "TEMPORARY_UNAVAILABLE", {"message": "依赖暂时不可用，请稍后重试"}
        except asyncio.CancelledError:
            cancelled_record = {
                "trace_id": ctx.trace_id, "tool_call_id": call.id, "tool_name": call.name,
                "tenant_id": ctx.tenant_id, "user_id": ctx.user_id,
                "ok": False, "error_code": "CANCELLED",
            }
            if prepared is not None and isinstance(prepared.args, DecideApprovalArgs):
                cancelled_record.update(request_id=prepared.args.request_id, decision_call_id=call.id)
                try:
                    saved = self.approvals.read(prepared.args.request_id)
                    if saved is not None:
                        cancelled_record["original_tool_call_id"] = saved["tool_call_id"]
                except (OSError, PolicyDenied):
                    pass
            await self.audit.write(cancelled_record)
            raise
        except Exception:
            ok, code, content = False, "TOOL_ERROR", {"message": "工具执行失败，请联系人工处理"}
        record = {
            "trace_id": ctx.trace_id, "tool_call_id": call.id, "tool_name": call.name,
            "tenant_id": ctx.tenant_id, "user_id": ctx.user_id, "ok": ok, "error_code": code,
            "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
            "argument_keys": argument_keys(call.arguments),
        }
        record["decision"] = prepared.decision if prepared is not None else PermissionDecision.DENY
        if prepared is not None:
            record.update(effect=prepared.tool.policy.effect, risk=prepared.tool.policy.risk)
        if isinstance(content, Mapping) and "request_id" in content:
            for key in ("request_id", "status", "confirmation_id", "decision_call_id", "approver_id"):
                record[key] = content.get(key)
        await self.audit.write(record)
        return ToolResult(call.id, call.name, ok, redact(content), code)

    async def invoke_batch(self, calls: Sequence[ToolCall], ctx: ExecutionContext) -> list[ToolResult]:
        # 批量调用调度器：纯读并发执行；含写操作时保持模型给出的因果顺序。
        # 纯读批次可并发；混入写操作时整批串行，保留模型给出的因果顺序。
        has_write = any(
            (tool := self._tools.get(call.name)
             ) is not None and tool.policy.effect is Effect.WRITE
            for call in calls
        )
        if has_write:
            return [await self.invoke(call, ctx) for call in calls]
        return list(await asyncio.gather(*(self.invoke(call, ctx) for call in calls)))


# 示例订单数据源：以租户和订单号共同隔离订单领域数据。
ORDERS: dict[tuple[str, str], dict[str, Any]] = {
    ("tenant_a", "ord_1001"): {
        "status": "paid",
        "refundable": "399.00",
        "customer_email": "alice@example.com",
    }
}
ACCOUNTS: dict[tuple[str, str], float] = {
    ("tenant_a", "ACC-A-123456"): 100000.0,
    ("tenant_a", "ACC-A-654321"): 5000.0,
    ("tenant_a", "ACC-A-888888"): 20000.0,
    ("tenant_b", "ACC-B-111111"): 50000.0,
}
# 示例退款结果存储：用于体现按订单幂等返回的写操作效果。
REFUNDS: dict[tuple[str, str], dict[str, Any]] = {}
TRANSFERS: dict[tuple[str, str, str], float] = {}


async def get_order(_tool_call_id: str, args: StrictArgs, ctx: ExecutionContext) -> dict[str, Any]:
    # 查询处理器：在租户隔离范围内读取订单，返回内容由运行时统一脱敏。
    assert isinstance(args, GetOrderArgs)
    order = ORDERS.get((ctx.tenant_id, args.order_id))
    if order is None:
        raise ValueError("order not found")
    return {"order_id": args.order_id, **order, "access_token": "do-not-leak"}


async def check_refund(args: StrictArgs, ctx: ExecutionContext) -> None:
    # 退款业务预检：在执行副作用前校验订单状态与可退金额。
    assert isinstance(args, CreateRefundArgs)
    order = ORDERS.get((ctx.tenant_id, args.order_id))
    if order is None or order["status"] != "paid":
        raise PolicyDenied("BUSINESS_RULE_DENIED", "订单不存在或当前状态不可退款")
    if args.amount > Decimal(order["refundable"]):
        raise PolicyDenied("BUSINESS_RULE_DENIED", "退款金额超过可退金额")


async def create_refund(_tool_call_id: str, args: StrictArgs, ctx: ExecutionContext) -> dict[str, Any]:
    # 退款写入处理器：按租户订单维度幂等创建并返回退款受理结果。
    assert isinstance(args, CreateRefundArgs)
    key = (ctx.tenant_id, args.order_id)
    if key in REFUNDS:
        return REFUNDS[key]
    result = {
        "refund_id": "ref_9001",
        "idempotency_key": ctx.idempotency_key,
        "tenant_id": ctx.tenant_id,
        "order_id": args.order_id,
        "amount": float(args.amount),
        "status": "accepted",
    }
    REFUNDS[key] = result
    return result


async def transfer_precheck(args: StrictArgs, context: ExecutionContext) -> None:
    assert isinstance(args, TransferArgs)
    if 50000 < args.amount <= 80000:
        raise PolicyDenied("EXCEED_LIMIT", "命中教学金额区间")
    from_key = (context.tenant_id, args.from_account)
    if ACCOUNTS.get(from_key, 0.0) < args.amount:
        raise PolicyDenied("INSUFFICIENT_BALANCE", "转出账户余额不足")
    if (context.tenant_id, args.to_account) not in ACCOUNTS:
        raise PolicyDenied("ACCOUNT_NOT_FOUND", "转入账户不存在")


async def transfer_handler(
    tool_call_id: str, args: StrictArgs, context: ExecutionContext,
) -> dict[str, Any]:
    assert isinstance(args, TransferArgs)
    if args.amount > 80000:
        await asyncio.sleep(3.0)

    from_key = (context.tenant_id, args.from_account)
    to_key = (context.tenant_id, args.to_account)
    if to_key not in ACCOUNTS:
        raise PolicyDenied("ACCOUNT_NOT_FOUND", "转入账户不存在")
    if from_key != to_key:
        from_balance, to_balance = ACCOUNTS[from_key], ACCOUNTS[to_key]
        remaining, credited = from_balance - args.amount, to_balance + args.amount
        if remaining == from_balance or credited == to_balance:
            raise PolicyDenied(
                "AMOUNT_BELOW_BALANCE_PRECISION", "转账金额低于账户余额可表示的精度")
        ACCOUNTS[from_key] = remaining
        ACCOUNTS[to_key] = credited
    TRANSFERS[(context.tenant_id, args.from_account,
               args.to_account)] = args.amount
    return {
        "transfer_id": "trf_7001",
        "idempotency_key": context.idempotency_key,
        "from": args.from_account,
        "to": args.to_account,
        "amount": args.amount,
        "status": "success",
    }


DANGEROUS_SHELL_PATTERNS = (
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bgit\s+push\s+--force\b", re.I),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r">\s*/dev/", re.I),
)


def _is_dangerous_shell(command: str) -> bool:
    return any(pattern.search(command) for pattern in DANGEROUS_SHELL_PATTERNS)


async def run_shell(_tool_call_id: str, args: StrictArgs, _ctx: ExecutionContext) -> dict[str, Any]:
    assert isinstance(args, RunShellArgs)
    if _is_dangerous_shell(args.command):
        raise PolicyDenied("SHELL_DENIED", "命令包含危险操作，已被拒绝")
    raise AssertionError("disabled shell tool must not execute")


def build_governance(*, store_dir: Path | None = None) -> tuple[ToolRuntime, ApprovalStore, AuditSink]:
    # 组合根：注册订单工具及其治理策略，并装配运行时依赖。
    approvals = ApprovalStore(store_dir)
    audit = AuditSink()
    tools = [
        ToolDefinition(
            name="get_order",
            description="查询订单当前状态和可退款金额。",
            parameters_model=GetOrderArgs,
            policy=ToolPolicy(
                effect=Effect.READ,
                risk=Risk.LOW,
                permission="order:read",
                timeout_seconds=1,
                max_retries=2,
                idempotent=True,
            ),
            handler=get_order,
            canonical_target=lambda args, ctx: f"{ctx.tenant_id}:{args.order_id}",
        ),
        ToolDefinition(
            name="create_refund",
            description="为已支付订单创建退款申请。",
            parameters_model=CreateRefundArgs,
            policy=ToolPolicy(
                effect=Effect.WRITE,
                risk=Risk.HIGH,
                permission="refund:create",
                requires_confirmation=True,
                requires_approval=True,
                approval_permission="refund:approve",
                timeout_seconds=2,
                max_retries=0,
                idempotent=False,
            ),
            handler=create_refund,
            precheck=check_refund,
            canonical_target=lambda args, ctx: f"{ctx.tenant_id}:{args.order_id}",
        ),
        ToolDefinition(
            name="run_shell",
            description="执行受控 Shell 命令。",
            parameters_model=RunShellArgs,
            policy=ToolPolicy(
                effect=Effect.SHELL,
                risk=Risk.HIGH,
                permission="shell:run",
                enabled=False,
            ),
            handler=run_shell,
            canonical_target=lambda args, ctx: "shell",
        ),
        ToolDefinition(
            name="transfer",
            description="在当前租户的两个账户之间转账。",
            parameters_model=TransferArgs,
            policy=ToolPolicy(
                effect=Effect.WRITE,
                risk=Risk.HIGH,
                permission="transfer:create",
                requires_confirmation=True,
                requires_approval=lambda args: args.amount > 80000,
                approval_permission="transfer:approve",
                timeout_seconds=2.0,
                max_retries=0,
                idempotent=False,
            ),
            handler=transfer_handler,
            precheck=transfer_precheck,
            canonical_target=lambda args, ctx: f"transfer:{ctx.tenant_id}",
        ),
    ]
    tools.extend([
        ToolDefinition(
            name="list_approvals", description="查看当前身份可访问的审批记录。",
            parameters_model=ListApprovalsArgs,
            policy=ToolPolicy(effect=Effect.READ, risk=Risk.LOW, permission="approval:list"),
            handler=None, canonical_target=lambda args, ctx: ctx.tenant_id, governance_action="list",
        ),
        ToolDefinition(
            name="decide_approval", description="由人工批准或拒绝待审批请求。",
            parameters_model=DecideApprovalArgs,
            policy=ToolPolicy(effect=Effect.WRITE, risk=Risk.HIGH, permission="approval:decide"),
            handler=None, canonical_target=lambda args, ctx: args.request_id, governance_action="decide",
        ),
    ])
    return ToolRuntime(
        tools, PermissionEngine(), approvals, audit, confirmations=ConfirmationStore(),
    ), approvals, audit


async def ask_confirmation_cli(message: str) -> bool | None:
    return await _ask_cli("确认", message)


async def _ask_cli(action: str, message: str) -> bool | None:
    # 交互层只展示 Runtime 的文本并返回答复，不创建或传递审批、确认凭证。
    print(f"请{action}以下操作：{message}\n是否同意？[y/n]", file=sys.stderr, flush=True)
    fd = sys.stdin.fileno()
    if stat.S_ISREG(os.fstat(fd).st_mode):
        answer = sys.stdin.readline()
    else:
        # POSIX 管道/终端使用可取消的事件监听；半行输入不能阻塞事件循环。
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        buffer = bytearray()

        def read_ready() -> None:
            if future.done():
                return
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                return
            except OSError as exc:
                future.set_exception(exc)
                return
            buffer.extend(chunk)
            if b"\n" in buffer or not chunk:
                future.set_result(bytes(buffer).split(b"\n", 1)[0])

        was_blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        try:
            try:
                loop.add_reader(fd, read_ready)
            except NotImplementedError as exc:
                raise OSError("当前终端不支持可取消的输入") from exc
            try:
                answer = (await future).decode(sys.stdin.encoding or "utf-8")
            finally:
                loop.remove_reader(fd)
        finally:
            os.set_blocking(fd, was_blocking)
    answer = answer.strip().lower()
    if answer in {"y", "yes"}:
        return True
    if answer in {"n", "no"}:
        return False
    return None


def output(result: ToolResult) -> None:
    content = result.content
    if result.ok and result.tool_name == "list_approvals":
        content = {"total": len(content["approvals"]), **content}
    print(json.dumps({
        "tool_call_id": result.tool_call_id,
        "tool_name": result.tool_name,
        "ok": result.ok,
        "action": PermissionDecision.ALLOW if result.ok else PermissionDecision.DENY,
        "code": result.error_code or "OK",
        "content": content,
    }, ensure_ascii=False, default=str, indent=2))


@dataclass(frozen=True)
class Example:
    tool_name: str
    description: str
    arguments: Mapping[str, Any]
    options: tuple[str, ...] = ()
    removed_permissions: frozenset[str] = frozenset()
    mode: PermissionMode = PermissionMode.DEFAULT


REFUND_EXAMPLE = {"order_id": "ord_1001", "amount": "399.00", "reason": "退款商品存在质量问题"}
TRANSFER_EXAMPLE = {"from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 1000.0}
EXAMPLES = {
    "get-order": Example("get_order", "查询订单并脱敏", {"order_id": "ord_1001"}, ("order_id",)),
    "get-order-no-permission": Example("get_order", "缺少订单查询权限", {"order_id": "ord_1001"}, removed_permissions=frozenset({"order:read"})),
    "get-order-invalid": Example("get_order", "订单号格式错误", {"order_id": "invalid"}),
    "get-order-not-found": Example("get_order", "订单不存在", {"order_id": "ord_9999"}),
    "refund-submit": Example("create_refund", "确认后提交退款审批", REFUND_EXAMPLE, ("order_id", "amount", "reason")),
    "refund-invalid": Example("create_refund", "拒绝额外退款参数", {**REFUND_EXAMPLE, "approved": True}),
    "refund-no-permission": Example("create_refund", "缺少退款权限", REFUND_EXAMPLE, removed_permissions=frozenset({"refund:create"})),
    "refund-wrong-role": Example("create_refund", "审批人尝试发起退款", REFUND_EXAMPLE),
    "refund-plan": Example("create_refund", "PLAN 模式拒绝退款", REFUND_EXAMPLE, mode=PermissionMode.PLAN),
    "refund-dont-ask": Example("create_refund", "DONT_ASK 拒绝确认操作", REFUND_EXAMPLE, mode=PermissionMode.DONT_ASK),
    "refund-exceed-amount": Example("create_refund", "退款金额超限", {**REFUND_EXAMPLE, "amount": "400.00"}),
    "refund-order-not-found": Example("create_refund", "退款订单不存在", {**REFUND_EXAMPLE, "order_id": "ord_9999"}),
    "shell-denied": Example("run_shell", "Shell 工具已禁用", {"command": "whoami"}),
    "transfer-normal": Example("transfer", "确认后普通转账", TRANSFER_EXAMPLE, ("from_account", "to_account", "amount")),
    "transfer-large": Example("transfer", "确认后提交大额转账审批", {**TRANSFER_EXAMPLE, "to_account": "ACC-A-888888", "amount": 90000.0}, ("from_account", "to_account", "amount")),
    "transfer-invalid": Example("transfer", "拒绝额外转账参数", {**TRANSFER_EXAMPLE, "approved": True}),
    "transfer-limit": Example("transfer", "命中教学金额区间", {**TRANSFER_EXAMPLE, "amount": 60000.0}),
    "transfer-wrong-role": Example("transfer", "审批人尝试发起转账", TRANSFER_EXAMPLE),
    "transfer-no-permission": Example("transfer", "缺少转账权限", TRANSFER_EXAMPLE, removed_permissions=frozenset({"transfer:create"})),
    "transfer-insufficient-balance": Example("transfer", "转出余额不足", {"from_account": "ACC-A-654321", "to_account": "ACC-A-123456", "amount": 6000.0}),
    "transfer-account-not-found": Example("transfer", "转入账户不存在", {**TRANSFER_EXAMPLE, "to_account": "ACC-A-000000"}),
    "transfer-cross-tenant": Example("transfer", "拒绝跨租户转账", {**TRANSFER_EXAMPLE, "to_account": "ACC-B-111111"}),
    "approvals-list": Example("list_approvals", "查看可见审批", {"status": "PENDING_APPROVAL"}, ("status",)),
    "approvals-list-no-permission": Example("list_approvals", "缺少审批查询权限", {}, removed_permissions=frozenset({"approval:list"})),
    "approvals-list-invalid": Example("list_approvals", "审批筛选状态非法", {"status": "INVALID"}),
    "approval-decide": Example("decide_approval", "人工批准或拒绝", {}, ("request_id", "decision")),
    "approval-self": Example("decide_approval", "模拟历史发起人转为审批人，拒绝审批本人申请", {"decision": "approve"}),
    "approval-wrong-role": Example("decide_approval", "发起人尝试审批", {"decision": "approve"}, ("request_id",)),
    "approval-no-permission": Example("decide_approval", "缺少审批动作权限", {"decision": "approve"}, ("request_id",), frozenset({"approval:decide"})),
    "approval-no-business-permission": Example("decide_approval", "缺少业务审批权限", {"decision": "approve"}, ("request_id",), frozenset({"refund:approve", "transfer:approve"})),
    "approval-cross-tenant": Example("decide_approval", "拒绝跨租户审批", {"decision": "approve"}, ("request_id",)),
    "approval-not-found": Example("decide_approval", "审批请求不存在", {"decision": "approve"}, ("request_id",)),
    "approval-invalid": Example("decide_approval", "审批参数非法", {"request_id": "invalid", "decision": "invalid"}),
}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="工具治理示例：按租户与模拟用户运行一次调用。",
        epilog="\n".join(f"{name}: {example.description}" for name, example in EXAMPLES.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tenant_id")
    parser.add_argument("user_id")
    parser.add_argument("example", choices=EXAMPLES)
    for option in ("order_id", "amount", "reason", "from_account", "to_account", "status", "request_id", "decision"):
        parser.add_argument("--" + option.replace("_", "-"), default=argparse.SUPPRESS)
    parsed = vars(parser.parse_args())
    tenant_id, user_id, name = (parsed.pop(key) for key in ("tenant_id", "user_id", "example"))
    example = EXAMPLES[name]
    if set(parsed) - set(example.options):
        parser.error("该例子不接受选项：" + ", ".join(sorted(set(parsed) - set(example.options))))
    arguments = {**example.arguments, **parsed}
    if example.tool_name == "decide_approval" and name not in {"approval-invalid", "approval-self"}:
        if not arguments.get("request_id") or not arguments.get("decision"):
            parser.error("审批需要 --request-id 和 --decision")
    if example.tool_name == "transfer" and "amount" in parsed:
        try:
            arguments["amount"] = float(parsed["amount"])
        except ValueError:
            parser.error("--amount 必须是数值")
    try:
        context = resolve_context(tenant_id, user_id, human=True)
    except PolicyDenied as exc:
        parser.error(str(exc))
    context = replace(context, permissions=context.permissions - example.removed_permissions, mode=example.mode)
    runtime, _, audit = build_governance()
    if name == "approval-self":
        if context.role != "approver":
            parser.error("approval-self 需要审批人身份 u_200")
        print("教学模拟：先以历史发起人身份确认退款，再以当前审批人身份尝试自审。", file=sys.stderr)
        # 仅此教学例子模拟角色变更；申请仍通过完整确认和落盘流程创建。
        historical = replace(resolve_context(tenant_id, "u_100"), user_id=user_id)
        submission = ToolCall("call_" + secrets.token_hex(12), "create_refund", REFUND_EXAMPLE)
        submitted = await runtime.invoke(submission, historical, ask_confirmation=ask_confirmation_cli)
        if not submitted.ok:
            output(submitted)
            return
        arguments["request_id"] = submitted.content["request_id"]
        print("本人申请 request_id: " + arguments["request_id"], file=sys.stderr)
    call = ToolCall("call_" + secrets.token_hex(12), example.tool_name, arguments)
    context = replace(context, idempotency_key=call.id)
    if example.tool_name == "decide_approval":
        try:
            # 详情读取与实际决定各自执行访问校验。
            runtime._prepare(call, context)
            detail = runtime.approval_detail(arguments["request_id"], context)
        except PolicyDenied:
            result = await runtime.invoke(call, context)
        else:
            if detail["approval_status"] == "PENDING_APPROVAL":
                message = json.dumps({"decision": arguments["decision"], "request": detail}, ensure_ascii=False)
                try:
                    async with asyncio.timeout(CONFIRMATION_TIMEOUT_SECONDS):
                        answer = await _ask_cli("审批", message)
                except (TimeoutError, OSError):
                    answer = None
                if answer is not True:
                    output(ToolResult(call.id, call.name, False, {"message": "审批决定已取消"}, "APPROVAL_CANCELLED"))
                    return
            result = await runtime.invoke(call, context)
    else:
        result = await runtime.invoke(call, context, ask_confirmation=ask_confirmation_cli)
    output(result)
    print(json.dumps({
        "audit_records": audit.records,
        "side_effects": {"refund_records": len(REFUNDS), "transfer_records": len(TRANSFERS)},
    }, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
