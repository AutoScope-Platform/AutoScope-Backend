"""Object storage drivers: local disk (development) and S3-compatible (staging/production).

Photos, KYC documents and claim evidence live in object storage. The database holds
only the object *key*; clients get a time-limited URL from `url()`.

The S3 driver works with any S3-compatible service (Cloudflare R2, Neon storage,
AWS S3, MinIO): set `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID` and
`S3_SECRET_ACCESS_KEY`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from pathlib import Path

from app.core.config import Settings
from app.integrations.base import IntegrationError


def _safe_path(root: Path, key: str) -> Path:
    """Resolve `key` under `root`, refusing anything that escapes it (path traversal)."""
    path = (root / key).resolve()
    if root.resolve() not in path.parents and path != root.resolve():
        raise IntegrationError("Invalid storage key")
    return path


class LocalStorage:
    """Stores files under a local folder. Development and tests only: Render's disk is
    wiped on every deploy, so staging and production must use the S3 driver."""

    def __init__(self, root: str, signing_secret: str) -> None:
        self.root = Path(root)
        self._secret = signing_secret.encode()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = _safe_path(self.root, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return _safe_path(self.root, key).read_bytes()

    def url(self, key: str, expires_seconds: int = 900) -> str:
        expires = int(time.time()) + expires_seconds
        sig = hmac.new(self._secret, f"{key}:{expires}".encode(), hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return f"/api/v1/files/{key}?expires={expires}&sig={token}"

    def delete(self, key: str) -> None:
        _safe_path(self.root, key).unlink(missing_ok=True)


class S3Storage:
    """S3-compatible storage through boto3 (imported lazily so local runs do not need it)."""

    def __init__(self, settings: Settings) -> None:
        import boto3  # noqa: PLC0415

        if not (settings.s3_bucket and settings.s3_access_key_id and settings.s3_secret_access_key):
            raise IntegrationError("S3 storage is not configured (see docs/INTEGRATIONS.md)")
        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def url(self, key: str, expires_seconds: int = 900) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_seconds
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)
