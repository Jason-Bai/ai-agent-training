"""Single-worker request permits, held across all Provider attempts and cleanup."""
import logging

from starlette import status as http_status

from errors import GatewayError


class RequestPermit:
    def __init__(self, limiter, agent_id, request_id):
        self._limiter, self._agent_id, self._request_id = limiter, agent_id, request_id
        self._released = False
        self._pending = set()

    def release(self):
        if self._released:
            return
        if self._pending:
            raise RuntimeError('Request cleanup is still active')
        self._limiter._release(self)
        self._released = True

    def release_when_done(self, tasks):
        if self._released:
            return
        for task in set(tasks):
            if task.done():
                if not task.cancelled():
                    task.exception()
            elif task not in self._pending:
                self._pending.add(task)
                task.add_done_callback(self._task_done)
        if not self._pending:
            self.release()

    def _task_done(self, task):
        if not task.cancelled():
            task.exception()
        self._pending.discard(task)
        if not self._pending:
            self.release()


class ConcurrencyLimiter:
    def __init__(self, policy):
        self._max_requests = policy.max_requests
        self._max_per_agent = policy.max_requests_per_agent
        self._requests = {}
        self._agents = {}

    def acquire(self, agent_id, request_id):
        # No suspension between checks and mutations: one worker's event loop
        # cannot admit two owners against the same last available slot.
        if request_id in self._requests:
            raise RuntimeError('Request already owns a concurrency permit')
        if len(self._requests) >= self._max_requests:
            self._reject('global', self._max_requests, len(self._requests), request_id)
        agent_count = self._agents.get(agent_id, 0)
        if agent_count >= self._max_per_agent:
            self._reject('agent', self._max_per_agent, agent_count, request_id)
        permit = RequestPermit(self, agent_id, request_id)
        self._requests[request_id] = permit
        self._agents[agent_id] = agent_count + 1
        return permit

    @staticmethod
    def _reject(scope, limit, in_use, request_id):
        logging.getLogger(__name__).warning(
            'Concurrency rejected request_id=%s scope=%s limit=%s in_use=%s',
            request_id, scope, limit, in_use)
        raise GatewayError('concurrency_limited', '当前模型调用并发已满，请稍后重试',
                           http_status.HTTP_429_TOO_MANY_REQUESTS)

    def _release(self, permit):
        if self._requests.get(permit._request_id) is not permit:
            raise RuntimeError('Concurrency permit owner does not match')
        del self._requests[permit._request_id]
        count = self._agents[permit._agent_id] - 1
        if count:
            self._agents[permit._agent_id] = count
        else:
            del self._agents[permit._agent_id]

    def snapshot(self):
        return {'active_requests': len(self._requests), 'agents': dict(self._agents)}
