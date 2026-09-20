from alembic import context
from sqlalchemy import create_engine, pool
from materials_ml_service.config import load_settings
from materials_ml_service.infrastructure.database import metadata

config = context.config
url = config.attributes.get("database_url")
if url is None:
    url = load_settings().database_url.get_secret_value()

if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True,
                      version_table="ml_alembic_version")
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, version_table="ml_alembic_version")
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
