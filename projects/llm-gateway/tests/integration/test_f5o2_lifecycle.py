"""Lifespan cleanup when an acknowledged claim remains unowned and PG is offline."""
from uuid import uuid4

from test_f5o2_http import server, case, make_app
from persistence.database import StorageUnavailable


async def test_database_pool_closes_even_when_orphan_reconciliation_is_unavailable(case,monkeypatch):
    _,config=case;app=make_app(config);closed=[]
    database=app.state.storage.database;original=database.close
    async def close():
        closed.append(True)
        await original()
    async def unavailable(request_id):
        raise StorageUnavailable()
    monkeypatch.setattr(database,'close',close)
    async with app.router.lifespan_context(app):
        app.state.streaming_manager.orphans.add(str(uuid4()))
        monkeypatch.setattr(app.state.storage,'reconcile',unavailable)
    assert closed==[True]
