from pathlib import Path
import unittest


class AuctionRefreshLockingTests(unittest.TestCase):
    def test_auction_refresh_uses_a_dedicated_lock(self) -> None:
        script = (Path(__file__).with_name("auction_refresh.sh")).read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'RADAR_AUCTION_REFRESH_LOCK_FILE:-/run/lock/sonardeals-auction-refresh.lock',
            script,
        )
        self.assertNotIn('LOCK="${RADAR_REFRESH_LOCK_FILE', script)
        self.assertIn('flock -w "${RADAR_AUCTION_LOCK_WAIT_SEC:-3500}" 9', script)


if __name__ == "__main__":
    unittest.main()
