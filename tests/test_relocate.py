"""Moving the data warehouse to another drive: verified copy, config updated, old copy removed only after; failures change nothing."""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import relocate as R  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def setup():
    root = Path(tempfile.mkdtemp())
    (root / "config.json").write_text(json.dumps({"env": "demo", "port": 8765}), "utf-8")
    c = sqlite3.connect(str(root / "warehouse.db"))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE wh_trades (trade_id TEXT, ticker TEXT, ts REAL)")
    c.execute("CREATE TABLE wh_markets (ticker TEXT PRIMARY KEY)")
    c.executemany("INSERT INTO wh_trades VALUES(?,?,?)", [(f"t{i}", "X", i) for i in range(5000)])
    c.execute("INSERT INTO wh_markets VALUES('X')")
    c.commit()
    c.close()
    return root


def main():
    root = setup()
    check("no request file -> nothing happens", R.relocate(root, root / "config.json", placeholder=False) == {"done": False, "reason": "no request"})
    target = Path(tempfile.mkdtemp()) / "KalshiDesk-data"
    (root / "move-data.request").write_text(str(target), "utf-8")
    r = R.relocate(root, root / "config.json", placeholder=False)
    cfg = json.loads((root / "config.json").read_text("utf-8"))
    moved = sqlite3.connect(str(target / "warehouse.db"))
    n = moved.execute("SELECT COUNT(*) FROM wh_trades").fetchone()[0]
    moved.close()
    check("moved: every row is in the new file, config points there, the old copy is gone, request consumed",
          r["done"] and n == 5000 and cfg["warehouse_dir"] == str(target) and not (root / "warehouse.db").exists()
          and not (root / "move-data.request").exists() and cfg["env"] == "demo", (r, n, cfg))
    log = (root / "logs" / "move-data.txt").read_text("utf-8")
    check("each step is written to logs/move-data.txt", "verified" in log and "freed" in log, log[-300:])

    # not enough room: nothing changes
    root2 = setup()
    t2 = Path(tempfile.mkdtemp()) / "d"
    (root2 / "move-data.request").write_text(str(t2), "utf-8")
    real = R.shutil.disk_usage
    R.shutil.disk_usage = lambda p: type("U", (), {"free": 10})()
    r2 = R.relocate(root2, root2 / "config.json", placeholder=False)
    R.shutil.disk_usage = real
    cfg2 = json.loads((root2 / "config.json").read_text("utf-8"))
    check("not enough room on the target -> nothing moved, config unchanged, original kept", not r2["done"] and "room" in r2["reason"]
          and (root2 / "warehouse.db").exists() and "warehouse_dir" not in cfg2 and not (t2 / "warehouse.db").exists(), r2)
    # target already has a warehouse: never overwritten
    root3 = setup()
    t3 = Path(tempfile.mkdtemp())
    (t3 / "warehouse.db").write_text("someone else's file", "utf-8")
    (root3 / "move-data.request").write_text(str(t3), "utf-8")
    r3 = R.relocate(root3, root3 / "config.json", placeholder=False)
    check("an existing warehouse.db on the target is never overwritten", not r3["done"] and (t3 / "warehouse.db").read_text("utf-8") == "someone else's file"
          and (root3 / "warehouse.db").exists(), r3)
    # a bad copy is caught and removed
    root4 = setup()
    t4 = Path(tempfile.mkdtemp()) / "e"
    (root4 / "move-data.request").write_text(str(t4), "utf-8")
    real_copy = R.shutil.copyfile
    R.shutil.copyfile = lambda a, b: Path(b).write_bytes(Path(a).read_bytes()[:-4096])
    r4 = R.relocate(root4, root4 / "config.json", placeholder=False)
    R.shutil.copyfile = real_copy
    check("a damaged copy is detected and deleted; the original stays in use", not r4["done"] and not (t4 / "warehouse.db.partial").exists()
          and (root4 / "warehouse.db").exists() and "warehouse_dir" not in json.loads((root4 / "config.json").read_text("utf-8")), r4)

    # the app opens the warehouse where the config says
    from app.config import Config
    from app.db import DB
    from app.server import create_app
    fresh = Path(tempfile.mkdtemp()) / "data"
    fresh.mkdir()
    cfgp = Path(tempfile.mkdtemp()) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "warehouse_dir": str(fresh), "ws_enabled": False}), "utf-8")
    c = Config(cfgp)
    app = create_app(c, DB(cfgp.parent / "t.db"), start_engine=False)
    check("the app opens the warehouse where the config's warehouse_dir says", Path(app.state.wdb.path).parent == fresh and (fresh / "warehouse.db").exists(), app.state.wdb.path)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
