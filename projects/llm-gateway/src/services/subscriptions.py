"""Read committed events; one subscriber's delivery never owns generation."""
import asyncio
import logging
from time import monotonic

from constants import (STREAM_HEARTBEAT_SECONDS, SUBSCRIPTION_PAGE_SIZE,
                       SUBSCRIPTION_POLL_SECONDS, SUBSCRIPTION_SEND_TIMEOUT_SECONDS)
from persistence.database import StorageUnavailable
from utils.json_codec import loads


class EventSubscription:
    def __init__(self, store, request_id, after_seq, *, on_first_text=None,
                 poll_seconds=SUBSCRIPTION_POLL_SECONDS, page_size=SUBSCRIPTION_PAGE_SIZE,
                 heartbeat_seconds=STREAM_HEARTBEAT_SECONDS,
                 send_timeout_seconds=SUBSCRIPTION_SEND_TIMEOUT_SECONDS):
        self.store, self.request_id, self.after_seq = store, request_id, after_seq
        self.on_first_text = on_first_text
        self.poll_seconds, self.page_size = poll_seconds, page_size
        self.heartbeat_seconds, self.send_timeout_seconds = heartbeat_seconds, send_timeout_seconds
        self.started_response = False
        self.stopped_reason = None
        self.query = None

    async def run(self, start, send_event, disconnected):
        worker = asyncio.create_task(self._read(start, send_event))
        disconnect = asyncio.create_task(disconnected.wait())
        try:
            done, _ = await asyncio.wait({worker, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                self.stopped_reason = 'client_disconnected'
            else:
                await worker
        except TimeoutError:
            self.stopped_reason = 'send_timeout'
        except OSError:
            self.stopped_reason = 'client_disconnected'
        except StorageUnavailable:
            self.stopped_reason = 'storage_unavailable'
        except Exception:
            self.stopped_reason = 'internal_error'
        finally:
            tasks = [task for task in (worker, disconnect, self.query) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.stopped_reason:
                logging.getLogger(__name__).warning('订阅已结束 request_id=%s code=%s',
                                                    self.request_id, self.stopped_reason)

    async def _send(self, operation):
        async with asyncio.timeout(self.send_timeout_seconds):
            await operation
        self.last_sent_at = monotonic()

    def _heartbeat_delay(self):
        return max(0, self.heartbeat_seconds - (monotonic() - self.last_sent_at))

    async def _read(self, start, send_event):
        await self._send(start())
        self.started_response = True

        async def heartbeat():
            await self._send(send_event('response.heartbeat', {'request_id': self.request_id}, seq=None))

        while True:
            self.query = asyncio.create_task(self.store.read_events(
                self.request_id, self.after_seq, limit=self.page_size))
            while not self.query.done():
                done, _ = await asyncio.wait({self.query}, timeout=self._heartbeat_delay())
                if not done:
                    await heartbeat()
            row, events = self.query.result()
            self.query = None
            if row is None:
                self.stopped_reason = 'not_found'
                return
            for event in events:
                if self._heartbeat_delay() == 0:
                    await heartbeat()
                name = event['event_type']
                await self._send(send_event(name, loads(event['payload']), seq=event['seq']))
                self.after_seq = event['seq']
                if name == 'response.delta' and self.on_first_text is not None:
                    callback, self.on_first_text = self.on_first_text, None
                    callback(self.last_sent_at)
                if name in {'response.completed', 'response.failed', 'response.canceled'}:
                    return
            if row['status'] in {'completed', 'failed', 'canceled'} and self.after_seq >= row['last_event_seq']:
                return
            # A populated page may have more committed rows: never delay it.
            if not events:
                delay = min(self.poll_seconds, self._heartbeat_delay())
                if delay > 0:
                    await asyncio.sleep(delay)
                if self._heartbeat_delay() == 0:
                    await heartbeat()
