#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 5 — 層0/2/3 コレクタ
  層0: e-Govパブリックコメントをキーワード照合し、改正の予兆を検知
  層2: 告示の集約元（OTIT/入管庁）ページのHTMLハッシュ差分
  層3: 通知・運用要領（OTIT/法務省/厚労省）ページのHTMLハッシュ差分
  ※官報(kanpo.go.jp)はクローラ禁止（利用ルール）のため自動取得せず、確認用リンクのみ。

monitor_layer1.py の差分基盤（change_log/通知）を共有する設計。
本ファイルは層0/2/3固有の取得・正規化・ハッシュ化を担う。

使い方:
  python3 monitor_layer235.py --selftest   # ネットワーク不要。ハッシュ/照合ロジックを実証
  python3 monitor_layer235.py --dry-run     # 取得するが通知はコンソールのみ
  python3 monitor_layer235.py               # 本番（要 SLACK_WEBHOOK_URL）

注意: 省庁サイトへ通信します。Anthropicサンドボックスからは外部ホストが遮断
（host_not_allowed 実測）されるため、本番実行はローカル/サーバ環境で。
出典: e-Govパブリックコメント等。各省庁公開情報。官報は官報発行サイト。
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

CONFIG_PATH = "config_sources.json"
SNAPSHOT_DIR = "snapshots_doc"
CHANGE_LOG = "change_log.jsonl"     # layer1と共有（追記専用）
TIMEOUT = 30
THROTTLE_SEC = 1.0
USER_AGENT = "ginou-jisshu-monitor/1.0 (legal-compliance)"


# ============================================================
# 取得・正規化
# ============================================================
def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    # 文字コードは概ねUTF-8。失敗時は寛容にデコード
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp932", errors="replace")


