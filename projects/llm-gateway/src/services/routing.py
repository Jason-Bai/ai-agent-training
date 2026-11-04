"""Local, ordered endpoint selection; no Provider calls or lifecycle ownership."""
from dataclasses import dataclass
from hashlib import sha256
import json

from contracts import Message
from errors import GatewayError
from services.models import ModelConfig, ProviderConfig
from services.prompts import build_messages, fingerprint_messages, validate_selection
from services.token_counters.base import TokenCount
from services.validation import PreparedSchema, prepare_schema


def endpoint_snapshot(endpoint):
    """Keep the F12 shape when the positive-output minimum is unchanged."""
    value = endpoint.model_dump(mode='json')
    if endpoint.min_output_tokens == 1:
        value.pop('min_output_tokens')
    return value


@dataclass(frozen=True)
class RouteCandidate:
    endpoint_id: str
    endpoint: ModelConfig
    provider: ProviderConfig
    messages: tuple[Message, ...]
    measured: TokenCount


@dataclass(frozen=True)
class RoutePlan:
    group_name: str
    candidates: tuple[RouteCandidate, ...]
    decisions: tuple[tuple[str, str | None], ...]
    configuration: str
    max_output_tokens: int
    initial_output_tokens: int
    timeout_seconds: int
    schema: PreparedSchema | None
    strategy: str = 'ordered'

    @property
    def config_fingerprint(self):
        return sha256(self.configuration.encode()).hexdigest()

    def metadata(self):
        return {
            'version': 1, 'strategy': self.strategy,
            'config_fingerprint': self.config_fingerprint,
            'candidates': [{'endpoint_id': name, 'status': 'skipped' if code else 'eligible', 'code': code}
                           for name, code in self.decisions],
            'selected_endpoint': self.candidates[0].endpoint_id if self.candidates else None,
            'reason': 'first_eligible' if self.candidates else 'no_eligible_endpoint',
        }

    def snapshot(self):
        first = self.candidates[0]
        return {
            'snapshot_version': 1,
            'routing': self.metadata(),
            'routing_configuration': json.loads(self.configuration),
            'messages': [message.model_dump() for message in first.messages],
            'model': endpoint_snapshot(first.endpoint),
            'provider': first.provider.model_dump(),
            'max_output_tokens': self.max_output_tokens,
            'initial_output_tokens': self.initial_output_tokens,
            'timeout_seconds': self.timeout_seconds,
            'response_schema': self.schema.schema if self.schema is not None else None,
            'recovery': json.loads(self.configuration)['recovery'],
            'candidates': [
                {'name': candidate.endpoint_id, 'model': endpoint_snapshot(candidate.endpoint),
                 'provider': candidate.provider.model_dump(),
                 'messages': [message.model_dump() for message in candidate.messages],
                 'input_token_count': candidate.measured.input_tokens,
                 'input_token_kind': candidate.measured.kind,
                 'counter_version': candidate.measured.counter_version}
                for candidate in self.candidates
            ],
            'skipped_candidates': [{'model': name, 'code': code} for name, code in self.decisions if code],
        }


