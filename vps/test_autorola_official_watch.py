#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

from autorola_official_watch import (
    AUCTIONS_URL,
    CATALOGUE_URL,
    AuctionSpec,
    build_watch,
    parse_auction_index,
    parse_catalogue_page,
    parse_end,
)


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 6, 0, tzinfo=UTC)  # Friday, 08:00 Brussels

INDEX_HTML = """
<html><body>
  <a href="sc?aid=111">Fixture public auction</a>
  <a href="sc?aid=111"><img src="x.jpg"></a>
  <a href="joinauction?aid=999">Fixture login-only eAuction</a>
</body></html>
"""

CATALOGUE_HTML = """
<html><body>
<div class="showing">Displays: 1 to 2 of 2</div>
<table>
  <tr>
    <td class="image"><a href="bid?zz=1&amp;eid=1001&amp;aid=111"><img src="x"></a></td>
    <td class="title"><a href="bid?zz=1&amp;eid=1001&amp;aid=111">2024 RENAULT MEGANE E-TECH, Petrol Hybrid 160 HP</a></td>
    <td class="regDate">01/2024</td>
    <td class="mileage">km 12,345</td>
    <td class="price"><div class="noprice">Register or login to bid</div></td>
  </tr>
  <tr>
    <td class="location"><img src="https://g.autorola.com/g/i/fl/FR.gif">FR, Paris</td>
    <td class="auctionEnd">End Monday 10:05:00hs+</td>
  </tr>
  <tr>
    <td class="image"><a href="bid?zz=1&amp;eid=1002&amp;aid=111"><img src="x"></a></td>
    <td class="title"><a href="bid?zz=1&amp;eid=1002&amp;aid=111">2022 FORD TRANSIT 2.0 Diesel 130 HP</a></td>
    <td class="regDate">11/2022</td>
    <td class="mileage">km 65,432</td>
    <td class="price">Max. bid EUR 18,400</td>
  </tr>
  <tr>
    <td class="location"><img src="https://g.autorola.com/g/i/fl/DE.gif">DE, Berlin</td>
    <td class="auctionEnd">End Tuesday 11:00hs</td>
  </tr>
</table>
</body></html>
"""


def index_html(specs: list[AuctionSpec]) -> str:
    links = "".join(
        f'<a href="sc?aid={spec.aid}">{spec.name}</a>' for spec in specs
    )
    return f"<html><body>{links}</body></html>"


def one_car_catalogue(*, aid: str, eid: str, title: str) -> str:
    return f"""
    <html><body><div class="showing">Displays: 1 to 1 of 1</div><table>
      <tr>
        <td class="image"><a href="bid?eid={eid}&amp;aid={aid}"><img src="x"></a></td>
        <td class="title"><a href="bid?eid={eid}&amp;aid={aid}">{title}</a></td>
        <td class="regDate">01/2024</td><td class="mileage">km 12,345</td>
        <td class="price"><div class="noprice">Register or login to bid</div></td>
      </tr>
      <tr>
        <td class="location"><img src="https://g.autorola.com/g/i/fl/FR.gif">FR, Paris</td>
        <td class="auctionEnd">End Monday 10:05:00hs+</td>
      </tr>
    </table></body></html>
    """


class FakeResponse:
    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url

    def raise_for_status(self) -> None:
        return None


class StableSession:
    def get(self, url: str, *, params=None, headers=None, timeout=None):
        if url == AUCTIONS_URL:
            return FakeResponse(INDEX_HTML, AUCTIONS_URL)
        if url == CATALOGUE_URL:
            self_params = params or {}
            if self_params.get("aid") != "111" or self_params.get("tcsp") != "0":
                raise AssertionError(f"unexpected catalogue request {self_params}")
            if self_params.get("tnoipp") != "1000":
                raise AssertionError(f"catalogue must use one-page reconciliation {self_params}")
            return FakeResponse(CATALOGUE_HTML, CATALOGUE_URL)
        raise AssertionError(f"unexpected URL {url}")

    def close(self) -> None:
        return None


class MovingIndexFactory:
    def __init__(self) -> None:
        self.index_calls = 0
        self.initial = AuctionSpec(aid="111", name="Initial public auction")
        self.added = AuctionSpec(aid="222", name="New public auction")

    def __call__(self) -> "MovingIndexSession":
        return MovingIndexSession(self)


