"""S3-compatible storage driver, configured the way Neon Object Storage needs it
(path-style addresses, SigV4, no default upload checksums). Runs offline: botocore's
Stubber stands in for the service and presigned links are signed locally."""

from __future__ import annotations

import io
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.core.config import Settings
from app.integrations.storage import S3Storage

ENDPOINT = "https://br-test-branch-123.storage.c-3.eu-central-1.aws.neon.tech"


def make_storage() -> S3Storage:
    return S3Storage(
        Settings(
            storage_driver="s3",
            s3_endpoint_url=ENDPOINT,
            s3_region="eu-central-1",
            s3_bucket="autoscope-uploads",
            s3_access_key_id="nak_test_key",
            s3_secret_access_key="nsk_test_secret",
        )
    )


def test_client_uses_path_style_and_sigv4():
    config = make_storage()._client.meta.config
    assert config.s3["addressing_style"] == "path"
    assert config.signature_version == "s3v4"
    assert config.request_checksum_calculation == "when_required"


def test_presigned_link_is_path_style_and_expires():
    url = make_storage().url("listings/1/photo.jpg", expires_seconds=600)
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}" == ENDPOINT  # bucket not in the host name
    assert parsed.path == "/autoscope-uploads/listings/1/photo.jpg"
    query = parse_qs(parsed.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["600"]
    assert "eu-central-1" in query["X-Amz-Credential"][0]


def test_put_get_and_missing_file():
    storage = make_storage()
    with Stubber(storage._client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "autoscope-uploads",
                "Key": "kyc/7/id.pdf",
                "Body": b"%PDF-1.4",
                "ContentType": "application/pdf",
            },
        )
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(b"%PDF-1.4"), 8)},
            {"Bucket": "autoscope-uploads", "Key": "kyc/7/id.pdf"},
        )
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

        storage.put("kyc/7/id.pdf", b"%PDF-1.4", "application/pdf")
        assert storage.get("kyc/7/id.pdf") == b"%PDF-1.4"
        with pytest.raises(FileNotFoundError):
            storage.get("kyc/7/gone.pdf")
        stub.assert_no_pending_responses()
