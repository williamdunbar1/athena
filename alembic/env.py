"""Ambiente Alembic dinamico apontando para o SQLite do `memoria.py` (ou `ATHENA_DB_PATH`)."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# Evita sobrescrever logging quando executado programaticamente via `executar_migrations()`
if config.config_file_name is not None and not config.attributes.get("programmatic", False):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def _resolver_url_sqlite() -> str:
    url_attr = config.attributes.get("db_url")
    if isinstance(url_attr, str) and url_attr.strip():
        return url_attr.strip()

    env_db = (os.getenv("ATHENA_DB_PATH") or "").strip()
    if env_db:
        return f"sqlite:///{Path(env_db).resolve().as_posix()}"

    url_cfg = (config.get_main_option("sqlalchemy.url") or "").strip()
    if url_cfg and url_cfg != "sqlite:///memoria.db":
        return url_cfg

    import memoria

    return f"sqlite:///{memoria.obter_db_path().resolve().as_posix()}"


def run_migrations_offline() -> None:
    """Executa migrations em modo offline (gera SQL sem conectar)."""
    url = _resolver_url_sqlite()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Executa migrations em modo online conectando ao SQLite alvo."""
    url = _resolver_url_sqlite()
    section = dict(config.get_section(config.config_ini_section, {}) or {})
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
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
