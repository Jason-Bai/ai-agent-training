import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import Field
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, DisconnectionError, TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from errors import GatewayError
from constants import DATABASE_POOL_SIZE, DATABASE_MAX_OVERFLOW, DATABASE_OPERATION_TIMEOUT_SECONDS
from services.models import Definition, ConfigurationError, read_yaml

REVISION = 'f9_001'


class DatabaseConfig(Definition):
    host: str
    port: int = Field(ge=1, le=65535)
    name: str
    username: str
    password_file: str


def load_database_config(config_dir):
    try:
        return DatabaseConfig.model_validate(read_yaml(Path(config_dir) / 'gateway.yaml')['database'])
    except (KeyError, ValueError, TypeError):
        raise ConfigurationError('database 配置不合法：请检查必需字段和类型') from None


def database_url(config, config_dir):
    try:
        password = (Path(config_dir) / config.password_file).read_bytes().decode('utf-8').rstrip('\n')
        if not password.strip():
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError('数据库密码文件无法读取或为空') from None
    return URL.create('postgresql+psycopg', username=config.username, password=password,
                      host=config.host, port=config.port, database=config.name)


class StorageUnavailable(GatewayError):
    def __init__(self, *, uncertain=False):
        super().__init__('storage_unavailable', '持久化存储暂时不可用', 503)
        self.uncertain = uncertain


class Database:
    def __init__(self, config, config_dir):
        self.engine = create_async_engine(database_url(config, config_dir), pool_size=DATABASE_POOL_SIZE,
                                          max_overflow=DATABASE_MAX_OVERFLOW,
                                          pool_timeout=DATABASE_OPERATION_TIMEOUT_SECONDS, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self, *, deadline=None):
        limit = asyncio.get_running_loop().time() + DATABASE_OPERATION_TIMEOUT_SECONDS
        if deadline is not None:
            limit = min(limit, deadline)
        committing = False
        try:
            async with asyncio.timeout_at(limit):
                async with self.sessions() as session:
                    async with session.begin():
                        yield session
                        committing = True
        # External cancellation propagates, including during commit. The caller
        # must reconcile the stopped request rather than infer its commit result.
        # asyncio.timeout_at converts only its own cancellation to TimeoutError.
        except (DBAPIError, DisconnectionError, PoolTimeoutError, OSError, TimeoutError):
            raise StorageUnavailable(uncertain=committing) from None

    async def check(self):
        async with self.transaction() as session:
            revision = await session.scalar(text('SELECT version_num FROM alembic_version'))
            if revision != REVISION:
                raise StorageUnavailable()
            await session.execute(text('SELECT id FROM tb_llm_request LIMIT 0'))
            await session.execute(text('SELECT request_pk FROM tb_llm_attempt LIMIT 0'))
            await session.execute(text('SELECT request_pk FROM tb_llm_stream_event LIMIT 0'))

    async def start(self):
        await self.check()

    async def close(self):
        await self.engine.dispose()
