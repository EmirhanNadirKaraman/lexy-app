import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# .env lives at the project root (sentence-to-phrase-matcher/.env)
# env.py → migrations/ → backend/ → lexy-app/ → sentence-to-phrase-matcher/
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

# Make `database` importable regardless of how alembic is invoked (cwd may or
# may not be backend/). env.py → migrations/ → backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from database import resolve_sslmode  # noqa: E402  (after the sys.path insert)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models — migrations use op.execute() with raw SQL
target_metadata = None


def get_url() -> str:
    url = (
        f"postgresql+psycopg2://"
        f"{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT', '5432')}"
        f"/{os.getenv('DB_NAME')}"
    )
    # S4: enforce TLS when DB_SSL_MODE opts in. unset/disable → no param (libpq
    # default preserved); prefer/allow/invalid → resolve_sslmode raises.
    sslmode = resolve_sslmode(os.getenv("DB_SSL_MODE"))
    if sslmode:
        url += f"?sslmode={sslmode}"
    return url


def run_migrations_offline() -> None:
    context.configure(url=get_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", get_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
