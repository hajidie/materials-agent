"""Fixed Resource API transport. Independent of MCP and the ML engine."""
from hashlib import sha256
import json
import re
from time import monotonic
from tempfile import SpooledTemporaryFile
from urllib.parse import quote, urlsplit

import httpx
import asyncio

from materialsagent.application.errors import ApplicationConflictError, ApplicationValidationError, DependencyUnavailableError, ResourceNotFoundError
from materialsagent.domain.models.ml_resource import IDENTITY_VERSION, RESOURCE_TYPES

PATHS = {"dataset": "datasets", "training_run": "training-runs", "model": "models", "prediction": "predictions"}
MEMBERS = {"dataset.csv": 20 * 1024**2, "predictions.json": 2 * 1024**2,
    "manifest.json": 32 * 1024**2, "evaluation.json": 32 * 1024**2,
    "splits.json": 32 * 1024**2, "pipeline.joblib": 256 * 1024**2}


def strict_constant(_):
    raise ValueError("Non-finite JSON")


def response_error(status, payload):
    code = "ML_RESOURCE_UNAVAILABLE"
    try:
        candidate = json.loads(payload).get("error", {}).get("code", "")
        if status < 500 and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", candidate):
            code = candidate
    except (ValueError, AttributeError, TypeError):
        pass
    if status == 404:
        raise ResourceNotFoundError()
    if status in (413, 422):
        raise ApplicationValidationError(code=code, status_code=status)
    if status in (409, 410):
        raise ApplicationConflictError(code=code)
    raise DependencyUnavailableError(code=code)


