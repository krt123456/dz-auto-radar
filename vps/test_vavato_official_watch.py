#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import unittest

import build_auction_board as board
import vavato_official_watch as watch


UTC = dt.timezone.utc


def product(lot_id: str, title: str) -> dict[str, object]:
    return {
        "@type": "Product",
        "name": title,
        "url": f"https://www.vavato.com/en/l/sample-{lot_id}",
    }


def card(lot_id: str, country: str) -> str:
    return f'<div class="lot-card"><a href="/en/l/sample-{lot_id}">Lot, {country}</a></div>'


def page(total: int, products: list[dict[str, object]], cards: str) -> str:
    data = {
        "@context": "https://schema.org",
        "@graph": [{
            "@type": "CollectionPage",
            "mainEntity": {
                "@type": "ItemList",
                "itemListElement": [
                    {"@type": "ListItem", "position": index + 1, "item": item}
                    for index, item in enumerate(products)
                ],
            },
        }],
    }
    return (
        f'<div>Cars {total} lots</div>'
        '<nav aria-label="Pagination"><a href="?page=2">Page 2</a></nav>'
        f'{cards}<script type="application/ld+json">{json.dumps(data)}</script>'
    )


PAGE_1 = page(
    3,
    [product("A1-10-1", "Ford 2024 Hybrid"), product("A1-10-2", "Tesla 2025 Electric")],
    card("A1-10-1", "NL") + card("A1-10-2", "BE"),
)
PAGE_2 = page(
    3,
    [product("A1-10-3", "Audi 2023 Diesel")],
    card("A1-10-3", "DE"),
)


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses

    def get(self, url: str, **_: object) -> Response:
        return Response(self.responses[url])


class VavatoWatchTest(unittest.TestCase):
    def responses(self, first: str = PAGE_1) -> dict[str, str]:
        return {watch.SOURCE_URL: first, watch.page_url(2): PAGE_2}

    def test_complete_category_reconciles_public_json_ld_identities(self) -> None:
        payload = watch.build_watch(
            session=Session(self.responses()),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
        )
        rows = {row["id"]: row for row in payload["rows"]}
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["vavato"]["declared"], 3)
        self.assertEqual(rows["vavato:A1-10-1"]["fuel"], "hybrid")
        self.assertEqual(rows["vavato:A1-10-2"]["fuel"], "electric")
        self.assertEqual(rows["vavato:A1-10-3"]["fuel"], "diesel")
        self.assertEqual(rows["vavato:A1-10-1"]["country"], "NL")
        self.assertIsNone(rows["vavato:A1-10-1"]["price_eur"])
        self.assertTrue(all(row["adapter_authorized"] for row in rows.values()))
        normalized, reason = board._normalize_monitored_row(
            rows["vavato:A1-10-1"],
            generated_at=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
        )
        self.assertEqual(reason, "")
        self.assertEqual(normalized["country"], "NL")

    def test_declared_total_mismatch_fails_closed(self) -> None:
        with self.assertRaises(watch.VavatoWatchError):
            watch.build_watch(
                session=Session(self.responses(first=PAGE_1.replace("3 lots", "4 lots"))),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )


if __name__ == "__main__":
    unittest.main()
