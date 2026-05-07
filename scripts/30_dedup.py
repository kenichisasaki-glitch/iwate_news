# 30_dedup.py — アーカイブ内の重複削除（新規追加分のみ対象に効率化）
# 入力: data/archive.jsonl  (20_judgeが追記済み)
# 出力: data/archive.jsonl  (重複削除＋日付降順ソート)

import os
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(os.getenv("IWATE_ROOT", ".")).resolve()
DATA_DIR = ROOT / "data"

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
SIMILARITY_THRESHOLD = 0.45
DATE_WINDOW_DAYS = 1
RECENT_ARCHIVE_DAYS = 14
MAX_CLUSTER_SIZE = 8


def normalize_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\[\]【】「」『』(()｜\|・,，、。\.\s\-_/]+", " ", s)
    return s.strip()


def title_bigrams(s: str) -> set[str]:
    s = normalize_title(s).replace(" ", "")
    return {s[i:i+2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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
                    "canonical_index": {"type": "integer"},
                    "is_duplicate": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["indices", "canonical_index", "is_duplicate", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["groups"],
    "additionalProperties": False,
}


DEDUP_SYSTEM = """あなたは岩手県不動産ニュース集の編集者です。
入力された記事群から「同じ事象を報じた重複記事」を見つけてグルーピングしてください。

ルール:
- 同じ事象 = 同じ施設・同じイベント・同じ発表を別ソースが報じている
- 別事象 = タイトルが似ていても内容が違う場合（例: 同じ店の別店舗、別の決算発表回）
- 重複と判断したら、代表(canonical)を1つ選ぶ:
  1. 一次情報・公式に近いソース優先（自治体公式 > 新聞 > 転載・まとめ）
  2. 内容が詳しい記事優先
  3. 同等なら新しい記事優先
- 重複か迷う場合は is_duplicate=false にして全部残す（取りこぼし回避）
- 同じ事象でも明らかに別記事（例: 続報、解説記事）は別グループとする

出力: groups配列。各groupは
  - indices: そのグループに属する記事のindex（入力順、0始まり）
  - canonical_index: indices内で代表に選ぶindex
  - is_duplicate: 重複と判定するか（trueなら canonical_index以外を落とす）
  - reason: 簡潔な判断理由
全indicesがいずれかのgroupに属するように出力してください。
"""


def dedup_cluster(client: anthropic.Anthropic, cluster_items: list[dict]) -> tuple[set[str], list[str]]:
    if len(cluster_items) > MAX_CLUSTER_SIZE:
        mid = len(cluster_items) // 2
        d1, l1 = dedup_cluster(client, cluster_items[:mid])
        d2, l2 = dedup_cluster(client, cluster_items[mid:])
        return d1 | d2, l1 + l2

    lines = []
    for i, it in enumerate(cluster_items):
        lines.append(
            f"[{i}] {it['published'][:10]} | {it['source']}\n"
            f"    タイトル: {it['title']}\n"
            f"    本文冒頭: {it.get('body', '')[:200]}"
        )
    user_text = "以下の記事群について重複判定してください。\n\n" + "\n\n".join(lines)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=DEDUP_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
            output_config={"format": {"type": "json_schema", "schema": DEDUP_SCHEMA}},
            timeout=60.0,
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        result = json.loads(text)
    except Exception as e:
        print(f"  [warn] dedup failed: {type(e).__name__}: {e} → keep all in cluster", flush=True)
        return set(), [f"AI失敗で全保持({len(cluster_items)}件)"]

    drop_urls = set()
    info = []
    for g in result.get("groups", []):
        idxs = g.get("indices", [])
        canonical = g.get("canonical_index")
        is_dup = g.get("is_duplicate", False)
        if not is_dup or canonical is None or canonical not in idxs:
            continue
        for i in idxs:
            if i != canonical and 0 <= i < len(cluster_items):
                drop_urls.add(cluster_items[i]["url"])
        info.append(
            f"代表={cluster_items[canonical]['title'][:30]}, "
            f"drop {len([i for i in idxs if i != canonical])}件: {g.get('reason','')[:60]}"
        )
    return drop_urls, info


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=1)
    archive = load_archive()
    if not archive:
        print("[done] archive empty", flush=True)
        return

    print(f"[start] archive size: {len(archive)}", flush=True)

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.date().isoformat()
    recent_cutoff = now_utc - timedelta(days=RECENT_ARCHIVE_DAYS)

    new_indices = []
    candidate_indices = []
    for i, it in enumerate(archive):
        added_at = it.get("_added_at", "")
        if added_at.startswith(today_str):
            new_indices.append(i)
        pub_dt = parse_date(it.get("published", ""))
        if pub_dt >= recent_cutoff:
            candidate_indices.append(i)

    if not new_indices:
        print("[done] no items added today, skip dedup", flush=True)
        return

    new_set = set(new_indices)
    cand_set = set(candidate_indices) | new_set
    cand_list = sorted(cand_set)
    print(f"  new_today={len(new_indices)}, candidate_pool(recent {RECENT_ARCHIVE_DAYS}d)={len(cand_list)}", flush=True)

    bigrams = {i: title_bigrams(archive[i]["title"]) for i in cand_list}
    dates = {i: parse_date(archive[i].get("published", "")) for i in cand_list}

    parent = {i: i for i in cand_list}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i_pos, i in enumerate(cand_list):
        for j in cand_list[i_pos + 1:]:
            if i not in new_set and j not in new_set:
                continue
            if abs((dates[i] - dates[j]).days) > DATE_WINDOW_DAYS:
                continue
            if jaccard(bigrams[i], bigrams[j]) >= SIMILARITY_THRESHOLD:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in cand_list:
        clusters.setdefault(find(i), []).append(i)
    cluster_lists = [c for c in clusters.values() if len(c) >= 2 and any(i in new_set for i in c)]

    print(f"  [clusters] {len(cluster_lists)} clusters with new items", flush=True)

    all_drop_urls: set[str] = set()
    for c_no, cluster in enumerate(cluster_lists, 1):
        cluster_items = [archive[i] for i in cluster]
        drop_urls, info = dedup_cluster(client, cluster_items)
        all_drop_urls |= drop_urls
        print(f"  [cluster {c_no}/{len(cluster_lists)}] size={len(cluster)} drop={len(drop_urls)}", flush=True)
        for line in info:
            print(f"      {line}", flush=True)
        time.sleep(0.3)

    if all_drop_urls:
        kept = [it for it in archive if it["url"] not in all_drop_urls]
        save_archive(kept)
        print(f"[done] dropped {len(all_drop_urls)} duplicates, archive: {len(archive)} → {len(kept)}", flush=True)
    else:
        save_archive(archive)
        print(f"[done] no duplicates dropped, archive size: {len(archive)} (sorted)", flush=True)


if __name__ == "__main__":
    main()
