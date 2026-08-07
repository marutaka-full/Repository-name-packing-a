#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 5 — 統合スケジューラ
監視台帳の頻度区分を daily / weekly ジョブに対応させ、層1〜層0/2/3を一括起動する。
cron や GitHub Actions からはこのファイルを daily / weekly 引数で呼ぶ。

使い方:
  python3 run_scheduler.py daily    # 日次ジョブ（更新一覧・層1日次・層0・層3）
  python3 run_scheduler.py weekly   # 週次ジョブ（層1週次・層2告示）
  python3 run_scheduler.py all      # 全部まとめて
  python3 run_scheduler.py daily --dry-run

cron例:
  0 7 * * *  cd /path/to && python3 run_scheduler.py daily  >> monitor.log 2>&1
  0 8 * * 1  cd /path/to && python3 run_scheduler.py weekly >> monitor.log 2>&1
"""
import subprocess
import sys
from datetime import datetime

PY = sys.executable

JOBS = {
    "daily":  [["monitor_layer1.py"], ["monitor_layer235.py"]],
    "weekly": [["monitor_layer1.py"], ["monitor_layer235.py"]],
    # 実運用では layer1 側に --freq daily/weekly フィルタを足して分離可能。
    # ここでは両ジョブとも全件チェック（過剰チェックは無害、取りこぼし防止優先）。
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("daily", "weekly", "all"):
        print("usage: run_scheduler.py [daily|weekly|all] [--dry-run]")
        sys.exit(2)
    mode = sys.argv[1]
    extra = ["--dry-run"] if "--dry-run" in sys.argv else []
    modes = ["daily", "weekly"] if mode == "all" else [mode]

    print(f"=== スケジューラ起動 mode={mode} {datetime.now().isoformat(timespec='seconds')} ===")
    seen = set()
    failures = 0
    for m in modes:
        for job in JOBS[m]:
            script = job[0]
            if script in seen:           # all指定時の二重起動防止
                continue
            seen.add(script)
            cmd = [PY] + job + extra
            print(f"\n--- 実行: {' '.join(cmd)} ---")
            try:
                r = subprocess.run(cmd, timeout=600)
                if r.returncode != 0:
                    failures += 1
                    print(f"[警告] {script} が非ゼロ終了 ({r.returncode})")
            except subprocess.TimeoutExpired:
                failures += 1
                print(f"[警告] {script} タイムアウト")
    print(f"\n=== スケジューラ完了 失敗ジョブ {failures}件 ===")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
