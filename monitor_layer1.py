#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 4 — 層1（法律・政令・省令）監視コレクタ
e-Gov法令API V2 で監視対象の現行リビジョンを取得し、前回スナップショットと
比較して変更を検知 → change_log（追記専用JSONL）へ記録 → Slack通知。

使い方:
  python3 monitor_layer1.py             # 本番実行（要ネットワーク／Dさんの環境）
  python3 monitor_layer1.py --selftest  # ネットワーク不要。差分エンジンを実証検証
  python3 monitor_layer1.py --dry-run   # 取得はするが通知はコンソール出力のみ

環境変数:
  SLACK_WEBHOOK_URL  設定時のみ Slack へ送信（未設定ならコンソール出力）

注意:
  本スクリプトは laws.e-gov.go.jp へ通信します。Anthropicサンドボックスからは
  当該ホストが遮断（x-deny-reason: host_not_allowed を実測）されているため、
  本番実行は必ずローカル／サーバ環境で行ってください。
出典: e-Gov法令API（デジタル庁）を使用（政府標準利用規約 第2.0版・出典明示義務）
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE_URL = "https://laws.e-gov.go.jp/api/2"
CONFIG_PATH = "config_laws.json"
SNAPSHOT_DIR = "snapshots"
CHANGE_LOG = "change_log.jsonl"
TIMEOUT = 30
THROTTLE_SEC = 1.0          # 短時間の大量リクエスト回避（API運用注意に準拠）
USER_AGENT = "ginou-jisshu-monitor/1.0 (legal-compliance)"


