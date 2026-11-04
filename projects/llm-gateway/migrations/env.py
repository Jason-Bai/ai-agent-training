from pathlib import Path
from alembic import context
from sqlalchemy import create_engine
from persistence.database import load_database_config, database_url
from persistence.tables import metadata

config = context.config
config_dir = Path(config.attributes.get('config_dir') or context.get_x_argument(as_dictionary=True).get('config_dir') or Path(__file__).resolve().parents[1] / 'config')
engine = create_engine(database_url(load_database_config(config_dir), config_dir), hide_parameters=True)
try:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
