import os
from alembic import context
from sqlalchemy import create_engine
from quantro.infrastructure.database.schema import metadata

url = os.environ.get("QUANTRO_DATABASE_URL")
if not url:
    raise RuntimeError("Set QUANTRO_DATABASE_URL before running migrations")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
