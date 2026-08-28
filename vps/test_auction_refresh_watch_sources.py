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
        self.assertIn('RADAR_EXLEASINGCAR_WATCH_WORKERS:-16', content)
        self.assertIn('run_official_watch "finshop,licytacje-komornik,e-leiloes"', content)
        self.assertIn('--source finshop --source licytacje-komornik --source e-leiloes', content)
        self.assertIn('VPAUTO_WATCH="$STATE/runtime/vpauto_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "vpauto"', content)
        self.assertIn('RBAUCTION_WATCH="$STATE/runtime/rbauction_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "rbauction-eu"', content)
        self.assertIn('AUTOROLA_WATCH="$STATE/runtime/autorola_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "autorola-eu"', content)
        self.assertIn('RADAR_AUTOROLA_WATCH_WORKERS:-6', content)
        self.assertIn('HUUTOKAUPAT_WATCH="$STATE/runtime/huutokaupat_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "huutokaupat"', content)
        self.assertIn('VAVATO_WATCH="$STATE/runtime/vavato_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "vavato"', content)
        self.assertIn('PONIP_WATCH="$STATE/runtime/ponip_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "fina-ponip"', content)
        self.assertIn('CARAUKCE_WATCH="$STATE/runtime/caraukce_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "caraukce"', content)
        self.assertIn('AURENA_WATCH="$STATE/runtime/aurena_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "aurena"', content)
        self.assertIn('AUCTIONMASTER_WATCH="$STATE/runtime/auctionmaster_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "auctionmaster"', content)
        self.assertIn('BILWEB_WATCH="$STATE/runtime/bilweb_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "bilweb"', content)
        self.assertIn('KVDCARS_WATCH="$STATE/runtime/kvdcars_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "kvdcars"', content)
        self.assertIn('KIERTONET_WATCH="$STATE/runtime/kiertonet_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "kiertonet"', content)
        self.assertIn('AUKTIONSHUSET_DAB_WATCH="$STATE/runtime/auktionshuset_dab_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "auktionshuset-dab"', content)
        self.assertIn('AGORASTORE_WATCH="$STATE/runtime/agorastore_official_auction_watch.json"', content)
        self.assertIn('run_official_watch "agorastore"', content)
        self.assertIn('run_official_watch "klaravik-se,klaravik-dk"', content)
        self.assertIn('python3 /opt/sonardeals-radar/klaravik_official_watch.py', content)
        for variable in (
            "AUTOBID_WATCH",
            "EXLEASINGCAR_WATCH",
            "VPAUTO_WATCH",
            "RBAUCTION_WATCH",
            "AUTOROLA_WATCH",
            "HUUTOKAUPAT_WATCH",
            "VAVATO_WATCH",
            "PONIP_WATCH",
            "CARAUKCE_WATCH",
            "AURENA_WATCH",
            "AUCTIONMASTER_WATCH",
            "BILWEB_WATCH",
            "KVDCARS_WATCH",
            "KIERTONET_WATCH",
            "AUKTIONSHUSET_DAB_WATCH",
            "AGORASTORE_WATCH",
            "ASTE_WATCH",
            "KLARAVIK_WATCH",
            "VEACOM_WATCH",
            "PVP_WATCH",
            "SCHENGEN_WIDE_WATCH",
            "RETRADE_WATCH",
            "TROOSTWIJK_WATCH",
        ):
            self.assertIn(f'"${variable}"', monitored_inputs)

    def test_public_watch_verification_uses_shard_manifest(self) -> None:
        content = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("load_published_official_auction_watch", content)
        self.assertIn("official_auction_watch_parts_sha256", content)
        self.assertIn('"part_count": len(published_root.get("parts", []))', content)
        self.assertIn('--watch "$PUBLIC_WATCH"', content)


if __name__ == "__main__":
    unittest.main()