class MovingIndexSession:
    def __init__(self, factory: MovingIndexFactory) -> None:
        self.factory = factory

    def get(self, url: str, *, params=None, headers=None, timeout=None):
        if url == AUCTIONS_URL:
            states = [
                [self.factory.initial],
                [self.factory.initial, self.factory.added],
                [self.factory.initial, self.factory.added],
            ]
            index = min(self.factory.index_calls, len(states) - 1)
            self.factory.index_calls += 1
            return FakeResponse(index_html(states[index]), AUCTIONS_URL)
        if url == CATALOGUE_URL:
            aid = str((params or {}).get("aid"))
            if aid == "111":
                return FakeResponse(
                    one_car_catalogue(
                        aid="111", eid="1001", title="2024 RENAULT MEGANE Petrol Hybrid"
                    ),
                    CATALOGUE_URL,
                )
            if aid == "222":
                return FakeResponse(
                    one_car_catalogue(
                        aid="222", eid="2001", title="2025 TOYOTA YARIS Petrol Hybrid"
                    ),
                    CATALOGUE_URL,
                )
        raise AssertionError(f"unexpected URL {url} {params}")

    def close(self) -> None:
        return None


class AutorolaOfficialWatchTest(unittest.TestCase):
    def test_index_keeps_public_routes_and_counts_login_only_eauction(self) -> None:
        specs, restricted = parse_auction_index(INDEX_HTML)
        self.assertEqual(specs, (AuctionSpec(aid="111", name="Fixture public auction"),))
        self.assertEqual(restricted, ("999",))

    def test_relative_ends_use_the_catalogue_timezone(self) -> None:
        self.assertEqual(
            parse_end("End Monday 10:05:00hs+", now=NOW),
            dt.datetime(2026, 8, 31, 8, 5, tzinfo=UTC),
        )
        self.assertEqual(
            parse_end("End Tuesday 11:00hs", now=NOW),
            dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        )
        self.assertIsNone(parse_end("End today 07:59hs", now=NOW))

    def test_page_retains_cards_without_inventing_a_bid(self) -> None:
        page = parse_catalogue_page(
            CATALOGUE_HTML,
            spec=AuctionSpec(aid="111", name="Fixture public auction"),
            observed_at=NOW.isoformat(),
            now=NOW,
        )
        self.assertEqual((page.start, page.end, page.total), (1, 2, 2))
        self.assertEqual(page.card_ids, ("1001", "1002"))
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0]["fuel"], "petrol/electric hybrid")
        self.assertEqual(page.rows[0]["mileage"], 12345)
        self.assertEqual(page.rows[0]["price_kind"], "unknown")
        self.assertIsNone(page.rows[0]["price_amount"])
        self.assertEqual(page.rows[0]["category"], "car")
        self.assertEqual(page.rejected_counts, {"not_passenger_car": 1})

    def test_full_watch_reconciles_the_exact_public_counter(self) -> None:
        payload = build_watch(
            now=NOW,
            timeout=5,
            max_workers=1,
            session_factory=StableSession,
        )
        report = payload["source_reports"]["autorola-eu"]
        self.assertEqual(payload["catalogue_total"], 2)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["auction_routes"], 1)
        self.assertEqual(payload["restricted_eauction_routes"], 1)
        self.assertEqual(report["pages"], 1)
        self.assertEqual(report["snapshot_mode"], "atomic")
        self.assertTrue(all(row["adapter_authorized"] is True for row in payload["rows"]))

    def test_moving_auction_index_retains_complete_final_route_coverage(self) -> None:
        factory = MovingIndexFactory()
        payload = build_watch(
            now=NOW,
            timeout=5,
            max_workers=1,
            session_factory=factory,
        )
        report = payload["source_reports"]["autorola-eu"]
        self.assertEqual(payload["auction_routes"], 2)
        self.assertEqual(payload["catalogue_total"], 2)
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(report["snapshot_mode"], "moving_catalogue_coverage")
        self.assertEqual(report["index_coverage_passes"], 1)
        self.assertIn("index changed", report["strict_snapshot_last_error"])


if __name__ == "__main__":
    unittest.main()
