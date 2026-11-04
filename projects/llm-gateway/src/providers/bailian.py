"""Bailian-specific Responses mapping over the shared SDK transport.

Official contract: help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses
Schema settings are nested, and early queued events need not name a model.
Missing early metadata remains unknown; the final result is still strict.
"""
from providers.openai_compatible import OpenAICompatibleProvider
from providers.streaming import responses_events


class BailianProvider(OpenAICompatibleProvider):
    def _response_format(self, schema):
        return {'type':'json_schema','json_schema':{
            'name':'gateway_response','schema':schema,'strict':True}}

    def _stream_events(self, lines, observed):
        return responses_events(lines,observed,allow_pending_metadata=True)