def normalize_text(html):
    """
    差分の安定化のため、本文だけを荒く抽出して正規化する。
    - script/style/コメント除去
    - タグ除去
    - 日付・時刻・カウンタ等の揺れを吸収（空白圧縮）
    完全なHTMLパースはしない（依存を増やさない方針）。誤検知抑制が目的。
    """
    h = re.sub(r"(?is)<script.*?</script>", " ", html)
    h = re.sub(r"(?is)<style.*?</style>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)          # タグ除去
    h = re.sub(r"&[a-zA-Z#0-9]+;", " ", h)       # 実体参照
    h = re.sub(r"\s+", " ", h)                    # 空白圧縮
    return h.strip()


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# 層0: パブコメ キーワード照合
# ============================================================
def scan_pubcomment(cfg):
    """
    意見公募ページを取得し、設定キーワードに合致する案件名を抽出。
    公布前の改正予兆を拾う。ページ構造に依存しすぎないよう、正規化テキストから
    キーワード近傍を抜き出す素朴な実装（精緻化は所管ページのDOMに合わせて拡張）。
    """
    hits = []
    try:
        html = fetch_html(cfg["search_url"])
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return {"error": str(e), "hits": []}
    text = normalize_text(html)
    for kw in cfg["keywords"]:
        for m in re.finditer(re.escape(kw), text):
            s = max(0, m.start() - 30)
            e = min(len(text), m.end() + 30)
            hits.append({"keyword": kw, "context": text[s:e]})
    # 重複文脈の圧縮
    uniq = {h["context"]: h for h in hits}
    return {"error": None, "hits": list(uniq.values())}


# ============================================================
# スナップショット（doc系: ハッシュ保存）
# ============================================================
def _snap_path(key):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return os.path.join(SNAPSHOT_DIR, f"{safe}.json")


def load_doc_snapshot(key):
    p = _snap_path(key)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_doc_snapshot(key, h, url):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(_snap_path(key), "w", encoding="utf-8") as f:
        json.dump({"key": key, "content_hash": h, "source_url": url,
                   "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                  f, ensure_ascii=False, indent=2)


# ============================================================
# 監査証跡（layer1と同形式・追記専用）
# ============================================================
def append_change_log(rec):
    with open(CHANGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def make_change(layer, name, field, old, new, severity, url):
    return {
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "law_id": None, "name": name, "layer": layer,
        "field": field, "old_value": old, "new_value": new,
        "severity": severity, "source_url": url,
    }


# ============================================================
# 通知（layer1と同じ整形方針）
# ============================================================
def notify(records, dry_run=False):
    if not records:
        print("変更なし（通知なし）")
        return
    high = [r for r in records if r["severity"] == "高"]
    lines = [f"【法令監視 層0/2/3】変更 {len(records)}件（高:{len(high)}）", ""]
    for r in records:
        mark = "🔴" if r["severity"] == "高" else "🟡"
        lines.append(f"{mark} [{r['layer']}] {r['name']}")
        lines.append(f"    {r['field']}: {r['new_value']}")
        lines.append(f"    {r['source_url']}")
    lines += ["", "出典: e-Govパブリックコメント／各省庁公開情報を使用"]
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
    except urllib.error.URLError:
        print(text)


# ============================================================
# メイン
# ============================================================
def run(dry_run=False):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    records = []
    print(f"層0/2/3 監視開始: {datetime.now().isoformat(timespec='seconds')}")

    # --- 層0: パブコメ予兆 ---
    p = cfg["layer0_pubcomment"]
    res = scan_pubcomment(p)
    if res["error"]:
        print(f"  [層0] 取得失敗: {res['error']}")
    else:
        key = "pubcomment_hits"
        prev = load_doc_snapshot(key)
        new_hash = content_hash(json.dumps(sorted(h["context"] for h in res["hits"]),
                                           ensure_ascii=False))
        if res["hits"] and (prev is None or prev["content_hash"] != new_hash):
            for h in res["hits"]:
                rec = make_change("0", "パブコメ予兆: " + h["keyword"],
                                  "意見公募で言及", None, h["context"], "高", p["search_url"])
                append_change_log(rec); records.append(rec)
        save_doc_snapshot(key, new_hash, p["search_url"])
        print(f"  [層0] パブコメ ヒット {len(res['hits'])}件")

    # --- 層2/3: HTMLハッシュ差分（manual_link_onlyは取得しない）---
    for layer_key, layer_no in (("layer2_kokuji", "2"), ("layer3_docs", "3")):
        for src in cfg[layer_key]:
            if src["method"] == "manual_link_only":
                print(f"  [層{layer_no}] {src['name']}: 確認用リンクのみ（自動取得せず）")
                continue
            try:
                html = fetch_html(src["url"])
            except (urllib.error.URLError, urllib.error.HTTPError) as e:
                print(f"  [層{layer_no}] {src['name']}: 取得失敗 {e}")
                continue
            h = content_hash(normalize_text(html))
            prev = load_doc_snapshot(src["url"])
            if prev is None:
                print(f"  [層{layer_no}] {src['name']}: 基準作成")
            elif prev["content_hash"] != h:
                sev = "高" if layer_no == "3" else "中低"
                rec = make_change(layer_no, src["name"], "ページ内容ハッシュ",
                                  prev["content_hash"][:12], h[:12], sev, src["url"])
                append_change_log(rec); records.append(rec)
                print(f"  [層{layer_no}] {src['name']}: 変更検知 ({sev})")
            else:
                print(f"  [層{layer_no}] {src['name']}: 変更なし")
            save_doc_snapshot(src["url"], h, src["url"])
            time.sleep(THROTTLE_SEC)

    notify(records, dry_run=dry_run)
    print(f"層0/2/3 完了: 変更 {len(records)}件")


# ============================================================
# 自己検証（ネットワーク不要）
# ============================================================
def selftest():
    print("=" * 60)
    print(" SELFTEST: 層0/2/3 ロジック実証（ネットワーク不要）")
    print("=" * 60)
    passed = total = 0

    def check(label, cond):
        nonlocal passed, total
        total += 1
        ok = bool(cond); passed += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        return ok

    # A: normalize_text がタグ/script/空白を除去
    html = "<html><script>x=1</script><body>  技能実習\n  施行規則 <b>改正</b> </body></html>"
    nt = normalize_text(html)
    check("A: scriptタグ内容を除去", "x=1" not in nt)
    check("A: 本文は保持", "技能実習" in nt and "改正" in nt)
    check("A: 空白圧縮", "  " not in nt)

    # B: 同一内容→同一ハッシュ / 差異→別ハッシュ
    h1 = content_hash(normalize_text("<p>育成就労 運用要領</p>"))
    h2 = content_hash(normalize_text("<p>育成就労   運用要領</p>"))  # 空白だけ違う
    h3 = content_hash(normalize_text("<p>育成就労 運用要領 改正</p>"))
    check("B: 空白差は同一ハッシュ（誤検知抑制）", h1 == h2)
    check("B: 内容変化は別ハッシュ（検知）", h1 != h3)

    # C: パブコメ キーワード照合（疑似HTML）
    cfg = {"search_url": "x", "keywords": ["育成就労", "監理"]}
    fake = "<li>育成就労制度に関する省令案の意見募集</li><li>道路法施行令</li>"
    import types
    # fetch_html を一時差し替え
    g = globals(); orig = g["fetch_html"]
    g["fetch_html"] = lambda u: fake
    try:
        res = scan_pubcomment(cfg)
    finally:
        g["fetch_html"] = orig
    kws = {h["keyword"] for h in res["hits"]}
    check("C: 育成就労を予兆検知", "育成就労" in kws)
    check("C: 無関係案件は拾わない（監理は不在）", "監理" not in kws)

    print("-" * 60)
    print(f" 結果: {passed}/{total} PASS")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    run(dry_run="--dry-run" in sys.argv)
