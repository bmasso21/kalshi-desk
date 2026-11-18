"""Full-catalogue summary, crypto price history for the fair-value study, and min-volume sampling."""
import datetime as dt
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import catalog as C  # noqa: E402
from app import goldfv as G  # noqa: E402
from app import studies as ST  # noqa: E402
from app.warehouse import Warehouse, WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


class FakeClient:
    """Public market data only."""
    def __init__(self, series, markets):
        self.series, self.markets = series, markets
        self.calls = []

    def paginate(self, path, key, params, max_items=None, auth=False):
        self.calls.append((path, dict(params)))
        src = self.series if path == "/series" else self.markets
        for i, x in enumerate(src):
            if max_items and i >= max_items:
                return
            yield x


def main():
    series = [{"ticker": "KXBTCD", "category": "Crypto", "frequency": "hourly", "title": "Bitcoin above/below",
               "settlement_sources": [{"name": "CF Benchmarks"}]},
              {"ticker": "KXMENTION", "category": "Mentions", "frequency": "custom", "title": "What will he say"},
              {"ticker": "KXNOTHING", "category": "Politics", "frequency": "annual", "title": "Quiet"}]
    markets = [{"series_ticker": "KXBTCD", "event_ticker": "KXBTCD-1", "volume_24h_fp": "1000", "open_interest_fp": "50"},
               {"series_ticker": "KXBTCD", "event_ticker": "KXBTCD-1", "volume_24h_fp": "500", "open_interest_fp": "10"},
               {"event_ticker": "KXMENTION-9", "volume_24h": 20, "open_interest": 7}]          # no series_ticker: taken from the event
    s = C.summarize(series, markets)
    top = {r["series"]: r for r in s["top_series"]}
    check("catalogue: open markets grouped by series with 24h volume, open interest and events, busiest first",
          s["top_series"][0]["series"] == "KXBTCD" and top["KXBTCD"]["volume_24h"] == 1500 and top["KXBTCD"]["open_events"] == 1
          and top["KXMENTION"]["open_markets"] == 1 and top["KXBTCD"]["settles_on"] == "CF Benchmarks", s["top_series"])
    cats = {c["category"]: c for c in s["categories"]}
    check("catalogue: per category, listed vs open series", cats["Politics"]["series_listed"] == 1 and cats["Politics"]["series_open"] == 0
          and cats["Crypto"]["volume_24h"] == 1500 and s["series_listed"] == 3 and s["open_markets"] == 3, cats)
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    fc = FakeClient(series, markets)
    cat = C.Catalog(wdb, fc)
    cat.run()
    got = cat.latest()
    check("catalogue run: only reads series and OPEN markets (no combos), stores the summary",
          got["catalog"]["open_markets"] == 3 and got["state"]["error"] is None
          and any(p == "/markets" and q.get("status") == "open" and q.get("mve_filter") == "exclude" for p, q in fc.calls), got["state"])
    check("the client it is given has no order methods", not any(hasattr(fc, a) for a in ("create_order", "cancel_order")))

    # ---- Coinbase one-minute history ----
    class Resp:
        def __init__(self, j):
            self.j = j

        def raise_for_status(self):
            pass

        def json(self):
            return self.j

    class Ses:
        def __init__(self):
            self.params = []

        def get(self, url, params=None, headers=None, timeout=None):
            assert "api.exchange.coinbase.com/products/BTC-USD/candles" in url and params["granularity"] == 60
            self.params.append(params)
            a = dt.datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
            b = dt.datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
            return Resp([[int(t), 1, 2, 3, 64000 + (t % 7), 5] for t in range(int(b) - 60, int(a) - 1, -60)])   # newest first, like Coinbase
    G.time.sleep = lambda x: None
    ses = Ses()
    t0 = 1_790_000_000 // 60 * 60
    bars = G.fetch_coinbase(ses, "BTC-USD", t0, t0 + 700 * 60)
    check("Coinbase: 300 minutes per request, keyed by bar start, close price kept", len(ses.params) == 3 and len(bars) == 700
          and bars[t0] == 64000 + (t0 % 7), (len(ses.params), len(bars)))
    check("the fair-value study picks the free price history by series (gold: futures; Bitcoin/Ethereum: Coinbase)",
          ST.UNDERLYING["KXBTCD"] == ("coinbase", ("BTC-USD",)) and ST.UNDERLYING["KXETHD"][0] == "coinbase" and ST.UNDERLYING["KXGOLDH"][0] == "yahoo")

    # ---- min-volume sampling ----
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda h: (now - dt.timedelta(hours=h)).isoformat()
    mk = [{"ticker": f"KXBTCD-X-T{i}", "event_ticker": "KXBTCD-X", "result": "no", "close_time": iso(i % 20 + 1), "volume_fp": "0" if i % 3 else "12"} for i in range(30)]
    wdb2 = WarehouseDB(Path(tempfile.mkdtemp()) / "w2.db")
    eng = types.SimpleNamespace(cfg=types.SimpleNamespace(data={"warehouse": {"sample": {"overrides": {"KXBTCD": {"min_volume": 1}}}}}))
    wh = Warehouse(eng, wdb2, client=FakeClient([], mk))
    n = wh._sync_sample_markets({"ticker": "KXBTCD", "category": "Crypto"}, 30, 100)
    kept = wdb2.query("SELECT ticker FROM wh_markets")
    check("sampling with min_volume keeps only contracts somebody traded", n == 10 and len(kept) == 10, (n, len(kept)))
    eng.cfg.data["warehouse"]["sample"]["overrides"] = {}
    wdb3 = WarehouseDB(Path(tempfile.mkdtemp()) / "w3.db")
    wh3 = Warehouse(eng, wdb3, client=FakeClient([], mk))
    check("without it every settled contract is kept (unchanged behaviour)", wh3._sync_sample_markets({"ticker": "KXBTCD", "category": "Crypto"}, 30, 100) == 30)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
