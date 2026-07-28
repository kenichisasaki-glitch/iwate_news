# 30_dedup.py — アーカイブ内の重複削除（AIによる一括グルーピング方式）
# 入力: data/archive.jsonl  (20_judgeが追記済み)
# 出力: data/archive.jsonl  (重複削除＋日付降順ソート)
#
# 旧方式（タイトルbigram類似度+日付窓の事前フィルタ）は、各社で言い回しが違う
# 同一事象の記事（例: キオクシア第2製造棟報道）を取りこぼしたため廃止。
# 直近RECENT_ARCHIVE_DAYS日分の記事をまとめてAIに渡し、重複グループを判定させる。

import os
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(os.getenv("IWATE_ROOT", ".")).resolve()
DATA_DIR = ROOT / "data"

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
RECENT_ARCHIVE_DAYS = int(os.getenv("DEDUP_WINDOW_DAYS", "14"))
RECENT_ADDED_DAYS = 3   # 「最近アーカイブに追加された」とみなす日数（_added_at基準）
CONTEXT_DAYS = 2        # 最近追加分の公開日±この日数の記事を比較対象に含める
MAX_POOL = 120          # 1回のAI呼び出しに渡す記事数の上限
POOL_OVERLAP = 10       # チャンク間の重なり（境界をまたぐ重複の取りこぼし対策）
MAX_RETRIES = 3
RETRY_BASE_DELAY = 3.0


