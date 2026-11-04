"""Test-side SDK/httpcore2 evidence; never serialize transport ``info``.

Missing stages are unobserved, not inferred failures or proof the supplier did
not receive a call. Invocation outcomes are local observations, not billing facts.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
from pathlib import Path
from time import monotonic
from uuid import UUID


STAGES = {'connect_tcp':'connect', 'connect_unix_socket':'connect', 'start_tls':'tls',
          'send_request_headers':'send_headers', 'send_request_body':'send_body',
          'receive_response_headers':'receive_headers', 'receive_response_body':'receive_body',
          'response_closed':'response_close'}


def install(app,directory,monkeypatch):
    """Attach transport observation after the application's trusted admission."""
    directory = Path(directory)
    current = ContextVar('transport_observation',default=None)
    requests = {}
    errors = []
    app.state.transport_observation_errors = errors

    def flush(context):
        if context is None:
            return
        rid,sample,_ = context
        path = directory/('transport-'+rid+'.json')
        try:
            value = dict(request_id=rid,attempts=[requests[rid][number] for number in sorted(requests[rid])])
            staging = path.with_suffix('.tmp')
            staging.write_text(json.dumps(value))
            staging.replace(path)
        except (OSError,ValueError,TypeError) as exc:
            errors.append(dict(request_id=rid,attempt_no=sample['attempt_no'],error_type=type(exc).__name__))

    original_begin = app.state.storage.begin_attempt
    async def begin(trace,attempt,**options):
        await original_begin(trace,attempt,**options)
        rid = str(UUID(str(trace.request_id)))
        sample = dict(attempt_no=attempt.number,events=[],observed_stages=[],
            invocation_status='registered',invocation_exception_type=None,call_cancelled=False)
        requests.setdefault(rid,{})[attempt.number] = sample
        context = (rid,sample,monotonic())
        current.set(context)
        flush(context)
    monkeypatch.setattr(app.state.storage,'begin_attempt',begin)

    def finish(context,status,error=None):
        if context is not None:
            _,sample,_ = context
            sample.update(invocation_status=status,
                invocation_exception_type=type(error).__name__ if error is not None else None,
                call_cancelled=isinstance(error,asyncio.CancelledError))
            flush(context)

    def wrap(provider):
        sdk = getattr(provider,'client',None)
        client = getattr(sdk,'_client',sdk)
        if client is None or not hasattr(client,'event_hooks'):
            return

        async def request_hook(request):
            context = current.get()
            if context is None:
                return
            previous = request.extensions.get('trace')
            async def observed(event,info):
                _,sample,started = context
                error = info.get('exception')
                sample['events'].append(dict(event=event,elapsed_ms=(monotonic()-started)*1000,
                    exception_type=type(error).__name__ if error is not None else None))
                stage = STAGES.get(event.split('.')[-2])
                if stage is not None and stage not in sample['observed_stages']:
                    sample['observed_stages'].append(stage)
                flush(context)
                if previous is not None:
                    await previous(event,info)
            request.extensions['trace'] = observed
        client.event_hooks.setdefault('request',[]).append(request_hook)
        complete,stream = provider.complete,provider.stream
        async def observed_complete(*args,**options):
            context = current.get()
            try:
                result = await complete(*args,**options)
            except asyncio.CancelledError as exc:
                finish(context,'cancelled',exc)
                raise
            except BaseException as exc:
                finish(context,'raised',exc)
                raise
            finish(context,'returned')
            return result
        async def observed_stream(*args,**options):
            context = current.get(); iterator = stream(*args,**options)
            try:
                async for event in iterator:
                    yield event
            except asyncio.CancelledError as exc:
                finish(context,'cancelled',exc)
                raise
            except GeneratorExit as exc:
                finish(context,'closed',exc)
                raise
            except BaseException as exc:
                finish(context,'raised',exc)
                raise
            else:
                finish(context,'returned')
            finally:
                await iterator.aclose()
                flush(context)
        monkeypatch.setattr(provider,'complete',observed_complete)
        monkeypatch.setattr(provider,'stream',observed_stream)
        return client,request_hook

    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            attached = [wrap(provider) for provider in {
                id(value):value for value in application.state.providers.values()}.values()]
            try:
                yield
            finally:
                for entry in attached:
                    if entry is not None:
                        client,hook = entry
                        client.event_hooks['request'].remove(hook)
    monkeypatch.setattr(app.router,'lifespan_context',lifespan)
