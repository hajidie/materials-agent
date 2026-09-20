"""Explicit integration fixtures: isolated database/role/bucket on the existing instances."""
from pathlib import Path
import os
import re
import secrets

from dotenv import dotenv_values
from minio import Minio
from minio.credentials import StaticProvider
from minio.minioadmin import MinioAdmin
import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import URL

from materials_ml_service.admin import provision, migrate
from materials_ml_service.application import MLService
from materials_ml_service.config import Settings
from materials_ml_service.infrastructure.database import PostgresRepository
from materials_ml_service.infrastructure.storage import MinioStorage


@pytest.fixture(scope="session")
def integration_settings(request):
    if os.environ.get("ML_INTEGRATION") != "1":
        pytest.skip("Set ML_INTEGRATION=1 to run against real PostgreSQL/MinIO")
    root = Path(__file__).resolve().parents[4]
    values = dotenv_values(root / ".env")
    admin_url = URL.create("postgresql+psycopg", username=values["POSTGRES_USER"], password=values["POSTGRES_PASSWORD"],
                          host=values.get("POSTGRES_HOST", "127.0.0.1"), port=int(values.get("POSTGRES_PORT", 5432)), database="postgres")
    name = "materials_ml_" + secrets.token_hex(8)
    assert re.fullmatch(r"materials_ml_[0-9a-f]{16}", name)
    url = admin_url.set(username=name, password=secrets.token_urlsafe(32), database=name)
    settings = Settings(database_url=url.render_as_string(hide_password=False),
        minio_endpoint=values.get("MINIO_ENDPOINT", "127.0.0.1:9000").removeprefix("http://"), minio_bucket=name.replace("_", "-"),
        minio_access_key=name, minio_secret_key=secrets.token_urlsafe(32),
        resource_token=secrets.token_urlsafe(32), worker_token=secrets.token_urlsafe(32), mcp_token=secrets.token_urlsafe(32))
    def cleanup():
        # Exact generated identities only, including a partially completed fixture setup.
        client = Minio(settings.minio_endpoint, access_key=values["MINIO_ACCESS_KEY"],
                       secret_key=values["MINIO_SECRET_KEY"], secure=False)
        if client.bucket_exists(settings.minio_bucket):
            for item in client.list_objects(settings.minio_bucket, recursive=True):
                client.remove_object(settings.minio_bucket, item.object_name)
            client.remove_bucket(settings.minio_bucket)
        admin = MinioAdmin(settings.minio_endpoint, StaticProvider(values["MINIO_ACCESS_KEY"], values["MINIO_SECRET_KEY"]), secure=False)
        admin.user_remove(name)
        try:
            admin.policy_remove(name)
        finally:
            with psycopg.connect(host=admin_url.host, port=admin_url.port, user=admin_url.username,
                                 password=admin_url.password, dbname="postgres", autocommit=True) as connection:
                connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
                connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
    request.addfinalizer(cleanup)
    provision(settings, admin_database_url=admin_url.render_as_string(hide_password=False),
              admin_access_key=values["MINIO_ACCESS_KEY"], admin_secret_key=values["MINIO_SECRET_KEY"])
    migrate(url.render_as_string(hide_password=False))
    return settings


@pytest.fixture
def service(integration_settings):
    repo = PostgresRepository(integration_settings.database_url.get_secret_value())
    storage = MinioStorage(integration_settings)
    yield MLService(repo, storage, integration_settings)
    from sqlalchemy import text
    with repo.engine.begin() as connection:
        connection.execute(text("TRUNCATE ml_scope_close, ml_scope, ml_operation, ml_model, ml_artifact, ml_training_run, ml_dataset CASCADE"))
    for item in storage.client.list_objects(integration_settings.minio_bucket, prefix="ml/v1/", recursive=True):
        storage.client.remove_object(integration_settings.minio_bucket, item.object_name)
    storage.close(); repo.close()


@pytest.fixture
def csv_payload(table):
    return table.to_csv(index=False).encode("utf-8")
