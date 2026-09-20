from hashlib import sha256
from io import BytesIO

from minio import Minio
from minio.error import S3Error
import urllib3

from materials_storage import ObjectStorageRef
from ..domain import ServiceError


class MinioStorage:
    def __init__(self, settings):
        self.bucket = settings.minio_bucket
        self.store_id = settings.store_id
        self.pool = urllib3.PoolManager(timeout=urllib3.Timeout(connect=2, read=30), retries=False)
        self.client = Minio(settings.minio_endpoint, access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(), secure=settings.minio_secure,
            http_client=self.pool)

    def _ref(self, value):
        try:
            ref = ObjectStorageRef(**value)
            if (ref.bucket != self.bucket or ref.store_id != self.store_id
                    or ref.version_id is not None or not ref.storage_key.startswith("ml/v1/")):
                raise ValueError()
            return ref
        except (ValueError, TypeError):
            raise ServiceError("STORAGE_IDENTITY_CONFLICT") from None

    def exists(self, value):
        ref = self._ref(value)
        try:
            stat = self.client.stat_object(ref.bucket, ref.storage_key)
            headers = {k.lower(): v for k, v in stat.metadata.items()}
            if (stat.size != ref.size_bytes or stat.content_type != ref.media_type
                    or headers.get("x-amz-meta-object-id") != ref.object_id
                    or headers.get("x-amz-meta-sha256") != ref.sha256):
                raise ServiceError("STORAGE_IDENTITY_CONFLICT")
            return True
        except S3Error as error:
            if error.code in ("NoSuchKey", "NoSuchObject", "NotFound"):
                return False
            raise ServiceError("STORAGE_UNAVAILABLE", 503) from None
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("STORAGE_UNAVAILABLE", 503) from None

    def put(self, value, payload):
        ref = self._ref(value)
        try:
            ref.verify(payload)
        except ValueError:
            raise ServiceError("ARTIFACT_INTEGRITY_MISMATCH") from None
        if self.exists(value):
            self.get(value)
            return
        try:
            self.client.put_object(ref.bucket, ref.storage_key, BytesIO(payload), len(payload),
                content_type=ref.media_type, metadata={"object-id": ref.object_id, "sha256": ref.sha256},
                num_parallel_uploads=1)
        except Exception:
            raise ServiceError("UPLOAD_OUTCOME_UNKNOWN", 503) from None
        self.get(value)

    def get(self, value):
        ref = self._ref(value)
        if not self.exists(value):
            raise ServiceError("OBJECT_NOT_FOUND", 404)
        response = None
        try:
            response = self.client.get_object(ref.bucket, ref.storage_key)
            payload = response.read(ref.size_bytes + 1)
            ref.verify(payload)
            return payload
        except ValueError:
            raise ServiceError("ARTIFACT_INTEGRITY_MISMATCH") from None
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("STORAGE_UNAVAILABLE", 503) from None
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def delete(self, value):
        ref = self._ref(value)
        if not self.exists(value):
            return
        self.get(value)  # Verify ownership and bytes before deleting a known object.
        try:
            self.client.remove_object(ref.bucket, ref.storage_key)
        except Exception:
            raise ServiceError("STORAGE_UNAVAILABLE", 503) from None

    def close(self):
        self.pool.clear()
