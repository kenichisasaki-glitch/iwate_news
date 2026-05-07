# 20_judge.py — Claude Sonnet 4.6で関連性判定（アーカイブ追記型）
# 入力: data/raw_items.jsonl  (10_fetchで既に新規分のみ)
# 出力:
#   data/cache/judged_cache.json  ← 全URL→判定結果（perfキャッシュ）
#   data/archive.jsonl            ← 関連=trueの記事のみ（永続アーカイブ・コミット対象）
#                                    新規分は今回ぶんだけ追記。dedup後にソート。

import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx

ROOT = Path(os.getenv("IWATE_ROOT", ".")).resolve()
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 2
RETRY_BASE_DELAY = 2.0
API_TIMEOUT = 60.0
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "0"))  # 0 = no limit


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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
            "type": "string",
            "enum": [
                "店舗・商業施設", "工場・物流", "住宅・マンション",
                "地価・統計", "都市計画・再開発", "用地・公売",
                "跡地・転用", "観光・宿泊", "その他", "該当なし"
            ],
        },
        "municipality": {"type": "string"},
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
            t0 = time.time()
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
                timeout=API_TIMEOUT,
            )
            elapsed = time.time() - t0
            text = next((b.text for b in resp.content if b.type == "text"), "")
            result = json.loads(text)
            result["_api_sec"] = round(elapsed, 2)
            return result
        except anthropic.BadRequestError as e:
            log(f"  [fatal] schema/request error (no retry): {e}")
            raise
        except (anthropic.RateLimitError, anthropic.APIStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
            last_err = e
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            log(f"  [retry {attempt+1}/{MAX_RETRIES}] {type(e).__name__}: {e} → wait {delay:.1f}s")
            time.sleep(delay)
        except Exception as e:
            last_err = e
            log(f"  [unexpected] {type(e).__name__}: {e}")
            break

    return {
        "relevant": True,
        "category": "その他",
        "municipality": "不明",
        "confidence": "low",
        "reason": f"AI判定失敗、保守的に通す: {type(last_err).__name__}",
    }


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key, timeout=API_TIMEOUT, max_retries=0)
    criteria = load_criteria()
    cache = load_cache()
    items = load_raw_items()
    now_iso = datetime.now(timezone.utc).isoformat()

    if not items:
        log("[done] no new items to judge")
        return

    if MAX_ITEMS > 0:
        items = items[:MAX_ITEMS]
        log(f"[start] {len(items)} items (capped by MAX_ITEMS={MAX_ITEMS}), cache size: {len(cache)}")
    else:
        log(f"[start] {len(items)} new items to judge, cache size: {len(cache)}")

    log(f"[config] model={MODEL} timeout={API_TIMEOUT}s retries={MAX_RETRIES} criteria_chars={len(criteria)}")

    n_new_judged = 0
    n_relevant_new = 0
    n_consec_failures = 0
    new_relevant_items = []
    t_start = time.time()

    for i, item in enumerate(items, 1):
        url = item["url"]
        if url in cache:
            judgment = cache[url]
        else:
            log(f"  [{i}/{len(items)}] judging: {item['title'][:60]}")
            judgment = judge_item(client, criteria, item)
            cache[url] = judgment
            n_new_judged += 1
            api_sec = judgment.get("_api_sec", "?")
            rel = "T" if judgment.get("relevant") else "F"
            cat = judgment.get("category", "?")
            log(f"      → relevant={rel} category={cat} ({api_sec}s)")

            if "AI判定失敗" in judgment.get("reason", ""):
                n_consec_failures += 1
                if n_consec_failures >= 5:
                    save_cache(cache)
                    raise SystemExit(f"5 consecutive failures, aborting. Last: {judgment.get('reason')}")
            else:
                n_consec_failures = 0

            if n_new_judged % 25 == 0:
                save_cache(cache)
                avg = (time.time() - t_start) / n_new_judged
                eta_min = avg * (len(items) - i) / 60
                log(f"  [progress] {i}/{len(items)} judged={n_new_judged} relevant={n_relevant_new} avg={avg:.1f}s/item ETA={eta_min:.1f}min")

        if judgment.get("relevant"):
            n_relevant_new += 1
            merged = {**item, "judgment": {k: v for k, v in judgment.items() if k != "_api_sec"}, "_added_at": now_iso}
            new_relevant_items.append(merged)

    save_cache(cache)
    append_to_archive(new_relevant_items)

    log(f"[done] new_judged={n_new_judged} relevant={n_relevant_new} → archive +{len(new_relevant_items)}")


if __name__ == "__main__":
    main()