class Router:
    def __init__(self, registry, prompt_registry, token_budget):
        self.registry, self.prompts, self.budget = registry, prompt_registry, token_budget

    def configuration_snapshot(self, group_name):
        group = self.registry.get_group(group_name)
        endpoints = {name: self.registry.get_endpoint(name) for name in group.candidates}
        return {
            'group_name': group_name, 'group': group.model_dump(mode='json', exclude_none=True),
            'endpoints': {name: endpoint_snapshot(endpoint) for name, endpoint in endpoints.items()},
            'providers': {endpoint.provider: self.registry.providers[endpoint.provider].model_dump()
                          for endpoint in endpoints.values()},
            'counter_versions': {name: self.budget.counter_for(endpoint).version
                                 for name, endpoint in endpoints.items()},
            'recovery': self.registry.recovery.model_dump(),
        }

    def configuration_fingerprint(self, group_name):
        return sha256(self._configuration(group_name).encode()).hexdigest()

    def _configuration(self, group_name):
        return json.dumps(self.configuration_snapshot(group_name), sort_keys=True,
                          ensure_ascii=False, separators=(',', ':'))

    def route(self, request, trace, *, streaming=False, probe=False):
        if not probe and self.registry.get_auto_route(request.model) is not None:
            from services.auto_routing import plan_auto_route
            return plan_auto_route(self, request, trace, streaming=streaming)
        trace.requested_model = request.model
        trace.prompt_name, trace.prompt_version = request.prompt.name, request.prompt.version
        trace.structured_output_mode = 'none'
        schema_value = getattr(request, 'response_schema', None)
        schema = prepare_schema(schema_value) if schema_value is not None else None
        if schema is not None:
            trace.schema_fingerprint = schema.fingerprint
        template = validate_selection(request.prompt, self.prompts)
        if schema is not None and not template.supports_structured_output:
            raise GatewayError('unsupported_capability', 'Prompt 模板不支持结构化输出', 400)
        # Cache shared text and known fingerprints, but report render overflow
        # only when the ordered filtering reaches the render stage.
        text_messages = None
        if schema is None:
            try:
                text_messages = build_messages(request.prompt, request.messages, self.prompts)
                trace.prompt_fingerprint = fingerprint_messages(text_messages)
            except GatewayError as error:
                if error.code != 'prompt_too_long':
                    raise
        group = self.registry.get_group(request.model)
        output = group.default_output_tokens if request.max_output_tokens is None else request.max_output_tokens
        timeout = group.default_timeout_seconds if request.timeout_seconds is None else request.timeout_seconds
        if output > group.max_output_tokens:
            raise GatewayError('invalid_output_budget', '输出预算超过模型组的平台上限', 400)
        if request.initial_output_tokens is not None and request.initial_output_tokens > output:
            raise GatewayError('invalid_output_budget', '首次输出预算超过授权上限', 400)
        trace.max_output_tokens = output
        if timeout > group.max_timeout_seconds:
            raise GatewayError('invalid_timeout', '调用超时超过模型组的平台上限', 400)
        trace.timeout_seconds = timeout
        initial = request.initial_output_tokens or output
        configuration = self._configuration(request.model)
        codes = {name: None for name in group.candidates}
        eligible = list(group.candidates)
        messages, measured = {}, {}
        strategy = 'probe_first_enabled' if probe else 'ordered'

        def plan(candidates=()):
            return RoutePlan(request.model, tuple(candidates), tuple(codes.items()), configuration,
                             output, initial, timeout, schema, strategy)

        def publish(selected=()):
            trace.routing = plan(selected).metadata()
            trace._skipped_candidates = [{'model': name, 'code': code}
                                         for name, code in codes.items() if code]

        def stage(check, code, message, status=400):
            nonlocal eligible
            remaining = []
            for name in eligible:
                endpoint = self.registry.get_endpoint(name)
                if check(name, endpoint):
                    remaining.append(name)
                else:
                    codes[name] = code
            eligible = remaining
            if not eligible:
                publish()
                trace.error_stage = 'routing'
                raise GatewayError(code if code != 'endpoint_disabled' else 'no_available_endpoint',
                                   message, status)

        stage(lambda name, endpoint: endpoint.enabled, 'endpoint_disabled',
              '模型组没有启用的候选端点', 503)
        if probe:
            chosen = eligible[0]
            # A health probe checks one endpoint, never a hidden fallback plan.
            prefix = list(codes)[:list(codes).index(chosen) + 1]
            codes = {name: codes[name] for name in prefix}
            eligible = [chosen]
            if timeout > self.registry.get_endpoint(chosen).max_timeout_seconds:
                raise GatewayError('invalid_timeout', '探测固定超时超过端点上限', 400)
        stage(lambda name, endpoint: (not streaming or endpoint.supports_streaming)
              and (schema is None or endpoint.structured_output_mode != 'none'),
              'unsupported_capability', '模型组没有支持本次调用能力的端点')
        stage(lambda name, endpoint: endpoint.min_output_tokens <= initial
              and output <= endpoint.max_output_tokens,
              'invalid_output_budget', '候选端点无法承接本次输出预算')

        def render(name, endpoint):
            try:
                if schema is None:
                    if text_messages is None:
                        return False
                    messages[name] = text_messages
                else:
                    contract = ('{"mode":"json","schema":' + schema.canonical + '}'
                                if endpoint.structured_output_mode == 'json_object' else '{"mode":"json"}')
                    messages[name] = build_messages(request.prompt, request.messages, self.prompts,
                                                    output_contract=contract)
                return True
            except GatewayError as error:
                if error.code != 'prompt_too_long':
                    raise
                return False

        stage(render, 'prompt_too_long', '候选端点渲染后的User内容均超过字符上限')
        first_measured = eligible[0]
        diagnostic_endpoint = self.registry.get_endpoint(first_measured)
        trace.prompt_fingerprint = fingerprint_messages(messages[first_measured])
        trace.counter_version = self.budget.counter_for(diagnostic_endpoint).version
        trace.structured_output_mode = diagnostic_endpoint.structured_output_mode if schema is not None else 'none'

        def fits(name, endpoint):
            options = {'response_schema': schema.schema} if schema is not None else {}
            measured[name] = self.budget.count(endpoint, messages[name], **options)
            if name == first_measured:
                # A rejected request still retains facts already measured. A
                # successful selection below replaces these with its own data.
                trace.input_token_count = measured[name].input_tokens
                trace.counter_version = measured[name].counter_version
                trace.input_token_kind = measured[name].kind
            try:
                self.budget.check(measured[name].input_tokens, output, endpoint)
                return True
            except GatewayError as error:
                if error.code != 'context_length_exceeded':
                    raise
                return False

        stage(fits, 'context_length_exceeded', '候选端点的输入与预留输出均超过上下文预算')
        candidates = tuple(RouteCandidate(name, self.registry.get_endpoint(name),
                                         self.registry.providers[self.registry.get_endpoint(name).provider],
                                         tuple(messages[name]), measured[name]) for name in eligible)
        publish(candidates)
        first = candidates[0]
        trace.prompt_fingerprint = fingerprint_messages(first.messages)
        trace.input_token_count, trace.counter_version = first.measured.input_tokens, first.measured.counter_version
        trace.input_token_kind = first.measured.kind
        trace.structured_output_mode = first.endpoint.structured_output_mode if schema is not None else 'none'
        return plan(candidates)
