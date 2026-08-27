#!/usr/bin/env python3
"""Guard against silently dropping completed broad-auction watch outputs."""

from __future__ import annotations

from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("auction_refresh.sh")


class AuctionRefreshWatchSourcesTest(unittest.TestCase):
    def test_every_started_broad_watch_is_merged(self) -> None:
        content = SCRIPT.read_text(encoding="utf-8")
        monitored_inputs = content.split("MONITORED_INPUT_ARGS=()", 1)[1].split(
            "python3 /opt/sonardeals-radar/capture_alces_fx.py", 1
        )[0]
        self.assertIn('AUTOBID_WATCH="$STATE/runtime/autobid_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "autobid"', content)
        self.assertIn('VPAUTO_WATCH="$STATE/runtime/vpauto_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "vpauto"', content)
        self.assertIn('HUUTOKAUPAT_WATCH="$STATE/runtime/huutokaupat_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "huutokaupat"', content)
        self.assertIn('VAVATO_WATCH="$STATE/runtime/vavato_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "vavato"', content)
        self.assertIn('PONIP_WATCH="$STATE/runtime/ponip_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "fina-ponip"', content)
        for variable in (
            "AUTOBID_WATCH",
            "EXLEASINGCAR_WATCH",
            "VPAUTO_WATCH",
            "HUUTOKAUPAT_WATCH",
            "VAVATO_WATCH",
            "PONIP_WATCH",
            "ASTE_WATCH",
            "KLARAVIK_WATCH",
            "VEACOM_WATCH",
            "PVP_WATCH",
            "SCHENGEN_WIDE_WATCH",
            "RETRADE_WATCH",
            "TROOSTWIJK_WATCH",
        ):
            self.assertIn(f'"${variable}"', monitored_inputs)


if __name__ == "__main__":
    unittest.main()
