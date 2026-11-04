from starlette import status as http_status


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


PROVIDER_MESSAGES = {
    'provider_error': '模型调用失败',
    'provider_auth_error': '模型服务认证失败',
    'provider_quota_exceeded': '模型服务余额或额度不足',
    'provider_rate_limited': '模型服务请求受限',
    'provider_connection_error': '模型服务连接失败',
    'provider_timeout': '模型调用超时',
    'provider_unavailable': '模型服务暂时不可用',
    'provider_rejected_request': '模型服务拒绝请求参数',
    'provider_invalid_response': '模型服务返回结果不合法',
    'provider_content_filtered': '模型输出被内容过滤',
    'provider_refused': '模型拒绝回答本次请求',
    'output_truncated': '模型输出达到长度限制，未完整完成',
}


class ProviderCallError(Exception):
    """Safe classification and confirmed metadata; never retain raw exceptions."""
    def __init__(self, raw_message='', *, code='provider_error', status_code=http_status.HTTP_502_BAD_GATEWAY,
                 actual_model=None, finish_reason=None, usage=None, cached_input_tokens=None, billing_observed_at=None,
                 upstream_status=None, upstream_finish_reason=None, transient=False,
                 retry_after_seconds=None, upstream_http_status=None):
        self.code = code
        self.status_code = status_code
        self.message = PROVIDER_MESSAGES[code]
        self.actual_model = actual_model
        self.finish_reason = finish_reason
        self.usage = usage
        self.cached_input_tokens = cached_input_tokens
        self.billing_observed_at = billing_observed_at
        self.upstream_status = upstream_status
        self.upstream_finish_reason = upstream_finish_reason
        self.transient = transient
        self.retry_after_seconds = retry_after_seconds
        self.upstream_http_status = upstream_http_status
        super().__init__(self.message)
