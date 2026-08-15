#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
law_text_sync.py - 条文保管・更新型
「関係法令すべての条文を手元に保存し、毎日実行すると最新条文を取り直して
 上書きし、前回からどの条が変わったかを差分表示する」を実現する。

毎日の実行で:
  1. 各法令の条文を e-Gov法令API /law_data/{id} で取得
  2. 条(Article)単位に分解して texts/{id}.json（比較用）と texts/{id}.txt（閲覧用）に保存
  3. 2回目以降は前回と条単位で比較し、追加/削除/変更された条だけを抽出
  4. 変更時は旧版を texts/_history/ に退避（証跡）し、新版で上書き
  5. diff_report_YYYYMMDD.txt に差分を出力し、change_log.jsonl にも記録

使い方:
  python law_text_sync.py --selftest   # ネットワーク不要。パース/差分を実証
  python law_text_sync.py --dry-run     # 取得・保存はするが change_log は控えめ
  python law_text_sync.py               # 本番（Dさんの環境で実行）

注意: laws.e-gov.go.jp へ通信します。Anthropicサンドボックスは遮断
（host_not_allowed 実測）のため本番はローカル/事業所PCで実行。
出典: e-Gov法令API（デジタル庁）を使用（政府標準利用規約 第2.0版・出典明示義務）
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import OrderedDict
from datetime import datetime, timezone

BASE_URL = "https://laws.e-gov.go.jp/api/2"
CONFIG_PATH = "config_laws.json"
TEXT_DIR = "texts"
HISTORY_DIR = os.path.join(TEXT_DIR, "_history")
CHANGE_LOG = "change_log.jsonl"
TIMEOUT = 60
THROTTLE_SEC = 1.0
USER_AGENT = "ginou-jisshu-monitor/1.0 (legal-compliance)"


def fetch_law_data(law_id):
    url = f"{BASE_URL}/law_data/{urllib.parse.quote(law_id)}?response_format=json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_law_full_text(api_json):
    """レスポンスから条文本体ノードを取り出す（キー名の揺れに対応）。"""
    for key in ("law_full_text", "lawFullText", "law_data"):
        if key in api_json:
            return api_json[key]
    return api_json


def walk_text(node):
    """部分木の文字列リーフを連結して本文テキストにする。"""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(walk_text(n) for n in node)
    if isinstance(node, dict):
        return walk_text(node.get("children", []))
    return ""


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_articles(node, out=None, section="main", sup_no=0, counter=None):
    """
    ツリー内の tag=='Article' を全て見つけ、{キー: 正規化本文} を返す。

    重要: 本則(MainProvision)と附則(SupplProvision)には同じ条番号が
    存在するため、番号だけを鍵にすると本則が附則で上書きされる。
    本則は "3"、附則は "附1_3"（1つ目の附則の第3条）と鍵を分ける。
    """
    if out is None:
        out = OrderedDict()
    if counter is None:
        counter = [0]

    if isinstance(node, dict):
        tag = node.get("tag")
        if tag == "Article":
            num = (node.get("attr") or {}).get("Num", "?")
            key = num if section == "main" else f"附{sup_no}_{num}"
            if key in out:                      # 想定外の衝突は退避して失わない
                i = 2
                while f"{key}#{i}" in out:
                    i += 1
                key = f"{key}#{i}"
            out[key] = normalize(walk_text(node))
            return out
        if tag == "MainProvision":
            section, sup_no = "main", 0
        elif tag == "SupplProvision":
            counter[0] += 1
            section, sup_no = "suppl", counter[0]
        for ch in node.get("children", []):
            extract_articles(ch, out, section, sup_no, counter)
    elif isinstance(node, list):
        for ch in node:
            extract_articles(ch, out, section, sup_no, counter)
    return out


def is_suppl(key):
    """附則の条かどうか。"""
    return str(key).startswith("附")