class MLResourceClient:
    def __init__(self, url, token, binding_version):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.path != "/mcp" or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("Invalid ML Resource endpoint")
        self.base = url[:-4] + "/"
        self.token = token
        self.service = {"service_id": "materials_ml", "binding_version": binding_version,
                        "endpoint_digest": sha256(url.encode()).hexdigest()}

    def request(self, scope, suffix, *, method="GET", body=None, files=None, headers=None, artifact=None):
        # suffixes are assembled solely by the methods below, never taken as caller URLs.
        url = self.base + "api/v1/scopes/" + quote(scope, safe="") + "/" + suffix
        spool = None
        from materialsagent.application.ml_resource_context import resource_read_deadline
        deadline = resource_read_deadline.get()
        if deadline is not None:
            deadline = min(deadline, monotonic() + 5)
            if deadline <= monotonic():
                raise DependencyUnavailableError(code="ML_RESOURCE_READ_TIMEOUT")
            if method == "GET" and artifact is None:
                return self.bounded_read(url, deadline)
        try:
            timeout = max(0.001, deadline - monotonic()) if deadline else 60
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(5, timeout)), follow_redirects=False, trust_env=False) as client:
                with client.stream(method, url, headers={"Authorization": "Bearer " + self.token, **(headers or {})},
                        json=body, files=files) as response:
                    maximum = artifact["size_bytes"] if artifact else 1024 * 1024
                    if not 200 <= response.status_code < 300:
                        code = "ML_RESOURCE_UNAVAILABLE"
                        if response.status_code < 500:
                            payload = bytearray()
                            for chunk in response.iter_bytes():
                                payload.extend(chunk)
                                if len(payload) > 65536:
                                    break
                            try:
                                candidate = json.loads(payload).get("error", {}).get("code", "")
                                if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", candidate):
                                    code = candidate
                            except (ValueError, AttributeError, TypeError):
                                pass
                        if response.status_code == 404:
                            raise ResourceNotFoundError()
                        if response.status_code in (413, 422):
                            raise ApplicationValidationError(code=code, status_code=response.status_code)
                        if response.status_code in (409, 410):
                            raise ApplicationConflictError(code=code)
                        raise DependencyUnavailableError(code=code)
                    spool = SpooledTemporaryFile(max_size=1024 * 1024)
                    checksum, size = sha256(), 0
                    for chunk in response.iter_bytes():
                        if deadline is not None and monotonic() >= deadline:
                            raise DependencyUnavailableError(code="ML_RESOURCE_READ_TIMEOUT")
                        size += len(chunk)
                        if size > maximum:
                            raise DependencyUnavailableError(code="ML_RESOURCE_TOO_LARGE")
                        checksum.update(chunk); spool.write(chunk)
                    spool.seek(0)
                    if artifact:
                        media = response.headers.get("content-type", "").split(";")[0]
                        if size != maximum or checksum.hexdigest() != artifact["sha256"] or media != artifact["media_type"]:
                            raise DependencyUnavailableError(code="ML_RESOURCE_INTEGRITY_MISMATCH")
                        result, spool = spool, None
                        return result
                    value = json.loads(spool.read(), parse_constant=strict_constant)
                    if not isinstance(value, dict):
                        raise ValueError("Expected resource response object")
                    return value
        except (httpx.HTTPError, ValueError, TypeError, OSError):
            raise DependencyUnavailableError(code="ML_RESOURCE_UNAVAILABLE") from None
        finally:
            if spool is not None:
                spool.close()

    def bounded_read(self, url, deadline):
        """Absolute cancellation deadline includes connect, headers and the entire body."""
        async def read():
            async with asyncio.timeout(max(0, deadline - monotonic())):
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=5) as client:
                    async with client.stream("GET", url, headers={"Authorization": "Bearer " + self.token}) as response:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > (1024 * 1024 if 200 <= response.status_code < 300 else 65536):
                                raise DependencyUnavailableError(code="ML_RESOURCE_TOO_LARGE")
                        if not 200 <= response.status_code < 300:
                            response_error(response.status_code, body)
                        value = json.loads(body, parse_constant=strict_constant)
                        if not isinstance(value, dict):
                            raise ValueError("Expected resource response object")
                        return value
        try:
            return asyncio.run(read())
        except (TimeoutError, httpx.TimeoutException):
            raise DependencyUnavailableError(code="ML_RESOURCE_READ_TIMEOUT") from None
        except (httpx.HTTPError, ValueError, TypeError, OSError):
            raise DependencyUnavailableError(code="ML_RESOURCE_UNAVAILABLE") from None

    def descriptor(self, scope, kind, identity, version=IDENTITY_VERSION):
        if kind not in RESOURCE_TYPES:
            raise ResourceNotFoundError()
        value = self.request(scope, f"resource-identities/{kind}/{quote(identity, safe='')}?identity_contract_version={quote(version, safe='')}")
        if (not isinstance(value, dict) or value.get("scope_id") != scope or value.get("resource_type") != kind
                or value.get("resource_id") != identity or value.get("identity_contract_version") != version
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("remote_identity_digest", "")))
                or not isinstance(value.get("identity"), dict) or not isinstance(value.get("files"), dict)):
            raise DependencyUnavailableError(code="ML_RESOURCE_IDENTITY_MISMATCH")
        return value

    def prepare_upload(self, scope, payload, metadata):
        value = self.request(scope, "dataset-upload-identities/prepare", method="POST",
            files={"file": ("dataset.csv", payload, "text/csv"), "metadata": (None, json.dumps(metadata, allow_nan=False))})
        if (value.get("scope_id") != scope or value.get("operation") != "dataset.upload"
                or value.get("digest_version") != "dataset-upload-v1" or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("request_digest", "")))):
            raise DependencyUnavailableError(code="ML_UPLOAD_IDENTITY_INVALID")
        return value

    def upload(self, scope, operation, payload, metadata):
        return self.request(scope, "datasets", method="POST", headers={"Idempotency-Key": operation["idempotency_key"],
            "X-ML-Expected-Request-Digest": operation["request_digest"]},
            files={"file": ("dataset.csv", payload, "text/csv"), "metadata": (None, json.dumps(metadata, allow_nan=False))})

    def lookup(self, scope, operation):
        return self.request(scope, "operation-receipts/lookup", method="POST", body={k: operation[k] for k in
            ("operation", "idempotency_key", "request_digest", "digest_version")})

    def resource(self, scope, kind, identity, *, action=None):
        path = PATHS[kind] + "/" + quote(identity, safe="")
        if action == "cancel" and kind in ("training_run", "prediction"):
            return self.request(scope, path + "/cancel", method="POST")
        if action == "delete" and kind == "dataset":
            return self.request(scope, path, method="DELETE")
        if action:
            raise ResourceNotFoundError()
        return self.request(scope, path)

    def download(self, scope, descriptor, member):
        item = descriptor["files"].get(member)
        if member not in MEMBERS or not item:
            raise ResourceNotFoundError()
        if (type(item.get("size_bytes")) is not int or not 0 <= item["size_bytes"] <= MEMBERS[member]
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                or item.get("media_type") != ("text/csv" if member == "dataset.csv" else
                    "application/octet-stream" if member == "pipeline.joblib" else "application/json")):
            raise DependencyUnavailableError(code="ML_RESOURCE_INTEGRITY_MISMATCH")
        kind = descriptor["resource_type"]
        if (kind == "dataset" and member != "dataset.csv") or (kind == "prediction" and member != "predictions.json"):
            raise ResourceNotFoundError()
        path = PATHS[kind] + "/" + quote(descriptor["resource_id"], safe="")
        path += "/files/" + member if kind == "model" else "/content"
        return self.request(scope, path, artifact=item), item["media_type"]

    def close_scope(self, scope, operation_id, *, lookup=False):
        value = self.request(scope, "close-operations" + ("/" + operation_id if lookup else ""),
            method="GET" if lookup else "POST", body=None if lookup else {"operation_id": operation_id})
        if (value.get("scope_id") != scope or value.get("operation_id") != operation_id
                or value.get("status") not in ("NOT_FOUND", "BUSY", "CLOSED")
                or (value["status"] == "CLOSED" and value.get("cleanup_accepted") is not True)):
            raise DependencyUnavailableError(code="ML_CLOSE_RECEIPT_INVALID")
        return value
