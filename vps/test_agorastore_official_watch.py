#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

import agorastore_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def item(
    native_id: str,
    *,
    title: str = "PEUGEOT 208 ESSENCE",
    vehicle_type: str = "BERLINE",
    fuel: str = "ESSENCE",
    registration: str = "31/05/2024",
    mileage: str = "22 140",
    end: str = "2026-09-03T14:00:00.000Z",
    current_cents: int = 1_250_000,
    start_cents: int = 1_000_000,
    bids: int = 2,
) -> dict[str, Any]:
    return {
        "itemShortId": native_id,
        "auctionEndDate": end,
        "status": "OPEN",
        "localities": ["75001 - Paris"],
        "seller": {"organisationName": "Ville de Paris"},
        "translations": [{
            "language": "fr",
            "name": title,
            "description": (
                f"<h3>Type de v?hicule</h3><p>{vehicle_type}</p>"
                f"<h3>Date de mise en circulation</h3><p>{registration}</p>"
                f"<h3>Kilom?trage</h3><p>{mileage}</p>"
                f"<h3>?nergie</h3><p>{fuel}</p>"
            ),
        }],
        "categories": [{
            "categoryShortId": watch.CAR_CATEGORY_ID,
            "code": "cars",
            "primary": True,
        }],
        "indexedMeta": {
            "info": {"country": "FR"},
            "saleInformation": {
                "pricesCents": {"current": current_cents, "start": start_cents, "currency": "EUR"},
                "numberOfBids": bids,
                "hasReservePrice": True,
                "reservePriceReached": False,
            },
        },
    }


class Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload

    def close(self) -> None:
        return None


class Session:
    def __init__(self, snapshots: list[list[dict[str, Any]]]) -> None:
        self.snapshots = snapshots
        self.pass_index = -1

    def post(self, url: str, *, json: dict[str, Any], **_: Any) -> Response:
        if url != watch.API_URL:
            raise AssertionError(url)
        offset = int(json["from"])
        size = int(json["size"])
        if offset == 0:
            self.pass_index += 1
        snapshot = self.snapshots[min(self.pass_index, len(self.snapshots) - 1)]
        values = snapshot[offset:offset + size]
        return Response({"total": len(snapshot), "count": len(values), "results": values})


class BrokenCountSession(Session):
    def post(self, url: str, *, json: dict[str, Any], **kwargs: Any) -> Response:
        response = super().post(url, json=json, **kwargs)
        response.payload["count"] += 1
        return response


class AgorastoreWatchTest(unittest.TestCase):
    def test_row_preserves_public_car_fields_and_bid(self) -> None:
        row = watch.row_from_item(item("agora-123"), observed_at=NOW.isoformat(), now=NOW)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["id"], "agorastore:agora-123")
        self.assertEqual(row["url"], "https://www.agorastore.fr/fr/ventes-occasions/voiture/agora-123")
        self.assertEqual(row["category"], "car")
        self.assertEqual(row["country"], "FR")
        self.assertEqual(row["year"], 2024)
        self.assertEqual(row["registration_date"], "2024-05-31")
        self.assertEqual(row["mileage"], 22140)
        self.assertEqual(row["fuel"], "petrol")
        self.assertEqual(row["price_amount"], 12500)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["canonical_end_utc"], "2026-09-03T14:00:00+00:00")

    def test_explicit_non_passenger_vehicle_is_not_emitted(self) -> None:
        pickup = item("agora-pickup", title="PICKUP MITSUBISHI L200", vehicle_type="PICK-UP")
        commercial = item("agora-commercial", title="CITROEN C3 V?hicule Commercial 2 places")
        self.assertIsNone(watch.row_from_item(pickup, observed_at=NOW.isoformat(), now=NOW))
        self.assertIsNone(watch.row_from_item(commercial, observed_at=NOW.isoformat(), now=NOW))

    def test_two_full_passes_reconcile_every_declared_category_item(self) -> None:
        cars = [
            item("agora-one", title="RENAULT CLIO", fuel="DIESEL"),
            item("agora-two", title="TOYOTA YARIS", fuel="HYBRIDE ESSENCE"),
            item("agora-pickup", title="PICKUP MITSUBISHI L200", vehicle_type="PICK-UP"),
        ]
        payload = watch.build_watch(session=Session([cars, cars]), now=NOW, timeout=5, page_size=2)
        self.assertEqual(payload["row_count"], 2)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(report["declared"], 3)
        self.assertEqual(report["visited"], 3)
        self.assertEqual(report["passenger_cars"], 2)
        self.assertEqual(report["excluded_explicit_non_passenger"], 1)
        self.assertEqual(report["pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])
        self.assertEqual({row["fuel"] for row in payload["rows"]}, {"diesel", "petrol/electric hybrid"})

    def test_changed_final_snapshot_fails_closed(self) -> None:
        first = [item("agora-one"), item("agora-two")]
        second = [item("agora-one"), item("agora-three")]
        with self.assertRaisesRegex(watch.AgorastoreWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, second]), now=NOW, timeout=5, page_size=2)

    def test_invalid_api_count_fails_closed(self) -> None:
        with self.assertRaisesRegex(watch.AgorastoreWatchError, "count"):
            watch.build_watch(
                session=BrokenCountSession([[item("agora-one")]]), now=NOW, timeout=5, page_size=2
            )


if __name__ == "__main__":
    unittest.main()
