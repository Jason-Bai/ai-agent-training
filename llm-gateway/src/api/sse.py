"""SSE framing and actual sends; generation remains application-owned."""
import asyncio
import logging

from starlette.responses import Response

from utils.json_codec import dumps


def encode_event(name, data, seq=None):
    identifier = '' if seq is None else f'id: {seq}\n'
    return f'{identifier}event: {name}\ndata: {dumps(data)}\n\n'.encode('utf-8')


class EventStreamResponse(Response):
    media_type = 'text/event-stream'

    def __init__(self, subscription, status_code: int = 200, *, owner=None):
        super().__init__(status_code=status_code, headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
        del self.headers['content-length']
        self.subscription, self.owner = subscription, owner

    async def __call__(self, scope, receive, send):
        disconnected = asyncio.Event()
        subscription = self.subscription
        current = asyncio.current_task()
        if self.owner is not None:
            self.owner.subscriptions.add(current)

        async def watch_disconnect():
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    disconnected.set()
                    return

        async def start():
            await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})

        async def emit(name, data, seq=None):
            await send({'type': 'http.response.body', 'body': encode_event(name, data, seq), 'more_body': True})

        watcher = asyncio.create_task(watch_disconnect())
        try:
            await subscription.run(start, emit, disconnected)
            if subscription.started_response and not disconnected.is_set() and subscription.stopped_reason in {None, 'storage_unavailable'}:
                async with asyncio.timeout(subscription.send_timeout_seconds):
                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        except (OSError, TimeoutError) as exc:
            logging.getLogger(__name__).warning('订阅传输结束 request_id=%s code=%s', subscription.request_id,
                'send_timeout' if isinstance(exc, TimeoutError) else 'client_disconnected')
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if self.owner is not None:
                self.owner.subscriptions.discard(current)
