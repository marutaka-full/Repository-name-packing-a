#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 1 検証スクリプト
技能実習法（平成28年法律第89号 / 法令ID: 428AC0000000089）の
改正履歴を e-Gov 法令API Version2 から取得し、構造を確認する。

【動作環境について】
  本スクリプトは laws.e-gov.go.jp への外部通信を行います。
  Anthropic のサンドボックスからは当該ホストへの通信が遮断されている
  （x-deny-reason: host_not_allowed を実測）ため、必ず D さんご自身の
  ローカル環境・サーバ等で実行してください。

【依存】
  追加インストール不要（Python 3.8+ 標準ライブラリのみ）

【出典明示義務】
  取得データを公開・配布する場合は「e-Gov法令APIを使用している」旨を
  明示してください（政府標準利用規約 第2.0版）。
"""

import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

# ============================================================
# 設定
# ============================================================
BASE_URL = "https://laws.e-gov.go.jp/api/2"
TARGET_LAW_ID = "428AC0000000089"      # 技能実習法
TARGET_LAW_TITLE = "外国人の技能実習の適正な実施及び技能実習生の保護に関する法律"
TIMEOUT_SEC = 30
USER_AGENT = "ginou-jisshu-monitor/1.0 (legal-compliance; contact: D)"


def _get_json(path, params=None):
    """GET リクエストを送り JSON を返す。エラー時は例外を送出。"""
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def step1_lookup_current():
    """/laws で現行リビジョン情報を取得し、法令IDの整合を確認する。"""
    print("【Step 1】/laws で現行リビジョン情報を取得")
    data = _get_json("/laws", {
        "law_title": TARGET_LAW_TITLE,
        "limit": 1,
        "response_format": "json",
    })
    total = data.get("total_count")
    print(f"  ヒット件数: {total}")
    if not data.get("laws"):
        print("  該当なし。タイトル指定を見直してください。")
        return None
    law = data["laws"][0]
    law_info = law.get("law_info", {})
    rev = law.get("revision_info", {})
    print(f"  法令ID         : {law_info.get('law_id')}")
    print(f"  法令番号       : {law_info.get('law_num')}")
    print(f"  公布日         : {law_info.get('promulgation_date')}")
    print(f"  現行リビジョンID: {rev.get('law_revision_id')}")
    print(f"  改正施行日     : {rev.get('amendment_enforcement_date')}")
    print(f"  改正法令       : {rev.get('amendment_law_num')}"
          f"（{rev.get('amendment_law_title')}）")
    sched = rev.get("amendment_scheduled_enforcement_date")
    if sched:
        print(f"  ★未施行の施行予定日: {sched}")
    # 法令IDの整合チェック
    if law_info.get("law_id") != TARGET_LAW_ID:
        print(f"  [警告] 想定ID {TARGET_LAW_ID} と不一致: {law_info.get('law_id')}")
    return law_info.get("law_id") or TARGET_LAW_ID


def step2_fetch_revisions(law_id):
    """/law_revisions/{id} で改正履歴の全件を取得する。"""
    print(f"\n【Step 2】/law_revisions/{law_id} で改正履歴を取得")
    data = _get_json(f"/law_revisions/{law_id}", {"response_format": "json"})

    # レスポンス構造を推測に頼らず、まず最上位キーを表示する
    print(f"  最上位キー: {list(data.keys())}")

    # 改正履歴の配列を取り出す（キー名の揺れに備えて候補を順に探す）
    revisions = None
    for key in ("law_revisions", "revisions", "revision_info"):
        if isinstance(data.get(key), list):
            revisions = data[key]
            print(f"  履歴配列キー: '{key}' / 件数: {len(revisions)}")
            break

    if revisions is None:
        print("  配列キーを自動判定できませんでした。全文を JSON 出力して構造を確認します。")
        return data

    # 履歴を施行日順で簡易表示
    print("\n  --- 改正履歴（抜粋表示）---")
    for i, r in enumerate(revisions, 1):
        rid = r.get("law_revision_id") or r.get("law_id") or "(id不明)"
        enf = r.get("amendment_enforcement_date") or r.get("enforcement_date") or "-"
        num = r.get("amendment_law_num") or "-"
        print(f"  {i:>3}. 施行 {enf} / {num} / {rid}")
    return data


def main():
    print("=" * 64)
    print(" 技能実習法 改正履歴 取得テスト（e-Gov 法令API V2）")
    print(f" 実行時刻: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 64)
    try:
        law_id = step1_lookup_current() or TARGET_LAW_ID
        time.sleep(1)  # サーバ負荷軽減
        full = step2_fetch_revisions(law_id)
    except urllib.error.HTTPError as e:
        print(f"\n[HTTPエラー] {e.code} {e.reason}")
        print("  403 host_not_allowed の場合はサンドボックス制限です。"
              "ローカル環境で実行してください。")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\n[接続エラー] {e.reason}")
        sys.exit(1)

    # 取得結果を JSON 保存（後続の差分比較の基準データになる）
    out_path = f"ginou_jisshu_revisions_{datetime.now():%Y%m%d}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)
    print(f"\n保存しました: {out_path}")
    print("出典: e-Gov法令API（デジタル庁）を使用")


if __name__ == "__main__":
    main()
