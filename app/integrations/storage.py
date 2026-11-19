"""Object storage drivers: local disk (development) and S3-compatible (staging/production).

Photos, KYC documents and claim evidence live in object storage. The database holds
only the object *key*; clients get a time-limited URL from `url()`.

The S3 driver works with any S3-compatible service (Neon Object Storage, Cloudflare R2,
AWS S3, MinIO): set `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY_ID` and
`S3_SECRET_ACCESS_KEY`. It uses path-style addresses and SigV4 signing, which every one of
those accepts (Neon accepts nothing else).
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
        from botocore.config import Config  # noqa: PLC0415

        if not (settings.s3_bucket and settings.s3_access_key_id and settings.s3_secret_access_key):
            raise IntegrationError("S3 storage is not configured (see docs/INTEGRATIONS.md)")
        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                # Recent boto3 adds CRC checksums to every upload by default; several
                # S3-compatible services reject them. Only send them when S3 requires it.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(key) from None

    def url(self, key: str, expires_seconds: int = 900) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_seconds
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)


class AzureBlobStorage:
    """Azure Blob Storage (production on Azure). Blob storage does not speak the S3 API,
    so it has its own driver. Files are private; `url()` returns a read-only SAS link that
    expires, like the S3 driver's presigned URLs.

    Settings: `AZURE_STORAGE_CONNECTION_STRING` (with AccountKey) and `AZURE_STORAGE_CONTAINER`.
    """

    def __init__(self, settings: Settings, service_client=None) -> None:
        if not (settings.azure_storage_connection_string and settings.azure_storage_container):
            raise IntegrationError(
                "Azure storage is not configured (AZURE_STORAGE_CONNECTION_STRING, "
                "AZURE_STORAGE_CONTAINER; see docs/AZURE.md)"
            )
        from azure.storage.blob import BlobServiceClient  # noqa: PLC0415

        self._service = service_client or BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string
        )
        self._container_name = settings.azure_storage_container
        self._container = self._service.get_container_client(self._container_name)
        self._account_name = self._service.account_name
        self._account_key = getattr(self._service.credential, "account_key", None)
        if not self._account_key:
            raise IntegrationError(
                "AZURE_STORAGE_CONNECTION_STRING must include AccountKey (needed to sign links)"
            )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        from azure.core.exceptions import AzureError  # noqa: PLC0415
        from azure.storage.blob import ContentSettings  # noqa: PLC0415

        try:
            self._container.upload_blob(
                key,
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        except AzureError as exc:
            raise IntegrationError(f"Azure upload failed: {exc}") from exc

    def get(self, key: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

        try:
            return self._container.download_blob(key).readall()
        except ResourceNotFoundError:
            raise FileNotFoundError(key) from None

    def url(self, key: str, expires_seconds: int = 900) -> str:
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415
        from urllib.parse import quote  # noqa: PLC0415

        from azure.storage.blob import BlobSasPermissions, generate_blob_sas  # noqa: PLC0415

        now = datetime.now(UTC)
        sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container_name,
            blob_name=key,
            account_key=self._account_key,
            permission=BlobSasPermissions(read=True),
            start=now - timedelta(minutes=5),  # allow for clock differences
            expiry=now + timedelta(seconds=expires_seconds),
        )
        return f"{self._container.url}/{quote(key)}?{sas}"

    def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

        try:
            self._container.delete_blob(key)
        except ResourceNotFoundError:
            pass
