"""渠道归因漏斗：读取 Popto 后台只读导出的 CSV（srcreg / h_src 归因），入 funnel_daily。

把导出文件放到 ~/popto-superops-data/exports/funnel/*.csv，列：
date,channel_code,clicks,registrations,first_done,second_done,paid
python -m superops.biz.collectors.funnel import-latest
"""
from __future__ import annotations

import csv
import sys

from .. import db
from ...infra import paths
from ..scriptkit import emit_result

COLS = ["clicks", "registrations", "first_done", "second_done", "paid"]


def import_dir() -> tuple[int, int]:
    d = paths.ops_data() / "exports" / "funnel"
    d.mkdir(parents=True, exist_ok=True)
    ops = db.open_ops()
    files = rows = 0
    for f in sorted(d.glob("*.csv")):
        done = f.with_suffix(".csv.imported")
        if done.exists():
            continue
        with f.open(encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if not r.get("date") or not r.get("channel_code"):
                    continue
                vals = [int(float(r.get(c) or 0)) for c in COLS]
                ops.execute("INSERT OR REPLACE INTO funnel_daily(date,channel_code," + ",".join(COLS) + ") VALUES (?,?,?,?,?,?,?)",
                            (r["date"], r["channel_code"], *vals))
                rows += 1
        done.write_text("ok")
        files += 1
    return files, rows


if __name__ == "__main__":
    if sys.argv[1:2] == ["import-latest"]:
        f, r = import_dir()
        emit_result(f"导入 {f} 个漏斗导出文件、{r} 行" if f else "exports/funnel 下没有新的导出文件")
    else:
        print(__doc__)
        sys.exit(2)
