#!/usr/bin/env python3

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import listing_availability as lifecycle


class ListingAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
        self.valid_until = self.as_of + timedelta(hours=8)

    def decide(self, **values):
        return lifecycle.decide_lifecycle(
            as_of=self.as_of,
            valid_until=self.valid_until,
            identity_proven=True,
            **values,
        )

    def test_authoritative_unavailable_states_are_dead(self) -> None:
        for state in ("Sold", "SoldOut", "Removed", "Expired", "Unavailable", "OutOfStock"):
            with self.subTest(state=state):
                self.assertEqual(self.decide(availability=state).status, "dead")

    def test_expiration_exact_observation_boundary_is_dead(self) -> None:
        before = self.as_of - timedelta(microseconds=1)
        after = self.as_of + timedelta(microseconds=1)
        self.assertEqual(self.decide(expires_at=before.isoformat()).status, "dead")
        self.assertEqual(self.decide(expires_at=self.as_of.isoformat()).status, "dead")
        self.assertEqual(self.decide(expires_at=after.isoformat()).status, "unknown")

    def test_expiration_must_extend_beyond_complete_public_window(self) -> None:
        self.assertEqual(
            self.decide(expires_at=self.valid_until.isoformat()).status,
            "unknown",
        )
        self.assertEqual(
            self.decide(
                availability="https://schema.org/InStock",
                expires_at=(self.valid_until + timedelta(microseconds=1)).isoformat(),
            ).status,
            "verified",
        )

    def test_malformed_or_naive_expiration_is_unknown(self) -> None:
        for value in ("not-a-time", "2026-08-13T02:00:00"):
            with self.subTest(value=value):
                self.assertEqual(self.decide(expires_at=value).status, "unknown")

    def test_identity_is_required_even_with_http_page_evidence(self) -> None:
        decision = lifecycle.decide_lifecycle(
            availability="InStock",
            as_of=self.as_of,
            valid_until=self.valid_until,
            identity_proven=False,
        )
        self.assertEqual(decision.status, "unknown")

    def test_structured_lifecycle_prefers_unavailable_and_earliest_expiry(self) -> None:
        html = """
        <script type="application/ld+json">
          {"@type":"Vehicle","offers":[
            {"availability":"https://schema.org/InStock","validThrough":"2026-08-14T10:00:00Z"},
            {"availability":"https://schema.org/SoldOut","validThrough":"2026-08-14T09:00:00Z"}
          ]}
        </script>
        <meta itemprop="availability" content="https://schema.org/OutOfStock">
        """
        availability, expiration = lifecycle.structured_lifecycle(html)
        self.assertEqual(availability, "outofstock")
        self.assertEqual(expiration, "2026-08-14T09:00:00Z")


if __name__ == "__main__":
    unittest.main()