def article_sort_key(num):
    """本則を先、附則を後に置き、それぞれ自然順で並べる。

    '2' < '10' < '2_2'(第二条の二) の順序は従来どおり。
    附則は '附1_3' の形で、附則ブロック順 → 条番号順。
    """
    s = str(num)
    if s.startswith("附"):
        m = re.match(r"^附(\d+)_(.*)$", s)
        if m:
            sup, rest = int(m.group(1)), m.group(2)
        else:
            sup, rest = 9999, s.lstrip("附")
        parts = re.split(r"[^0-9]+", rest)
        return (1, sup) + tuple(int(p) for p in parts if p != "")
    parts = re.split(r"[^0-9]+", s)
    return (0, 0) + tuple(int(p) for p in parts if p != "")


def diff_articles(old_map, new_map):
    changes = []
    old_keys, new_keys = set(old_map), set(new_map)
    for k in sorted(new_keys - old_keys, key=article_sort_key):
        changes.append({"article": k, "status": "追加", "new": new_map[k]})
    for k in sorted(old_keys - new_keys, key=article_sort_key):
        changes.append({"article": k, "status": "削除", "old": old_map[k]})
    for k in sorted(old_keys & new_keys, key=article_sort_key):
        if old_map[k] != new_map[k]:
            changes.append({"article": k, "status": "変更",
                            "old": old_map[k], "new": new_map[k]})
    return changes