def parse_date(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def load_archive() -> list[dict]:
    path = DATA_DIR / "archive.jsonl"
    if not path.exists():
        return []
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    return items


def save_archive(items: list[dict]):
    path = DATA_DIR / "archive.jsonl"
    items.sort(key=lambda x: x.get("published", ""), reverse=True)
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "indices": {"type": "array", "items": {"type": "integer"}},
                    "keep_indices": {"type": "array", "items": {"type": "integer"}},
                    "is_duplicate": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["indices", "keep_indices", "is_duplicate", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["groups"],
    "additionalProperties": False,
}


DEDUP_SYSTEM = """あなたは岩手県不動産ニュース集の編集者です。
入力された記事一覧から「同じ事象を報じた重複記事」のグループを見つけてください。

ルール:
- 同じ事象 = 同じ施設・同じイベント・同じ発表を別ソース（または同一ソース）が報じている
  - タイトルの言い回しが各社で大きく違っても、報じている事象が同じなら重複
  - 同じ事象の報道が数日にまたがることもある（発表日と続報日など）
- 別事象 = タイトルが似ていても内容が違う場合（例: 同じ店の別店舗、別の決算発表回）
- 続報・解説記事など、同じ事象でも独自の追加情報が主体の記事は別扱いにしてよい
- 重複と判断したら、残す記事(keep)を選ぶ:
  1. 情報量が多く内容が詳しい記事を最優先（本文が充実、具体的な数字・詳細がある）
  2. 同等なら一次情報・公式に近いソース優先（自治体公式 > 新聞・テレビ > 転載・まとめ）
  3. それも同等なら新しい記事優先
- 残すのは基本1つ。複数の記事がそれぞれ独自の重要な情報を持つ場合
  （例: 一方は設備詳細、他方は投資計画に詳しい）に限り複数残してよいが、
  同一事象につき最大でも2〜3本まで。切り口が少し違う程度なら1本に集約する
- 重複かどうか迷う場合は is_duplicate=false にして全部残す（取りこぼし回避を最優先）

出力: groups配列。重複の疑いがある2件以上のグループだけを出力してください（単独記事は出力不要）。
各groupは
  - indices: そのグループに属する記事のindex（入力の[番号]、0始まり）
  - keep_indices: indices内で残す記事のindex（1つ以上）
  - is_duplicate: 重複と確信できるか（trueなら keep_indices以外を削除する）
  - reason: 簡潔な判断理由
"""


def ai_group_duplicates(client: anthropic.Anthropic, pool: list[dict]) -> tuple[set[str], list[str]]:
    """pool内の重複グループをAIに判定させ、削除すべきURL集合と判定ログを返す"""
    lines = []
    for i, it in enumerate(pool):
        lines.append(
            f"[{i}] {it.get('published', '')[:10]} | {it.get('source', '?')}\n"
            f"    タイトル: {it.get('title', '')}\n"
            f"    本文冒頭: {it.get('body', '')[:100]}"
        )
    user_text = "以下の記事一覧から重複グループを判定してください。\n\n" + "\n\n".join(lines)

    result = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=DEDUP_SYSTEM,
                messages=[{"role": "user", "content": user_text}],
                output_config={"format": {"type": "json_schema", "schema": DEDUP_SCHEMA}},
                timeout=180.0,
            )
            text = next((b.text for b in resp.content if b.type == "text"), "")
            result = json.loads(text)
            break
        except anthropic.BadRequestError as e:
            print(f"  [fatal] schema/request error (no retry): {e}", flush=True)
            return set(), [f"AI失敗で全保持({len(pool)}件)"]
        except Exception as e:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print(f"  [retry {attempt+1}/{MAX_RETRIES}] {type(e).__name__}: {e} → wait {delay:.1f}s", flush=True)
            time.sleep(delay)

    if result is None:
        print(f"  [warn] dedup failed after retries → keep all", flush=True)
        return set(), [f"AI失敗で全保持({len(pool)}件)"]

    drop_urls = set()
    info = []
    for g in result.get("groups", []):
        idxs = [i for i in g.get("indices", []) if 0 <= i < len(pool)]
        keeps = [i for i in g.get("keep_indices", []) if i in idxs]
        is_dup = g.get("is_duplicate", False)
        if not is_dup or not keeps or len(idxs) < 2:
            continue
        drops = [i for i in idxs if i not in keeps]
        if not drops:
            continue
        for i in drops:
            drop_urls.add(pool[i]["url"])
        keep_titles = " / ".join(pool[i]["title"][:30] for i in keeps)
        info.append(
            f"残={keep_titles}, drop {len(drops)}件: {g.get('reason', '')[:80]}"
        )
    return drop_urls, info


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=0)
    (DATA_DIR / "dropped.jsonl").touch(exist_ok=True)  # workflowのgit add対策
    archive = load_archive()
    if not archive:
        print("[done] archive empty", flush=True)
        return

    print(f"[start] archive size: {len(archive)}, model={MODEL}", flush=True)

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.date().isoformat()
    recent_cutoff = now_utc - timedelta(days=RECENT_ARCHIVE_DAYS)

    has_new_today = any(it.get("_added_at", "").startswith(today_str) for it in archive)
    force = os.getenv("FORCE_DEDUP", "") == "1"
    if not has_new_today and not force:
        print("[done] no items added today, skip dedup (set FORCE_DEDUP=1 to run anyway)", flush=True)
        return

    # プール選定: 公開日が直近RECENT_ARCHIVE_DAYS日 に加えて、
    # (a) 最近アーカイブに追加された記事（公開日が古い配信転載の再流入を含む）
    # (b) その公開日±CONTEXT_DAYS日の記事（比較相手となる既存記事）
    # を含める。公開日だけで絞ると、遅れて流入した同一事象の記事が
    # 一度もAIに見せられないまま素通りするため。
    added_cutoff = now_utc - timedelta(days=RECENT_ADDED_DAYS)
    pool_idx = set()
    recent_added_pubs = []
    for i, it in enumerate(archive):
        if parse_date(it.get("published", "")) >= recent_cutoff:
            pool_idx.add(i)
        if parse_date(it.get("_added_at", "1970-01-01T00:00:00+00:00")) >= added_cutoff:
            pool_idx.add(i)
            recent_added_pubs.append(parse_date(it.get("published", "")))
    for i, it in enumerate(archive):
        if i in pool_idx:
            continue
        p = parse_date(it.get("published", ""))
        if any(abs((p - np).days) <= CONTEXT_DAYS for np in recent_added_pubs):
            pool_idx.add(i)

    pool = [archive[i] for i in pool_idx]
    pool.sort(key=lambda x: x.get("published", ""), reverse=True)

    if len(pool) < 2:
        print("[done] pool too small, nothing to dedup", flush=True)
        return

    print(f"  [pool] {len(pool)} items (last {RECENT_ARCHIVE_DAYS}d) → AI grouping", flush=True)

    # MAX_POOL件ずつ時系列チャンクで処理（重複は日付近傍に固まるため、
    # POOL_OVERLAP件の重なりを持たせて境界をまたぐ事象を拾う）
    drop_urls: set[str] = set()
    start = 0
    chunk_no = 0
    while start < len(pool):
        chunk = pool[start:start + MAX_POOL]
        chunk_no += 1
        if len(pool) > MAX_POOL:
            print(f"  [chunk {chunk_no}] {len(chunk)} items ({chunk[-1].get('published','')[:10]}〜{chunk[0].get('published','')[:10]})", flush=True)
        d, info = ai_group_duplicates(client, chunk)
        drop_urls |= d
        for line in info:
            print(f"      {line}", flush=True)
        if start + MAX_POOL >= len(pool):
            break
        start += MAX_POOL - POOL_OVERLAP

    if drop_urls:
        kept = [it for it in archive if it["url"] not in drop_urls]
        save_archive(kept)
        # 削除した記事を記録し、10_fetchが同一見出しの再流入を弾けるようにする
        with open(DATA_DIR / "dropped.jsonl", "a", encoding="utf-8") as f:
            for it in archive:
                if it["url"] in drop_urls:
                    f.write(json.dumps({
                        "url": it["url"],
                        "title": it.get("title", ""),
                        "published": it.get("published", ""),
                        "dropped_at": now_utc.isoformat(),
                    }, ensure_ascii=False) + "\n")
        print(f"[done] dropped {len(drop_urls)} duplicates, archive: {len(archive)} → {len(kept)}", flush=True)
    else:
        save_archive(archive)
        print(f"[done] no duplicates dropped, archive size: {len(archive)} (sorted)", flush=True)


if __name__ == "__main__":
    main()
