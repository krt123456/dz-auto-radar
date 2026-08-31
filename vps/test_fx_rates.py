#!/usr/bin/env python3
from __future__ import annotations

import unittest

import fx_rates


class FxRatesTest(unittest.TestCase):
    def test_to_eur_converts_and_rounds(self) -> None:
        self.assertEqual(fx_rates.to_eur(15000, 12.0), 1250)
        self.assertEqual(fx_rates.to_eur(15000.5, 12.0), 1250.04)
        self.assertEqual(fx_rates.to_eur(100, 10.0), 10)
        self.assertIsNone(fx_rates.to_eur(None, 10.0))

    def test_fetch_ecb_rate_parses_obs_value(self) -> None:
        calls = []

        def fake_urlopen(url, timeout=30):
            calls.append(url)

            class R:
                def __init__(self, body):
                    self.body = body

                def read(self):
                    return self.body.encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            body = (
                "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,TIME_PERIOD,OBS_VALUE\n"
                "EXR.D.SEK.EUR.SP00.A,D,SEK,EUR,SP00,2026-08-28,11.0885\n"
            )
            return R(body)

        original = fx_rates.urllib.request.urlopen
        fx_rates.urllib.request.urlopen = fake_urlopen
        try:
            rate, date = fx_rates.fetch_ecb_units_per_eur("SEK")
        finally:
            fx_rates.urllib.request.urlopen = original
        self.assertEqual(rate, 11.0885)
        self.assertEqual(date, "2026-08-28")
        self.assertIn("D.SEK.EUR", calls[0])

    def test_fetch_ecb_rate_fails_closed_on_garbage(self) -> None:
        class R:
            def read(self):
                return b"oops"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original = fx_rates.urllib.request.urlopen
        fx_rates.urllib.request.urlopen = lambda url, timeout=30: R()
        try:
            with self.assertRaises(fx_rates.FxRateError):
                fx_rates.fetch_ecb_units_per_eur("SEK")
        finally:
            fx_rates.urllib.request.urlopen = original


if __name__ == "__main__":
    unittest.main()
