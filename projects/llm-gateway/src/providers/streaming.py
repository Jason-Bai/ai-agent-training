"""Wire-only SSE decoding and protocol state; Gateway consumes platform events."""
from constants import CHAT_TERMINAL_REASONS
from errors import ProviderCallError
from providers.base import ProviderStreamEvent, observe_terminal_usage, observe_reported_metadata, parse_responses_response
from utils.diagnostic_values import metadata_text
from utils.json_codec import loads


def failure(code, **metadata):
    return ProviderStreamEvent(status='failed', error_code=code, **metadata)


async def frames(lines):
    data=[]; name=None; first=True
    async for line in lines:
        if first:
            line=line.removeprefix('\ufeff');first=False
        if not line:
            if data:yield name, '\n'.join(data)
            data=[];name=None
        elif not line.startswith(':'):
            field,_,value=line.partition(':')
            value=value.removeprefix(' ')
            if field=='data':data.append(value)
            elif field=='event':name=value
    # An unterminated SSE event is not a completed protocol message.


_REASONS={'length':'output_truncated','content_filter':'provider_content_filtered',
          'refusal':'provider_refused','aborted':'provider_unavailable',
          'insufficient_system_resource':'provider_unavailable'}


async def chat_events(lines, observed):
    terminal=None
    async for name,data in frames(lines):
        if data=='[DONE]':
            if terminal is None:raise ValueError
            yield terminal
            return
        if terminal is not None:raise ValueError
        raw=loads(data)
        if not isinstance(raw,dict) or raw.get('object')!='chat.completion.chunk':raise ValueError
        actual,usage=observe_reported_metadata(raw, observed, chat=True)
        meta={'actual_model':actual}
        choices=raw.get('choices')
        if not isinstance(choices,list) or len(choices)!=1:raise ValueError
        choice=choices[0]
        if not isinstance(choice,dict) or type(choice.get('index')) is not int or choice['index']!=0:raise ValueError
        reason=choice.get('finish_reason')
        if reason is not None and metadata_text(reason) is None:raise ValueError
        if reason in CHAT_TERMINAL_REASONS:
            observe_terminal_usage(observed)
        delta=choice.get('delta')
        if not isinstance(delta,dict) or delta.get('role','assistant')!='assistant' or delta.get('tool_calls'):raise ValueError
        text=delta.get('content','')
        if text is None:text=''
        if not isinstance(text,str):raise ValueError
        refusal=delta.get('refusal')
        if refusal is not None:
            if not isinstance(refusal,str):raise ValueError
            if refusal:reason='refusal'
        if reason is None:
            if usage is not None:raise ValueError
            yield ProviderStreamEvent(delta=text,**meta)
        elif reason=='stop':
            terminal=ProviderStreamEvent(status='completed',usage=usage,cached_input_tokens=observed.get('cached_input_tokens'),billing_observed_at=observed.get('billing_observed_at'),upstream_terminal_received=True,upstream_finish_reason='stop',**meta)
            observed.update(usage=terminal.usage,upstream_finish_reason='stop')
            if text:yield ProviderStreamEvent(delta=text,**meta)
        else:
            if reason not in _REASONS:raise ValueError
            yield failure(_REASONS[reason],transient=reason in {'aborted','insufficient_system_resource'},delta=text,usage=usage,cached_input_tokens=observed.get('cached_input_tokens'),billing_observed_at=observed.get('billing_observed_at'),upstream_terminal_received=True,upstream_finish_reason=reason,**meta)
            return
    raise ValueError


_LIFECYCLE={'response.created','response.in_progress','response.output_text.done',
    'response.output_item.added','response.output_item.done','response.content_part.added',
    'response.content_part.done','response.reasoning_text.delta','response.reasoning_text.done',
    'response.reasoning_summary_text.delta','response.reasoning_summary_text.done',
    'response.reasoning_summary_part.added','response.reasoning_summary_part.done'}


async def responses_events(lines, observed, *, allow_pending_metadata=False):
    refusal_seen = False
    async for name,data in frames(lines):
        raw=loads(data)
        if not isinstance(raw,dict):raise ValueError
        kind=raw.get('type')
        if name is not None and name!=kind:raise ValueError
        if kind=='response.output_text.delta':
            if not isinstance(raw.get('delta'),str):raise ValueError
            yield ProviderStreamEvent(delta=raw['delta'])
        elif kind in {'response.refusal.delta', 'response.refusal.done'}:
            text = raw.get('delta' if kind.endswith('.delta') else 'refusal')
            if not isinstance(text, str):raise ValueError
            refusal_seen = True
        elif kind in {'response.completed','response.failed','response.incomplete'}:
            body=raw.get('response')
            if not isinstance(body,dict) or body.get('object')!='response':raise ValueError
            if kind!='response.'+str(body.get('status')):
                observe_reported_metadata(body, observed, chat=False)
                raise ValueError
            try:result=parse_responses_response(body)
            except ProviderCallError as error:
                if error.code=='provider_invalid_response':
                    if error.actual_model is not None:
                        observed['actual_model']=error.actual_model
                    if error.usage is not None:
                        observed['usage']=error.usage
                        observed['cached_input_tokens']=error.cached_input_tokens
                        observed['billing_observed_at']=error.billing_observed_at
                    raise ValueError from None
                yield failure(error.code,transient=error.transient,actual_model=error.actual_model,usage=error.usage, cached_input_tokens=error.cached_input_tokens, billing_observed_at=error.billing_observed_at,
                              upstream_status=error.upstream_status,upstream_terminal_received=True)
                return
            meta={'actual_model':result.actual_model,'usage':result.usage,'cached_input_tokens':result.cached_input_tokens,'billing_observed_at':result.billing_observed_at,'upstream_status':result.upstream_status,
                  'upstream_finish_reason':result.upstream_finish_reason,'upstream_terminal_received':True}
            if refusal_seen:yield failure('provider_refused', **meta)
            elif result.finish_reason=='stop':yield ProviderStreamEvent(status='completed',**meta)
            elif result.finish_reason in _REASONS:yield failure(_REASONS[result.finish_reason],**meta)
            else:raise ValueError
            return
        elif kind in _LIFECYCLE:
            if kind in {'response.created','response.in_progress'}:
                body=raw.get('response')
                states={'in_progress'}
                if allow_pending_metadata and kind=='response.created':states.add('queued')
                if not isinstance(body,dict) or body.get('object')!='response' or body.get('status') not in states:raise ValueError
                model = metadata_text(body.get('model'))
                if model is None and not (allow_pending_metadata and body.get('model') in (None,'')):raise ValueError
                observed['upstream_status']=body['status']
                if model is not None:observed['actual_model']=model
            item=raw.get('item') or raw.get('part')
            if item is not None and (not isinstance(item,dict) or item.get('type') not in {'message','reasoning','output_text','reasoning_text','summary_text','refusal'}):raise ValueError
            if isinstance(item,dict) and item.get('type')=='refusal':refusal_seen=True
        else:raise ValueError
    raise ValueError
