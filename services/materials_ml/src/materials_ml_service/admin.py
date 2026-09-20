"""Explicit initialization/migration; never invoked from API startup or Worker."""
import argparse
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from minio import Minio
from minio.credentials import StaticProvider
from minio.minioadmin import MinioAdmin
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from .config import load_settings


def migrate(url, target="head"):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")).replace("%", "%%"))
    config.attributes["database_url"] = url
    command.upgrade(config, target)


def provision(settings, *, admin_database_url, admin_access_key, admin_secret_key):
    """Creates only new, explicitly ML-named resources; refuses existing identities."""
    url = make_url(settings.database_url.get_secret_value())
    database, username, bucket = url.database, url.username, settings.minio_bucket
    if (not re.fullmatch(r"materials_ml(?:_[0-9a-f]{16})?", database or "")
            or username != database or bucket != database.replace("_", "-")):
        raise ValueError("Explicit ML database, role and bucket names must correspond")
    admin_url = make_url(admin_database_url)
    client = Minio(settings.minio_endpoint, access_key=admin_access_key, secret_key=admin_secret_key,
                   secure=settings.minio_secure)
    if client.bucket_exists(bucket):
        raise ValueError("ML bucket already exists; provisioning refuses overwrite")
    access = settings.minio_access_key.get_secret_value()
    if access != username:
        raise ValueError("ML access key must use the explicitly provisioned role name")
    admin = MinioAdmin(settings.minio_endpoint, StaticProvider(admin_access_key, admin_secret_key),
                       secure=settings.minio_secure)
    if access in json.loads(admin.user_list()) or username in json.loads(admin.policy_list()):
        raise ValueError("ML storage identity already exists; provisioning refuses overwrite")
    with psycopg.connect(host=admin_url.host, port=admin_url.port or 5432, dbname=admin_url.database or "postgres",
                        user=admin_url.username, password=admin_url.password, autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            raise ValueError("ML database already exists; provisioning refuses overwrite")
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (username,)).fetchone():
            raise ValueError("ML role already exists; provisioning refuses overwrite")
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
            sql.Identifier(username), sql.Literal(url.password)))
        connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(username)))
        connection.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
    client.make_bucket(bucket)
    admin.user_add(access, settings.minio_secret_key.get_secret_value())
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetBucketLocation", "s3:ListBucket"], "Resource": [f"arn:aws:s3:::{bucket}"]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload",
                                           "s3:ListMultipartUploadParts"], "Resource": [f"arn:aws:s3:::{bucket}/ml/v1/*"]}]}
    with TemporaryDirectory(prefix="ml-policy-") as folder:
        path = Path(folder) / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        admin.policy_add(username, str(path))
    admin.policy_set(username, user=access)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("provision", "migrate", "maintain"))
    args = parser.parse_args()
    try:
        settings = load_settings()
        if args.action == "provision":
            provision(settings, admin_database_url=os.environ["ML_ADMIN_DATABASE_URL"],
                      admin_access_key=os.environ["ML_ADMIN_MINIO_ACCESS_KEY"],
                      admin_secret_key=os.environ["ML_ADMIN_MINIO_SECRET_KEY"])
        elif args.action == "migrate":
            migrate(settings.database_url.get_secret_value())
        else:
            from .application import MLService
            from .infrastructure.database import PostgresRepository
            from .infrastructure.storage import MinioStorage
            repository, storage = PostgresRepository(settings.database_url.get_secret_value()), MinioStorage(settings)
            try:
                MLService(repository, storage, settings).maintain()
            finally:
                storage.close(); repository.close()
    except Exception:
        raise SystemExit("ML_ADMIN_OPERATION_FAILED") from None
    print("ML_ADMIN_OK")


if __name__ == "__main__":
    main()
