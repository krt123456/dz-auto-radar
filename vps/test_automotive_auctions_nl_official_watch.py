#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import automotive_auctions_nl_official_watch as watch


UTC = dt.timezone.utc


def root_card(
    auction_id: str,
    slug: str,
    title: str,
    count: int,
    end: str,
) -> str:
    noun = "item" if count == 1 else "items"
    return f'''<div class="auction set"><h2><span>{title}</span></h2>
      <div class="auction-set-details"><div class="auction-content">
      <div class="lot-count">Items: <span class="val">{count} {noun}</span></div>
      <div class="auction-id">Veilingnummer <span class="val">#{auction_id}</span></div>
      <a href="/nl/ons-aanbod/{slug}/" class="btn">Bekijk veiling</a></div>
      <div class="auction-picture"><span class="countdown-timer" data-target="{end}"></span></div></div></div>'''


def root_page(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


def item_card(
    lot_id: str,
    slug: str,
    title: str,
    *,
    current: str = "€ 4 000,00",
    starting: str = "€ 3 000,00",
    end: str = "2026-09-01 18:00:00",
    mileage: str = "123.456 KM",
) -> str:
    return f'''<div class="auction-tiles-item"><div class="auction-picture">
      <a href="/nl/ons-aanbod/{slug}/{lot_id}-detail"><img src="https://images.example/{lot_id}.jpg"></a></div>
      <div class="auction-content" data-lotid="{lot_id}"><h2>{title}</h2>
      <div class="listing-holder">
        <div class="listing"><span>Startbod:</span><span class="val starting-bid">{starting}</span></div>
        <div class="listing"><span>Huidig bod:</span><span class="val current-offer">{current}</span></div>
        <div class="listing"><span>Afgelezen tellerstand:</span><span class="val">{mileage}</span></div>
      </div><div class="panel"><span class="countdown-timer" data-target="{end}"></span></div></div></div>'''


def auction_page(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str | list[str]]) -> None:
        self.responses = responses

    def get(self, url: str, **_: object) -> Response:
        response = self.responses[url]
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"no remaining response for {url}")
            response = response.pop(0)
        return Response(response)

    def close(self) -> None:
        return None


