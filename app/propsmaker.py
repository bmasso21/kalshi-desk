"""Player props, paper test: resting bids at the back of the queue, 1 to 24 hours before the game.

The trade study on Kalshi's NBA and NFL player-prop markets (13,143 markets, Jul-Nov 2026, run 25 Nov 2026) found the
same pattern as Mentions: the side that crosses the spread LOSES from 1 to 24 hours before the close (resting side
+3.3c at 1-6 h, t 2.6; +5.9c at 6-24 h, t 3.5) and WINS in the last hour (takers +6.0c, t 7.2 - lineup news).
Cheap props (10-29c) were the worst buys (-4.6c and -6.3c for takers).

So this is the Mentions paper test pointed at props: one resting bid per side at the best bid on every prop market
closing in 1-24 hours, cancelled one hour before the close; filled only by real trades after the queue ahead of it;
held to settlement. Read-only public market data. Places no orders."""
from __future__ import annotations

from .mentions import MentionsPaper

PROP_SERIES = ("KXNBAPTSPROT", "KXNBAREBPROT", "KXNBA3PTPROT", "KXNFLPASSTDS", "KXNFLTD")


class PropsMaker(MentionsPaper):
    TABLE = "prop_orders"
    CATEGORY = "player-prop"

    def series(self, now: float) -> list[str]:
        ss = list(self.opt("prop_series", PROP_SERIES))
        self.state["series"] = len(ss)
        return ss
