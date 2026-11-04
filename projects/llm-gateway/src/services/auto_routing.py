"""Pure local group and classifier plans; no execution or lifecycle ownership."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from errors import GatewayError
from services.classification import prepare_classification
from services.models import AutoRouteConfig
from services.prompts import build_messages, fingerprint_messages, validate_selection
from services.routing import RouteCandidate, RoutePlan
from services.trace import RequestTrace
from services.validation import PreparedSchema, prepare_schema
from utils.json_codec import dumps, loads


_GROUP_FILTER_ERRORS = frozenset({'no_available_endpoint', 'unsupported_capability',
    'invalid_output_budget', 'invalid_timeout', 'prompt_too_long', 'context_length_exceeded'})


def _template_definition(template):
    return {
        'name': template.name, 'version': template.version,
        'system_template': template.system_template, 'user_template': template.user_template,
        'variable_specs': {name: {'max_chars': spec.max_chars}
                           for name, spec in template.variable_specs.items()},
        'few_shots': [{'user': example.user, 'assistant': example.assistant}
                      for example in template.few_shots],
        'supports_structured_output': template.supports_structured_output,
        'change_note': template.change_note,
    }


def auto_configuration_snapshot(router, alias, request):
    profile = router.registry.get_auto_route(alias)
    if profile is None:
        raise GatewayError('unknown_model', '自动路由未注册', 400)
    groups = dict.fromkeys((*profile.candidate_groups, profile.classifier_group))
    return {
        'profile_name': alias, 'profile': profile.model_dump(mode='json'),
        'groups': {name: router.configuration_snapshot(name) for name in groups},
        'classifier_prompt': _template_definition(router.prompts.get(
            profile.classifier_prompt.name, profile.classifier_prompt.version)),
        'answer_prompt': _template_definition(router.prompts.get(request.prompt.name, request.prompt.version)),
    }


@dataclass(frozen=True)
class AutoRoutePlan:
    profile_name: str
    profile: AutoRouteConfig
    plans: Mapping[str, RoutePlan]
    decisions: tuple[tuple[str, str | None], ...]
    classifier: RoutePlan | None
    classifier_error: str | None
    max_output_tokens: int
    initial_output_tokens: int
    timeout_seconds: int
    schema: PreparedSchema | None
    configuration: str
    request_json: str

    def __post_init__(self):
        object.__setattr__(self, 'plans', MappingProxyType(dict(self.plans)))

    def metadata(self, *, group=None, source=None, fallback_reason=None, duration=None):
        return {
            'version': 1, 'profile': self.profile_name,
            'candidate_groups': [{'group': name, 'status': 'skipped' if code else 'eligible', 'code': code}
                                 for name, code in self.decisions],
            'resolved_group': group, 'source': source, 'fallback_reason': fallback_reason,
            'classifier_prompt_name': self.profile.classifier_prompt.name,
            'classifier_prompt_version': self.profile.classifier_prompt.version,
            'classification_duration_ms': duration,
        }

    def select(self, group, trace, source, fallback_reason=None):
        if source not in {'classifier', 'single_eligible_group', 'default_fallback'}:
            raise ValueError('Unknown automatic route selection source')
        if group not in self.plans:
            code = 'auto_route_failed' if source == 'default_fallback' else 'classifier_invalid_group'
            raise GatewayError(code, '所选模型组不满足本次硬条件', 503 if source == 'default_fallback' else 502)
        selected = self.plans[group]
        first = selected.candidates[0]
        previous = getattr(trace, 'auto_routing', None) or {}
        trace.auto_routing = self.metadata(group=group, source=source, fallback_reason=fallback_reason,
                                           duration=previous.get('classification_duration_ms'))
        trace.requested_model = self.profile_name
        trace.resolved_group = group
        trace.routing = selected.metadata()
        trace.prompt_fingerprint = fingerprint_messages(first.messages)
        trace.input_token_count = first.measured.input_tokens
        trace.counter_version = first.measured.counter_version
        trace.input_token_kind = first.measured.kind
        trace.structured_output_mode = first.endpoint.structured_output_mode if selected.schema else 'none'
        trace.schema_fingerprint = selected.schema.fingerprint if selected.schema else None
        trace.max_output_tokens, trace.timeout_seconds = self.max_output_tokens, self.timeout_seconds
        trace._skipped_candidates = [{'model': name, 'code': code} for name, code in selected.decisions if code]
        return selected

    def snapshot(self):
        return {
            'snapshot_version': 2, 'auto_request': loads(self.request_json),
            'auto_configuration': loads(self.configuration),
            'candidate_plans': {name: plan.snapshot() for name, plan in self.plans.items()},
            'classifier_plan': self.classifier.snapshot() if self.classifier is not None else None,
            'classifier_error': self.classifier_error, 'auto_routing': self.metadata(),
        }


def _classification_plan(router, profile, prepared):
    group = router.registry.get_group(profile.classifier_group)
    codes = []
    error_code = 'no_available_endpoint'
    for name in group.candidates:
        endpoint = router.registry.get_endpoint(name)
        code = None
        if not endpoint.enabled:
            code = 'endpoint_disabled'
        elif endpoint.structured_output_mode == 'none':
            code = 'unsupported_capability'
        elif not endpoint.min_output_tokens <= profile.classifier_max_output_tokens <= endpoint.max_output_tokens:
            code = 'invalid_output_budget'
        elif profile.classifier_timeout_seconds > endpoint.max_timeout_seconds:
            code = 'invalid_timeout'
        if code is not None:
            codes.append((name, code))
            error_code = 'no_available_endpoint' if code == 'endpoint_disabled' else code
            continue
        measured = router.budget.count(endpoint, prepared.messages, response_schema=prepared.schema.schema)
        try:
            router.budget.check(measured.input_tokens, profile.classifier_max_output_tokens, endpoint)
        except GatewayError as error:
            if error.code != 'context_length_exceeded':
                raise
            codes.append((name, error.code)); error_code = error.code
            continue
        codes.append((name, None))
        candidate = RouteCandidate(name, endpoint, router.registry.providers[endpoint.provider],
                                   prepared.messages, measured)
        return RoutePlan(profile.classifier_group, (candidate,), tuple(codes),
            dumps(router.configuration_snapshot(profile.classifier_group), sort_keys=True),
            profile.classifier_max_output_tokens, profile.classifier_max_output_tokens,
            profile.classifier_timeout_seconds, prepared.schema), None
    return None, error_code


def plan_auto_route(router, request, trace, *, streaming=False):
    profile = router.registry.get_auto_route(request.model)
    if profile is None:
        raise GatewayError('unknown_model', '自动路由未注册', 400)
    trace.requested_model = request.model
    trace.prompt_name, trace.prompt_version = request.prompt.name, request.prompt.version
    template = validate_selection(request.prompt, router.prompts)
    schema_value = getattr(request, 'response_schema', None)
    schema = prepare_schema(schema_value) if schema_value is not None else None
    if schema is not None and not template.supports_structured_output:
        raise GatewayError('unsupported_capability', 'Prompt 模板不支持结构化输出', 400)
    # Shared text must be valid independently of any endpoint. A subsequent
    # json_object Schema contract can still overflow only an affected endpoint.
    build_messages(request.prompt, request.messages, router.prompts)
    if schema is not None:
        trace.schema_fingerprint = schema.fingerprint
    output = profile.default_output_tokens if request.max_output_tokens is None else request.max_output_tokens
    initial = output if request.initial_output_tokens is None else request.initial_output_tokens
    timeout = profile.default_timeout_seconds if request.timeout_seconds is None else request.timeout_seconds
    if output > profile.max_output_tokens or initial > output:
        raise GatewayError('invalid_output_budget', '输出预算超过自动路由授权', 400)
    if timeout > profile.max_timeout_seconds:
        raise GatewayError('invalid_timeout', '总超时超过自动路由授权', 400)
    trace.max_output_tokens, trace.timeout_seconds = output, timeout
    plans, decisions = {}, []
    for name in profile.candidate_groups:
        candidate_request = request.model_copy(update={'model': name, 'max_output_tokens': output,
                                                       'initial_output_tokens': initial, 'timeout_seconds': timeout})
        candidate_trace = RequestTrace(trace.request_id)
        try:
            plans[name] = router.route(candidate_request, candidate_trace, streaming=streaming)
            decisions.append((name, None))
        except GatewayError as error:
            if error.code not in _GROUP_FILTER_ERRORS:
                raise
            decisions.append((name, error.code))
    classifier, classifier_error = None, None
    if len(plans) > 1:
        first = next(iter(plans.values())).candidates[0]
        descriptions = {name: router.registry.get_group(name).routing_description for name in plans}
        try:
            prepared = prepare_classification(profile, router.prompts, request, first.messages, descriptions)
            classifier, classifier_error = _classification_plan(router, profile, prepared)
        except GatewayError as error:
            if error.code not in {'prompt_too_long', 'invalid_prompt_variables'}:
                raise
            classifier_error = error.code
    result = AutoRoutePlan(request.model, profile, plans, tuple(decisions), classifier, classifier_error,
                           output, initial, timeout, schema,
                           dumps(auto_configuration_snapshot(router, request.model, request), sort_keys=True),
                           dumps(request.model_dump(exclude_none=True)))
    trace.auto_routing = result.metadata()
    if not plans:
        trace.error_stage = 'routing'
        raise GatewayError('no_available_group', '没有模型组满足本次硬条件', 400)
    first = next(iter(plans.values())).candidates[0]
    trace.prompt_fingerprint = fingerprint_messages(first.messages)
    trace.input_token_count, trace.counter_version = first.measured.input_tokens, first.measured.counter_version
    trace.input_token_kind = first.measured.kind
    return result