class AutomotiveAuctionsNlWatchTest(unittest.TestCase):
    def test_root_card_parses_count_and_amsterdam_time(self) -> None:
        parsed = watch.parse_root_page(root_page(root_card(
            "A1-10", "A1-10-cars", "Premium daily drivers", 2, "2026-09-01T19:00:00+02:00"
        )))
        self.assertEqual(parsed[0].auction_id, "A1-10")
        self.assertEqual(parsed[0].declared_count, 2)
        self.assertEqual(parsed[0].end_utc, dt.datetime(2026, 9, 1, 17, tzinfo=UTC))

    def test_item_card_parses_public_price_and_end(self) -> None:
        auction = watch.parse_root_page(root_page(root_card(
            "A1-10", "A1-10-cars", "Premium daily drivers", 1, "2026-09-01T19:00:00"
        )))[0]
        parsed = watch.parse_auction_page(auction_page(item_card(
            "42", "A1-10-cars", "Toyota Corolla Hybrid 2022", end="2026-09-02 10:15:30"
        )), auction)[0]
        self.assertEqual(parsed.current_bid, 4000)
        self.assertEqual(parsed.starting_bid, 3000)
        self.assertEqual(parsed.mileage_km, 123456)
        self.assertEqual(parsed.end_utc, dt.datetime(2026, 9, 2, 8, 15, 30, tzinfo=UTC))

    def test_two_pass_reconciles_and_excludes_non_cars(self) -> None:
        active_slug = "A1-10-cars"
        old_slug = "A1-9-ended"
        root = root_page(
            root_card("A1-10", active_slug, "Persons and business cars", 3, "2026-09-01T19:00:00"),
            root_card("A1-9", old_slug, "Old auction", 1, "2026-08-20T19:00:00"),
        )
        active = auction_page(
            item_card("42", active_slug, "Toyota Corolla Hybrid 2022"),
            item_card("43", active_slug, "Ford Transit bedrijfswagen 2020"),
            item_card("44", active_slug, "Chaparral 225 Speedboot 2012"),
        )
        responses: dict[str, str | list[str]] = {
            watch.SOURCE_URL: [root, root],
            f"https://www.automotive-auctions.nl/nl/ons-aanbod/{active_slug}/": [active, active],
        }
        payload = watch.build_watch(
            session=Session(responses), now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC), workers=1
        )
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["id"], "automotive-auctions-nl:A1-10:42")
        self.assertEqual(payload["rows"][0]["fuel"], "hybrid")
        self.assertEqual(report["overview_auctions"], 2)
        self.assertEqual(report["expired_overview_auctions"], 1)
        self.assertEqual(report["declared"], 3)
        self.assertEqual(report["visited"], 3)
        self.assertEqual(report["source_excluded"], {
            "commercial_or_non_passenger_vehicle": 1,
            "watercraft": 1,
        })
        self.assertTrue(report["two_pass_verified"])

    def test_zero_current_bid_uses_public_starting_bid(self) -> None:
        auction = watch.parse_root_page(root_page(root_card(
            "A1-10", "A1-10-cars", "Premium daily drivers", 1, "2026-09-01T19:00:00"
        )))[0]
        card = watch.parse_auction_page(auction_page(item_card(
            "42", "A1-10-cars", "Toyota Corolla 2022", current="€ 0,00", starting="€ 4 500,00"
        )), auction)[0]
        row = watch.normalize_card(card, auction, observed_at="2026-08-28T20:00:00+00:00")
        self.assertEqual(row["price_amount"], 4500)
        self.assertEqual(row["price_kind"], "starting_bid")

    def test_zero_current_and_start_bid_stays_unknown(self) -> None:
        auction = watch.parse_root_page(root_page(root_card(
            "A1-10", "A1-10-cars", "Premium daily drivers", 1, "2026-09-01T19:00:00"
        )))[0]
        card = watch.parse_auction_page(auction_page(item_card(
            "42", "A1-10-cars", "Toyota Corolla 2022", current="€ 0,00", starting="€ 0,00"
        )), auction)[0]
        row = watch.normalize_card(card, auction, observed_at="2026-08-28T20:00:00+00:00")
        self.assertIsNone(row["price_amount"])
        self.assertEqual(row["price_kind"], "unknown")

    def test_explicit_commercial_models_and_bus_are_excluded(self) -> None:
        auction = watch.Auction("A1-10", "https://www.automotive-auctions.nl/nl/ons-aanbod/A1-10-cars/", "Mixed vehicles", 1, dt.datetime(2026, 9, 1, 17, tzinfo=UTC))
        for title in (
            "MAN TGE 30 2.0 2018",
            "Dacia Dokker 1.6 MPI 2016",
            "International 3700 Schoolbus 1999",
            "Ford USA F-150 SuperCab 2015",
            "Piaggio Ape 50 Brommobiel 1985",
        ):
            card = watch.Card("A1-10", "42", "https://www.automotive-auctions.nl/nl/ons-aanbod/A1-10-cars/42", title, dt.datetime(2026, 9, 2, 17, tzinfo=UTC), 100, 100, "€ 100,00", "€ 100,00", None, "")
            self.assertEqual(watch.passenger_exclusion_reason(card, auction), "commercial_or_non_passenger_vehicle")

    def test_changed_lot_facts_between_passes_fail_closed(self) -> None:
        slug = "A1-10-cars"
        root = root_page(root_card("A1-10", slug, "Premium daily drivers", 1, "2026-09-01T19:00:00"))
        first = auction_page(item_card("42", slug, "Toyota Corolla 2022"))
        second = auction_page(item_card("42", slug, "Toyota Corolla 2023"))
        with self.assertRaisesRegex(watch.AutomotiveAuctionsWatchError, "lot facts changed"):
            watch.build_watch(
                session=Session({watch.SOURCE_URL: [root, root], f"https://www.automotive-auctions.nl/nl/ons-aanbod/{slug}/": [first, second]}),
                now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC), workers=1,
            )

    def test_declared_count_mismatch_fails_closed(self) -> None:
        auction = watch.parse_root_page(root_page(root_card(
            "A1-10", "A1-10-cars", "Premium daily drivers", 2, "2026-09-01T19:00:00"
        )))[0]
        with self.assertRaisesRegex(watch.AutomotiveAuctionsWatchError, "count mismatch"):
            watch.parse_auction_page(auction_page(item_card("42", "A1-10-cars", "Toyota Corolla 2022")), auction)


if __name__ == "__main__":
    unittest.main()
