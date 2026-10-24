from logging.config import fileConfig
from alembic import context
from sqlalchemy import engine_from_config, pool
from app.models import Base
from app.config import settings

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata

def include_object(_object, name, type_, _reflected, _compare_to):
    # The expression GIN index is maintained explicitly in migrations.
    return not (type_ == "index" and name == "ix_chunks_search")

def run_migrations_offline():
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    engine = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