# ============================================================
# 取得
# ============================================================
def _get_json(path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_revision(law_id):
    """/law_revisions/{id} を取得。失敗時は /laws フォールバック。"""
    try:
        return _get_json(f"/law_revisions/{law_id}", {"response_format": "json"})
    except urllib.error.HTTPError:
        # フォールバック: /laws で現行リビジョンだけ取得
        return _get_json("/laws", {"law_id": law_id, "limit": 1,
                                   "response_format": "json"})


def extract_current_revision(api_json):
    """
    APIレスポンスの形が複数あり得るため、防御的に現行リビジョン情報を取り出す。
    返り値: dict(law_revision_id, enforcement_date, scheduled_enforcement_date,
                 amendment_law_num, title) いずれも無ければ None 値。
    """
    def pick(d):
        if not isinstance(d, dict):
            return None
        return {
            "law_revision_id": d.get("law_revision_id"),
            "enforcement_date": d.get("amendment_enforcement_date")
                                or d.get("enforcement_date"),
            "scheduled_enforcement_date": d.get("amendment_scheduled_enforcement_date")
                                or d.get("scheduled_enforcement_date"),
            "amendment_law_num": d.get("amendment_law_num"),
            "title": d.get("law_title") or d.get("title"),
        }

    # 形1: /laws → {"laws":[{"revision_info":{...}}]}
    if isinstance(api_json.get("laws"), list) and api_json["laws"]:
        ri = api_json["laws"][0].get("revision_info")
        r = pick(ri)
        if r and r["law_revision_id"]:
            return r

    # 形2: /law_revisions → 配列。最新（施行日最大）を採用
    for key in ("law_revisions", "revisions"):
        arr = api_json.get(key)
        if isinstance(arr, list) and arr:
            def keyf(x):
                return x.get("amendment_enforcement_date") or x.get("enforcement_date") or ""
            latest = sorted(arr, key=keyf)[-1]
            r = pick(latest)
            if r and r["law_revision_id"]:
                return r

    # 形3: 直接 revision_info を持つ
    for key in ("revision_info", "current_revision_info"):
        r = pick(api_json.get(key))
        if r and r["law_revision_id"]:
            return r

    return None


# ============================================================
# スナップショット永続化
# ============================================================
def load_snapshot(law_id):
    p = os.path.join(SNAPSHOT_DIR, f"{law_id}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_snapshot(law_id, rev):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    p = os.path.join(SNAPSHOT_DIR, f"{law_id}.json")
    rec = dict(rev)
    rec["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)


# ============================================================
# 差分検知
# ============================================================
def classify_severity(field, old, new):
    """層1の重要度判定。設計書 §4 準拠。"""
    if field == "scheduled_enforcement_date" and new and not old:
        return "高"                       # 未施行予定日の新規出現（育成就労等）
    if field == "law_revision_id":
        return "高"                       # 改正が反映＝施行された
    if field == "enforcement_date":
        return "高"
    return "中低"


def diff(prev, curr):
    """前回スナップショット prev と今回 curr を比較し変更レコード列を返す。"""
    changes = []
    if prev is None:
        # 初回登録は基準作成のみ（変更ではない）
        return changes
    for field in ("law_revision_id", "enforcement_date",
                  "scheduled_enforcement_date"):
        old = prev.get(field)
        new = curr.get(field)
        if old != new:
            changes.append({
                "field": field,
                "old_value": old,
                "new_value": new,
                "severity": classify_severity(field, old, new),
            })
    return changes


# ============================================================
# 監査証跡（追記専用）
# ============================================================
def append_change_log(law_id, name, change):
    rec = {
        "change_id": None,  # 連番は読み出し側で付与可。ここでは時刻で一意化
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "law_id": law_id,
        "name": name,
        "layer": 1,
        "field": change["field"],
        "old_value": change["old_value"],
        "new_value": change["new_value"],
        "severity": change["severity"],
        "source_url": f"https://laws.e-gov.go.jp/law/{law_id}",
    }
    with open(CHANGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# ============================================================
# 通知
# ============================================================
def notify(records, dry_run=False):
    if not records:
        return
    high = [r for r in records if r["severity"] == "高"]
    lines = [f"【法令監視】変更 {len(records)}件（高:{len(high)}）", ""]
    for r in records:
        mark = "🔴" if r["severity"] == "高" else "🟡"
        lines.append(f"{mark} {r['name']}（{r['law_id']}）")
        lines.append(f"    {r['field']}: {r['old_value']} → {r['new_value']}")
        lines.append(f"    {r['source_url']}")
    lines.append("")
    lines.append("出典: e-Gov法令API（デジタル庁）を使用")
    text = "\n".join(lines)

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if dry_run or not webhook:
        print("---- 通知（コンソール出力）----")
        print(text)
        return
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT)
        print(f"Slack通知済: {len(records)}件")
    except urllib.error.URLError as e:
        print(f"[Slack通知失敗] {e} — コンソール出力に切替")
        print(text)


# ============================================================
# メイン
# ============================================================
def run(dry_run=False):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        laws = json.load(f)["laws"]

    all_records = []
    print(f"監視開始: {len(laws)}件  {datetime.now().isoformat(timespec='seconds')}")
    for i, law in enumerate(laws, 1):
        lid, name = law["law_id"], law["name"]
        try:
            data = fetch_revision(lid)
            curr = extract_current_revision(data)
        except urllib.error.HTTPError as e:
            print(f"  [{i}/{len(laws)}] {name}: HTTP {e.code}")
            continue
        except urllib.error.URLError as e:
            print(f"  [{i}/{len(laws)}] {name}: 接続エラー {e.reason}")
            continue
        if not curr:
            print(f"  [{i}/{len(laws)}] {name}: リビジョン抽出失敗（要構造確認）")
            continue

        prev = load_snapshot(lid)
        for ch in diff(prev, curr):
            rec = append_change_log(lid, name, ch)
            all_records.append(rec)
            print(f"  [変更] {name}: {ch['field']} {ch['old_value']}→{ch['new_value']} ({ch['severity']})")
        save_snapshot(lid, curr)
        if prev is None:
            print(f"  [{i}/{len(laws)}] {name}: 基準スナップショット作成")
        time.sleep(THROTTLE_SEC)

    notify(all_records, dry_run=dry_run)
    print(f"監視完了: 変更 {len(all_records)}件")


# ============================================================
# 自己検証（ネットワーク不要・差分エンジンの実証）
# ============================================================
def selftest():
    print("=" * 60)
    print(" SELFTEST: 差分エンジン実証（ネットワーク不要）")
    print("=" * 60)
    passed = total = 0

    def check(label, cond):
        nonlocal passed, total
        total += 1
        ok = bool(cond)
        passed += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        return ok

    # ケースA: 実例（施行規則 2026-01-30改正） revision_id 変化 → 高
    prevA = {"law_revision_id": "428M60000110003_20240401_504M60000110001",
             "enforcement_date": "2024-04-01", "scheduled_enforcement_date": None}
    currA = {"law_revision_id": "428M60000110003_20260130_508M60000110001",
             "enforcement_date": "2026-01-30", "scheduled_enforcement_date": None}
    chA = diff(prevA, currA)
    fields = {c["field"]: c for c in chA}
    check("A: revision_id変化を検知", "law_revision_id" in fields)
    check("A: revision_id変化は重要度=高", fields.get("law_revision_id", {}).get("severity") == "高")
    check("A: enforcement_date変化も検知", "enforcement_date" in fields)

    # ケースB: 未施行予定日の新規出現（育成就労 2027-04-01）→ 高
    prevB = {"law_revision_id": "326CO0000000319_20250601_x",
             "enforcement_date": "2025-06-01", "scheduled_enforcement_date": None}
    currB = {"law_revision_id": "326CO0000000319_20250601_x",
             "enforcement_date": "2025-06-01", "scheduled_enforcement_date": "2027-04-01"}
    chB = {c["field"]: c for c in diff(prevB, currB)}
    check("B: scheduled日の新規出現を検知", "scheduled_enforcement_date" in chB)
    check("B: scheduled新規は重要度=高",
          chB.get("scheduled_enforcement_date", {}).get("severity") == "高")

    # ケースC: 変化なし → 0件
    chC = diff(currA, dict(currA))
    check("C: 無変化は0件", len(chC) == 0)

    # ケースD: 初回（prev=None）→ 変更扱いしない
    chD = diff(None, currA)
    check("D: 初回は変更0件（基準作成のみ）", len(chD) == 0)

    # ケースE: extract_current_revision が /laws 形を解釈
    sample_laws = {"laws": [{"revision_info": {
        "law_revision_id": "428AC0000000089_20250601_504AC0000000068",
        "amendment_enforcement_date": "2025-06-01",
        "amendment_scheduled_enforcement_date": None,
        "amendment_law_num": "令和四年法律第六十八号",
        "law_title": "外国人の技能実習の適正な実施及び技能実習生の保護に関する法律"}}]}
    ex = extract_current_revision(sample_laws)
    check("E: /laws形からrevision_id抽出",
          ex and ex["law_revision_id"] == "428AC0000000089_20250601_504AC0000000068")

    # ケースF: extract が /law_revisions 配列形（最新採用）を解釈
    sample_rev = {"law_revisions": [
        {"law_revision_id": "x_20180101_a", "amendment_enforcement_date": "2018-01-01"},
        {"law_revision_id": "x_20260130_b", "amendment_enforcement_date": "2026-01-30"},
    ]}
    ex2 = extract_current_revision(sample_rev)
    check("F: 配列形から最新(2026-01-30)を採用",
          ex2 and ex2["law_revision_id"] == "x_20260130_b")

    print("-" * 60)
    print(f" 結果: {passed}/{total} PASS")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    run(dry_run="--dry-run" in sys.argv)
