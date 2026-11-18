"""Azure support: Blob storage driver, Redis TLS for the worker, Application Insights
switch, readiness probe, database pool settings."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from azure.core.exceptions import ResourceNotFoundError

from app.core.config import Settings
from app.integrations import registry
from app.integrations.base import IntegrationError
from app.integrations.storage import AzureBlobStorage

# The public, well-known development key of Azurite (Microsoft's local storage emulator).
# Not a secret: it lets the real SDK sign links in tests without an Azure account.
AZURITE = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)


class FakeContainer:
    url = "https://autoscopestaging.blob.core.windows.net/uploads"

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    def upload_blob(self, key, data, overwrite, content_settings):
        assert overwrite is True
        self.blobs[key] = (data, content_settings.content_type)

    def download_blob(self, key):
        if key not in self.blobs:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(readall=lambda: self.blobs[key][0])

    def delete_blob(self, key):
        if key not in self.blobs:
            raise ResourceNotFoundError("missing")
        del self.blobs[key]


def _fake_service(container, *, key="a2V5"):
    return SimpleNamespace(
        account_name="autoscopestaging",
        credential=SimpleNamespace(account_key=key) if key else None,
        get_container_client=lambda name: container,
    )


def _settings(**overrides) -> Settings:
    values = {
        "storage_driver": "azure",
        "azure_storage_connection_string": AZURITE,
        "azure_storage_container": "uploads",
    }
    return Settings(**{**values, **overrides})


def test_blob_driver_put_get_delete():
    container = FakeContainer()
    storage = AzureBlobStorage(_settings(), service_client=_fake_service(container))
    storage.put("kyc/u1/id.pdf", b"%PDF-1.4", "application/pdf")
    assert container.blobs["kyc/u1/id.pdf"] == (b"%PDF-1.4", "application/pdf")
    assert storage.get("kyc/u1/id.pdf") == b"%PDF-1.4"
    storage.delete("kyc/u1/id.pdf")
    storage.delete("kyc/u1/id.pdf")  # already gone: no error, like the other drivers
    with pytest.raises(FileNotFoundError):
        storage.get("kyc/u1/id.pdf")


def test_blob_links_are_signed_read_only_and_expire():
    """The real SDK, offline: the link points at the blob and carries a read-only SAS."""
    storage = AzureBlobStorage(_settings())
    link = urlparse(storage.url("listings/abc/photo 1.jpg", expires_seconds=600))
    assert link.netloc == "127.0.0.1:10000"
    assert link.path == "/devstoreaccount1/uploads/listings/abc/photo%201.jpg"
    sas = parse_qs(link.query)
    assert sas["sp"] == ["r"] and sas["sr"] == ["b"] and sas["sig"][0] and sas["se"][0]


def test_blob_driver_needs_settings_and_an_account_key():
    with pytest.raises(IntegrationError):
        AzureBlobStorage(_settings(azure_storage_container=None))
    with pytest.raises(IntegrationError, match="AccountKey"):
        AzureBlobStorage(_settings(), service_client=_fake_service(FakeContainer(), key=None))


def test_registry_picks_the_azure_driver(monkeypatch):
    monkeypatch.setattr(registry, "get_settings", lambda: _settings())
    registry.reset()
    try:
        assert isinstance(registry.get_storage(), AzureBlobStorage)
    finally:
        registry.reset()


def test_production_accepts_azure_storage():
    """Production refuses fake drivers; Azure storage is not one of them. (KYC still is,
    until a vendor is chosen, so production cannot boot yet: that is intended.)"""
    with pytest.raises(ValueError) as exc:
        Settings(
            env="production", jwt_secret="x" * 40, cors_origins="https://admin.autoscope.ng",
            sms_driver="termii", email_driver="smtp", push_driver="fcm", payment_driver="paystack",
            vin_driver="nhtsa", social_driver="live", storage_driver="azure",
        )  # fmt: skip
    assert "KYC_DRIVER" in str(exc.value) and "STORAGE_DRIVER" not in str(exc.value)


# --- Redis TLS for the worker ----------------------------------------------------------------
def test_worker_uses_tls_for_rediss():
    import ssl

    from app.worker import broker_ssl_options

    assert broker_ssl_options("rediss://:key@autoscope.redis.cache.windows.net:6380/0") == {
        "ssl_cert_reqs": ssl.CERT_REQUIRED
    }
    assert broker_ssl_options("redis://localhost:6379/0") is None
    assert broker_ssl_options(None) is None


# --- Application Insights --------------------------------------------------------------------
def test_monitoring_starts_only_with_a_connection_string(monkeypatch):
    import azure.monitor.opentelemetry as azmon

    import app.main as main

    calls = []
    monkeypatch.setattr(azmon, "configure_azure_monitor", lambda **kw: calls.append(kw))
    monkeypatch.setattr(main, "_monitoring_started", False)
    assert main.setup_monitoring(Settings()) is False
    conn = "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    assert main.setup_monitoring(Settings(applicationinsights_connection_string=conn)) is True
    assert (
        main.setup_monitoring(Settings(applicationinsights_connection_string=conn)) is False
    )  # once
    assert calls == [{"connection_string": conn}]


# --- Readiness and pool settings -------------------------------------------------------------
def test_readiness_probe(client, monkeypatch):
    ok = client.get("/health/ready")
    assert ok.status_code == 200 and ok.json() == {"status": "ok", "checks": {"database": "ok"}}

    import app.core.db as db_module

    def broken():
        raise ConnectionError("database down")

    monkeypatch.setattr(db_module, "new_session", broken)
    down = client.get("/health/ready")
    assert down.status_code == 503
    assert down.json()["checks"]["database"] == "error: ConnectionError"
    assert client.get("/health").status_code == 200  # liveness does not touch the database


def test_pool_size_is_configurable():
    s = Settings(db_pool_size=3, db_max_overflow=2)
    assert (s.db_pool_size, s.db_max_overflow) == (3, 2)
    assert (Settings().db_pool_size, Settings().db_max_overflow) == (10, 20)
