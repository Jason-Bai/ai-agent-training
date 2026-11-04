"""Startup, recovery and shutdown orchestration for one Gateway application."""
import asyncio
from contextlib import asynccontextmanager

from constants import DATABASE_RECONCILE_INTERVAL_SECONDS, DATABASE_SHUTDOWN_GRACE_SECONDS
from persistence.database import StorageUnavailable
from services.models import ConfigurationError


def application_lifespan(database, storage, clients, manager, reconciler):
    async def reconcile_pending():
        while True:
            try:
                await manager.reconcile()
            except StorageUnavailable:
                pass
            await reconciler.flush()
            await asyncio.sleep(DATABASE_RECONCILE_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(app):
        recovery = None
        try:
            try:
                await database.start()
                await storage.recover()
            except StorageUnavailable:
                raise ConfigurationError('数据库连接、结构版本或启动恢复检查失败') from None
            recovery = asyncio.create_task(reconcile_pending())
            async with clients.open() as instances:
                app.state.providers = instances
                manager.providers = instances
                try:
                    yield
                finally:
                    await manager.close()
        finally:
            if recovery is not None:
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
                try:
                    async with asyncio.timeout(DATABASE_SHUTDOWN_GRACE_SECONDS):
                        await manager.reconcile()
                        await reconciler.flush()
                except (TimeoutError, StorageUnavailable):
                    pass
            await database.close()

    return lifespan
