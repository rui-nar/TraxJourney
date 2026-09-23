from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# disable_existing_loggers=False is critical: `alembic upgrade head` runs inside
# the live API process (lifespan), and the default (True) would DISABLE the app's
# own `api.*`/`src.*` loggers configured at import — silently killing all app logs
# in production (the long-standing "no logs on the NAS" bug).
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Import all SQLModel table models so they register with the shared metadata
# before autogenerate inspects it.
import sqlmodel  # noqa: F401
from models.billing import Subscription, UserUsage  # noqa: F401
from models.user import LocalUser, StravaToken, UserInfo  # noqa: F401
from models.project_db import (  # noqa: F401
    DBActivity,
    DBActivityGeoPrepared,
    DBDeviceKey,
    DBJournalEntry,
    DBMemory,
    DBProject,
    DBProjectItem,
    DBProjectSyncMeta,
    DBRecoveryWrap,
    DBShareVisit,
    DBStravaCache,
)

target_metadata = sqlmodel.SQLModel.metadata

# Always resolve the URL the way the app does (models/db_url.py): DATABASE_URL
# wins, otherwise the shared default. Never fall back to alembic.ini's value —
# entrypoint.sh migrates before uvicorn starts, so this is where an install
# still sitting on the old default database file must be refused.
from models.db_url import resolve_database_url  # noqa: E402

config.set_main_option("sqlalchemy.url", resolve_database_url())

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