def load_prev_map(law_id):
    p = os.path.join(TEXT_DIR, f"{law_id}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=OrderedDict)
    return None


def sanitize_filename(name):
    """Windowsで使えない文字を全角/安全文字に置換してファイル名にする。"""
    table = {'\\': '＼', '/': '／', ':': '：', '*': '＊', '?': '？',
             '"': '”', '<': '＜', '>': '＞', '|': '｜'}
    for k, v in table.items():
        name = name.replace(k, v)
    return name.strip()


def save_current(law_id, name, art_map):
    os.makedirs(TEXT_DIR, exist_ok=True)
    # 比較用JSON（ファイル名は法令IDのまま・差分比較の基準。変更しないこと）
    with open(os.path.join(TEXT_DIR, f"{law_id}.json"), "w", encoding="utf-8") as f:
        json.dump(art_map, f, ensure_ascii=False, indent=1)
    # 閲覧用テキスト（ファイル名は法令名。例: 技能実習法.txt）
    lines = [f"{name}（{law_id}）",
             f"取得: {datetime.now().isoformat(timespec='seconds')}", ""]
    keys = sorted(art_map, key=article_sort_key)
    main_keys = [k for k in keys if not is_suppl(k)]
    sup_keys = [k for k in keys if is_suppl(k)]
    if main_keys:
        lines += ["【本則】", ""]
        for k in main_keys:
            lines.append(art_map[k]); lines.append("")
    if sup_keys:
        lines += ["【附則】", ""]
        for k in sup_keys:
            lines.append(art_map[k]); lines.append("")
    safe = sanitize_filename(name)
    with open(os.path.join(TEXT_DIR, f"{safe}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    # 旧版（法令ID名の閲覧用txt）が残っていれば整理
    legacy = os.path.join(TEXT_DIR, f"{law_id}.txt")
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
        except OSError:
            pass


def write_index(index_rows):
    """法令名<->ID<->条数<->状態の対応表を texts/_対応表.txt に出力。"""
    if not index_rows:
        return
    os.makedirs(TEXT_DIR, exist_ok=True)
    path = os.path.join(TEXT_DIR, "_対応表.txt")
    lines = ["法令 対応表（ファイル名の手引き）",
             f"作成: {datetime.now().isoformat(timespec='seconds')}",
             "出典: e-Gov法令API（デジタル庁）を使用", "",
             f"{'法令名':<32}{'法令ID':<20}{'条数':>6}  状態",
             "-" * 70]
    for r in index_rows:
        lines.append(f"{r['name']:<32}{r['law_id']:<20}{str(r['count']):>6}  {r['status']}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def archive_prev(law_id):
    """上書き前に旧版を _history/ へ退避（証跡）。"""
    src = os.path.join(TEXT_DIR, f"{law_id}.json")
    if not os.path.exists(src):
        return
    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    dst = os.path.join(HISTORY_DIR, f"{law_id}_{stamp}.json")
    with open(src, encoding="utf-8") as f:
        data = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(data)


def append_change_log(law_id, name, change):
    rec = {
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "law_id": law_id, "name": name, "layer": 1,
        "field": f"第{change['article']}条", "status": change["status"],
        "old_excerpt": (change.get("old") or "")[:60],
        "new_excerpt": (change.get("new") or "")[:60],
        "severity": "高",
        "source_url": f"https://laws.e-gov.go.jp/law/{law_id}",
    }
    with open(CHANGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def write_diff_report(all_diffs):
    if not all_diffs:
        return None
    path = f"diff_report_{datetime.now():%Y%m%d}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"法令条文 差分レポート  {datetime.now().isoformat(timespec='seconds')}\n")
        f.write("出典: e-Gov法令API（デジタル庁）を使用\n")
        f.write("=" * 60 + "\n\n")
        for name, law_id, changes in all_diffs:
            f.write(f"■ {name}（{law_id}） 変更{len(changes)}件\n")
            for c in changes:
                f.write(f"  ・第{c['article']}条 [{c['status']}]\n")
                if c.get("old"):
                    f.write(f"      旧: {c['old'][:80]}\n")
                if c.get("new"):
                    f.write(f"      新: {c['new'][:80]}\n")
            f.write("\n")
    return path


def run(dry_run=False):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        laws = json.load(f)["laws"]
    print(f"条文同期 開始: {len(laws)}件  {datetime.now().isoformat(timespec='seconds')}")
    all_diffs = []
    index_rows = []
    failures = []          # 取得できなかった法令（通信断の握りつぶし防止）
    for i, law in enumerate(laws, 1):
        lid, name = law["law_id"], law["name"]
        if not law.get("text_sync", True):
            print(f"  [{i}/{len(laws)}] {name}: 条文保管の対象外（改正法/未施行・/law_data無し）- スキップ")
            index_rows.append({"name": name, "law_id": lid, "count": "-", "status": "対象外(改正法)"})
            continue
        try:
            data = fetch_law_data(lid)
        except urllib.error.HTTPError as e:
            print(f"  [{i}/{len(laws)}] {name}: HTTP {e.code}")
            failures.append((name, f"HTTP {e.code}"))
            index_rows.append({"name": name, "law_id": lid, "count": "-", "status": f"取得失敗(HTTP {e.code})"})
            continue
        except urllib.error.URLError as e:
            print(f"  [{i}/{len(laws)}] {name}: 接続エラー {e.reason}")
            failures.append((name, f"接続エラー {e.reason}"))
            index_rows.append({"name": name, "law_id": lid, "count": "-", "status": "取得失敗(接続エラー)"})
            continue

        art_map = extract_articles(get_law_full_text(data))
        if not art_map:
            print(f"  [{i}/{len(laws)}] {name}: 条文抽出0件（構造要確認）")
            failures.append((name, "条文抽出0件"))
            index_rows.append({"name": name, "law_id": lid, "count": 0, "status": "取得失敗(抽出0件)"})
            continue

        prev = load_prev_map(lid)
        if prev is None:
            save_current(lid, name, art_map)
            index_rows.append({"name": name, "law_id": lid, "count": len(art_map), "status": "初回保存"})
            print(f"  [{i}/{len(laws)}] {name}: 初回保存 {len(art_map)}条")
        else:
            changes = diff_articles(prev, art_map)
            if changes:
                archive_prev(lid)
                save_current(lid, name, art_map)
                for c in changes:
                    append_change_log(lid, name, c)
                all_diffs.append((name, lid, changes))
                index_rows.append({"name": name, "law_id": lid, "count": len(art_map), "status": f"変更{len(changes)}条"})
                print(f"  [{i}/{len(laws)}] {name}: 変更{len(changes)}条 → 更新・記録")
            else:
                index_rows.append({"name": name, "law_id": lid, "count": len(art_map), "status": "変更なし"})
                print(f"  [{i}/{len(laws)}] {name}: 変更なし（{len(art_map)}条）")
        time.sleep(THROTTLE_SEC)

    idx_path = write_index(index_rows)
    if idx_path:
        print(f"対応表: {idx_path}")
    report = write_diff_report(all_diffs)
    total = sum(len(c) for _, _, c in all_diffs)
    targets = sum(1 for l in laws if l.get("text_sync", True))
    ok_count = targets - len(failures)
    print(f"条文同期 完了: 変更 {total}条 / {len(all_diffs)}法令"
          f"（取得成功 {ok_count}/{targets}件）")
    if report:
        print(f"差分レポート: {report}")

    if failures:
        print("")
        print("*" * 60)
        print(f"*  警告: {len(failures)}件の法令を取得できませんでした。")
        print("*  この結果は『改正なし』ではありません。未確認です。")
        print("*" * 60)
        for nm, why in failures:
            print(f"   × {nm}: {why}")
        if len(failures) == targets:
            print("")
            print("   → 全件失敗しています。ネットワーク／プロキシ設定を確認してください。")
        print("")
    return len(failures)


def selftest():
    print("=" * 60)
    print(" SELFTEST: 条文パース＋差分エンジン実証（ネットワーク不要）")
    print("=" * 60)
    passed = total = 0

    def check(label, cond):
        nonlocal passed, total
        total += 1
        ok = bool(cond); passed += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        return ok

    mock = {"law_full_text": {"tag": "Law", "children": [
        {"tag": "LawNum", "children": ["平成二十八年法律第八十九号"]},
        {"tag": "LawBody", "children": [
            {"tag": "LawTitle", "children": ["技能実習法"]},
            {"tag": "MainProvision", "children": [
                {"tag": "Chapter", "children": [
                    {"tag": "ChapterTitle", "children": ["第一章 総則"]},
                    {"tag": "Article", "attr": {"Num": "1"}, "children": [
                        {"tag": "ArticleCaption", "children": ["（目的）"]},
                        {"tag": "ArticleTitle", "children": ["第一条"]},
                        {"tag": "Paragraph", "children": [
                            {"tag": "ParagraphSentence", "children": [
                                {"tag": "Sentence", "children": ["この法律は、技能実習に関し定める。"]}]}]}]},
                    {"tag": "Article", "attr": {"Num": "2"}, "children": [
                        {"tag": "ArticleTitle", "children": ["第二条"]},
                        {"tag": "Paragraph", "children": [
                            {"tag": "Sentence", "children": ["定義を定める。"]}]}]},
                ]},
                {"tag": "Chapter", "children": [
                    {"tag": "ChapterTitle", "children": ["第二章"]},
                    {"tag": "Article", "attr": {"Num": "3"}, "children": [
                        {"tag": "ArticleTitle", "children": ["第三条"]},
                        {"tag": "Sentence", "children": ["基本理念を定める。"]}]}]},
            ]}]}]}}

    arts = extract_articles(get_law_full_text(mock))
    check("A: 章を跨いで全3条を抽出", list(arts.keys()) == ["1", "2", "3"])
    check("A: 第1条の本文を取得", "技能実習に関し定める" in arts["1"])
    check("A: 見出し(目的)も含む", "目的" in arts["1"])

    old = OrderedDict(arts)
    new = OrderedDict(arts)
    new["2"] = "第二条 定義を改正して定める。"
    ch = diff_articles(old, new)
    check("B: 変更された第2条のみ検知", len(ch) == 1 and ch[0]["article"] == "2")
    check("B: ステータスが『変更』", ch[0]["status"] == "変更")

    new2 = OrderedDict(arts)
    new2["4"] = "第四条 追加された条。"
    del new2["3"]
    ch2 = {c["article"]: c["status"] for c in diff_articles(arts, new2)}
    check("C: 追加(第4条)を検知", ch2.get("4") == "追加")
    check("C: 削除(第3条)を検知", ch2.get("3") == "削除")

    check("D: 無変化は0件", len(diff_articles(arts, OrderedDict(arts))) == 0)

    keys = sorted(["10", "2", "2_2", "1"], key=article_sort_key)
    check("E: 自然順ソート(1,2,2_2,10)", keys == ["1", "2", "2_2", "10"])

    check("F: 深い入れ子の本文連結", "基本理念を定める" in arts["3"])

    # G: 本則と附則の条番号衝突（本則が附則に上書きされないこと）
    def _a(n, t):
        return {"tag": "Article", "attr": {"Num": n}, "children": [
            {"tag": "Paragraph", "children": [{"tag": "ParagraphSentence",
             "children": [{"tag": "Sentence", "children": [t]}]}]}]}
    collide = {"tag": "Law", "children": [{"tag": "LawBody", "children": [
        {"tag": "MainProvision", "children": [_a("1", "本則第一条。"), _a("2", "本則第二条。")]},
        {"tag": "SupplProvision", "children": [_a("1", "附則その一の第一条。")]},
        {"tag": "SupplProvision", "children": [_a("1", "附則その二の第一条。")]},
    ]}]}
    cm = extract_articles(collide)
    check("G: 本則が附則に上書きされない", "本則第一条" in cm.get("1", ""))
    check("G: 附則が別鍵で保持される", len(cm) == 4)
    check("G: 複数の附則が区別される",
          "その一" in cm.get("附1_1", "") and "その二" in cm.get("附2_1", ""))
    ck = sorted(cm, key=article_sort_key)
    check("G: 本則が先、附則が後", ck == ["1", "2", "附1_1", "附2_1"])

    print("-" * 60)
    print(f" 結果: {passed}/{total} PASS")
    print("=" * 60)
    return passed == total


def inspect(law_id="428AC0000000089"):
    """実機で /law_data の実レスポンス構造を確認する。
    技能実習法(確認済ID)を1件取得し、最上位キー・条数・抜粋を表示し生JSONを保存。"""
    print("=" * 60)
    print(f" INSPECT: /law_data/{law_id} の実構造を確認")
    print("=" * 60)
    try:
        data = fetch_law_data(law_id)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} {e.reason}")
        print("  403なら通信制限。ローカル/事業所PCで実行してください。")
        return False
    except urllib.error.URLError as e:
        print(f"  接続エラー: {e.reason}")
        return False
    print(f"  最上位キー: {list(data.keys())}")
    node = get_law_full_text(data)
    print(f"  本文ノードの型: {type(node).__name__}")
    if isinstance(node, dict):
        print(f"  本文ノードのキー: {list(node.keys())[:8]}")
        print(f"  本文ノードのtag: {node.get('tag')}")
    arts = extract_articles(node)
    print(f"  抽出できた条数: {len(arts)}")
    if arts:
        first = next(iter(arts))
        print(f"  例) 第{first}条: {arts[first][:60]}...")
        print("  → パーサは実構造に適合（条文抽出OK）")
    else:
        print("  → 条が0件。実構造のキー名が想定と異なります。生JSONを共有してください。")
    out = f"sample_law_data_{law_id}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"  生データ保存: {out}")
    print("=" * 60)
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    if "--inspect" in sys.argv:
        _args = [a for a in sys.argv[2:] if not a.startswith("--")]
        sys.exit(0 if inspect(*(_args[:1] or [])) else 1)
    _failed = run(dry_run="--dry-run" in sys.argv)
    sys.exit(3 if _failed else 0)
