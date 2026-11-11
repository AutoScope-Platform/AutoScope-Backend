"""VIN decoding through the free NHTSA vPIC API (no API key needed).

Note: NHTSA covers vehicles sold in the US market well and other markets partially.
When a VIN cannot be decoded the API returns None and the app asks the seller to
type the details in by hand (FR-3.3 is a "Could" requirement).
"""

from __future__ import annotations

import httpx

from app.integrations.base import VinInfo


class NhtsaVinDecoder:
    URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=10)

    def decode(self, vin: str) -> VinInfo | None:
        try:
            response = self._client.get(self.URL.format(vin=vin))
            response.raise_for_status()
            row = response.json()["Results"][0]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return None
        if not row.get("Make"):
            return None
        year = row.get("ModelYear")
        return VinInfo(
            vin=vin,
            make=row.get("Make", "").title() or None,
            model=row.get("Model") or None,
            year=int(year) if year and year.isdigit() else None,
            body_class=row.get("BodyClass") or None,
            fuel_type=row.get("FuelTypePrimary") or None,
            engine=row.get("DisplacementL") and f"{row['DisplacementL']}L" or None,
        )
