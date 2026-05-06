# 10_fetch.py — 全ソース収集（キーワードフィルタなし、AI判定に全て委ねる）
# 出力: data/raw_items.jsonl (1行1記事のJSON)

import os
import json
import html
import re
import socket
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path

import feedparser
import yaml

ROOT = Path(os.getenv("IWATE_ROOT", ".")).resolve()
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

socket.setdefaulttimeout(15)

MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "90"))


def clean_html(s: str) -> str:
    s = html.unescape(s or "")
    return re.sub(r"<[^>]+>", "", s).strip()


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def to_iso(dt) -> str:
    if dt:
        return datetime(*dt[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url: str) -> str:
    """URL正規化: トラッキングパラメータ除去・末尾スラッシュ統一"""
    if not url:
        return ""
    p = urlparse(url)
    drop_params = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "fbclid", "gclid", "mc_cid", "mc_eid"}
    query_pairs = []
    if p.query:
        for kv in p.query.split("&"):
            if "=" in kv:
                k = kv.split("=", 1)[0]
                if k not in drop_params:
                    query_pairs.append(kv)
            else:
                query_pairs.append(kv)
    query = "&".join(query_pairs)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc}{path}" + (f"?{query}" if query else "")


def load_sources() -> list[dict]:
    path = CONFIG_DIR / "sources.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def fetch_one(src: dict, age_cutoff: datetime) -> list[dict]:
    url = src["url"]
    name = src.get("name", host_of(url))
    items = []
    try:
        d = feedparser.parse(url)
    except Exception as e:
        print(f"[error] {name} → {e}")
        return items

    n_entries = len(d.entries)
    n_old = 0

    for e in d.entries:
        title = unicodedata.normalize("NFKC", (e.get("title") or "").strip())
        link = canonical_url(e.get("link") or "")
        if not title or not link:
            continue

        body = ""
        if e.get("content") and isinstance(e["content"], list) and e["content"]:
            body = clean_html(e["content"][0].get("value") or "")
        if not body:
            body = clean_html(e.get("summary") or e.get("description") or "")
        body = unicodedata.normalize("NFKC", body)[:2000]

        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = to_iso(e.get(key))
                break
        if not published:
            published = to_iso(None)

        try:
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if pub_dt < age_cutoff:
                n_old += 1
                continue
        except Exception:
            pass

        items.append({
            "title": title,
            "url": link,
            "source": name,
            "source_host": host_of(link),
            "published": published,
            "body": body,
        })

    suffix = f" (skipped {n_old} older than {MAX_AGE_DAYS}d)" if n_old else ""
    print(f"[fetch] {name}: {n_entries} entries, kept {len(items)}{suffix}")
    return items


def load_existing_archive_urls() -> set[str]:
    """既にアーカイブ・キャッシュ済みのURLを読み込み、fetch段階でスキップ"""
    seen = set()
    for path in [DATA_DIR / "archive.jsonl"]:
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        seen.add(json.loads(line)["url"])
                    except Exception:
                        pass
    cache_path = DATA_DIR / "cache" / "judged_cache.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            seen.update(cache.keys())
        except Exception:
            pass
    return seen


def main():
    from datetime import timedelta
    age_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    sources = load_sources()
    already_seen = load_existing_archive_urls()
    print(f"[start] {len(sources)} sources, {len(already_seen)} URLs already in archive/cache, MAX_AGE_DAYS={MAX_AGE_DAYS}")

    all_items = []
    seen_urls = set()
    n_skip_archived = 0
    for src in sources:
        items = fetch_one(src, age_cutoff)
        for it in items:
            url = it["url"]
            if url in already_seen:
                n_skip_archived += 1
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            all_items.append(it)

    out = DATA_DIR / "raw_items.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for it in all_items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(f"[done] {len(all_items)} new items (skipped {n_skip_archived} already archived) → {out}")


if __name__ == "__main__":
    main()
