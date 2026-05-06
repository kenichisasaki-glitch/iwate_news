# 20_judge.py — Claude Sonnet 4.6で関連性判定（アーカイブ追記型）
# 入力: data/raw_items.jsonl  (10_fetchで既に新規分のみ)
# 出力:
#   data/cache/judged_cache.json  ← 全URL→判定結果（perfキャッシュ）
#   data/archive.jsonl            ← 関連=trueの記事のみ（永続アーカイブ・コミット対象）
#                                    新規分は今回ぶんだけ追記。dedup後にソート。

import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(os.getenv("IWATE_ROOT", ".")).resolve()
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


def load_criteria() -> str:
    return (CONFIG_DIR / "judge_criteria.md").read_text(encoding="utf-8")


def load_cache() -> dict:
    path = CACHE_DIR / "judged_cache.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict):
    path = CACHE_DIR / "judged_cache.json"
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def load_raw_items() -> list[dict]:
    items = []
    path = DATA_DIR / "raw_items.jsonl"
    if not path.exists():
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def append_to_archive(items: list[dict]):
    if not items:
        return
    path = DATA_DIR / "archive.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "category": {
            "type": ["string", "null"],
            "enum": [
                "店舗・商業施設", "工場・物流", "住宅・マンション",
                "地価・統計", "都市計画・再開発", "用地・公売",
                "跡地・転用", "観光・宿泊", "その他", None
            ],
        },
        "municipality": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
    "required": ["relevant", "category", "municipality", "confidence", "reason"],
    "additionalProperties": False,
}


def judge_item(client: anthropic.Anthropic, criteria: str, item: dict) -> dict:
    user_text = (
        f"以下の記事を判定基準に従って判定してください。\n\n"
        f"【タイトル】{item['title']}\n"
        f"【ソース】{item['source']}\n"
        f"【公開日】{item['published'][:10]}\n"
        f"【本文】{item['body'][:1500]}"
    )

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=[{
                    "type": "text",
                    "text": criteria,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_text}],
                output_config={
                    "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}
                },
            )
            text = next((b.text for b in resp.content if b.type == "text"), "")
            return json.loads(text)
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            last_err = e
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print(f"  [retry {attempt+1}/{MAX_RETRIES}] {e} → wait {delay:.1f}s")
            time.sleep(delay)
        except Exception as e:
            last_err = e
            break

    return {
        "relevant": True,
        "category": "その他",
        "municipality": None,
        "confidence": "low",
        "reason": f"AI判定失敗、保守的に通す: {last_err}",
    }


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    criteria = load_criteria()
    cache = load_cache()
    items = load_raw_items()
    now_iso = datetime.now(timezone.utc).isoformat()

    if not items:
        print("[done] no new items to judge")
        return

    print(f"[start] {len(items)} new items to judge, cache size: {len(cache)}")

    n_new_judged = 0
    n_relevant_new = 0
    new_relevant_items = []

    for i, item in enumerate(items, 1):
        url = item["url"]
        if url in cache:
            judgment = cache[url]
        else:
            judgment = judge_item(client, criteria, item)
            cache[url] = judgment
            n_new_judged += 1
            if n_new_judged % 25 == 0:
                save_cache(cache)
                print(f"  [progress] {i}/{len(items)} judged: {n_new_judged}")

        if judgment.get("relevant"):
            n_relevant_new += 1
            merged = {**item, "judgment": judgment, "_added_at": now_iso}
            new_relevant_items.append(merged)

    save_cache(cache)
    append_to_archive(new_relevant_items)

    print(f"[done] new_judged={n_new_judged} relevant={n_relevant_new} → archive +{len(new_relevant_items)}")


if __name__ == "__main__":
    main()
