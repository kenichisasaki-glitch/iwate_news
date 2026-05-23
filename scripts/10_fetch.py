# 10_fetch.py — 全ソース収集（キーワードフィルタなし、AI判定に全て委ねる）
# 出力: data/raw_items.jsonl (1行1記事のJSON)

import os
import json
import html
import re
import socket
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

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


def fetch_rss(src: dict, age_cutoff: datetime) -> list[dict]:
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


def fetch_nsls_json(src: dict, age_cutoff: datetime) -> list[dict]:
    """陸前高田市・野田村のNSLS系CMS用 (index.update.json直接取得)"""
    url = src["url"]
    name = src.get("name", host_of(url))
    items = []
    try:
        r = requests.get(url, timeout=15, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[error] {name} → {e}")
        return items

    entries = data if isinstance(data, list) else data.get("items") or data.get("pages") or []
    n_old = 0

    for e in entries:
        if e.get("is_category_index") or e.get("is_keitai_page"):
            continue
        title = unicodedata.normalize("NFKC", (e.get("page_name") or e.get("title") or "").strip())
        link = canonical_url(e.get("url") or "")
        if not title or not link:
            continue

        published_raw = e.get("publish_datetime") or e.get("published")
        try:
            pub_dt = datetime.fromisoformat(published_raw)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            published = pub_dt.isoformat()
        except Exception:
            published = to_iso(None)
            pub_dt = datetime.now(timezone.utc)

        if pub_dt < age_cutoff:
            n_old += 1
            continue

        items.append({
            "title": title,
            "url": link,
            "source": name,
            "source_host": host_of(link),
            "published": published,
            "body": title,  # JSONフィードに本文無し→タイトルを兼用
        })

    suffix = f" (skipped {n_old} older than {MAX_AGE_DAYS}d)" if n_old else ""
    print(f"[fetch] {name} (json): {len(entries)} entries, kept {len(items)}{suffix}")
    return items


def parse_jp_date(s: str) -> datetime | None:
    s = unicodedata.normalize("NFKC", s).strip()
    s = re.sub(r"\s+", "", s)
    for pat, fmt in [
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日", "ymd"),
        (r"(\d{4})\.(\d{1,2})\.(\d{1,2})", "ymd"),
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", "ymd"),
        (r"(\d{4})/(\d{1,2})/(\d{1,2})", "ymd"),
    ]:
        m = re.search(pat, s)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            try:
                return datetime(y, mo, d, tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def fetch_html_scrape(src: dict, age_cutoff: datetime) -> list[dict]:
    """汎用HTMLスクレイプ。sourceに mode='shiwa'/'karumai' を指定して個別処理に分岐"""
    url = src["url"]
    name = src.get("name", host_of(url))
    mode = src.get("mode", "")
    items = []
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"[error] {name} → {e}")
        return items

    raw_entries: list[tuple[str, str, datetime | None]] = []  # (title, link, pub_dt)

    if mode == "shiwa":
        # 紫波町: div.s-info-list ul > li > a (内側に div.title p, div.date)
        for li in soup.select("div.s-info-list ul > li"):
            a = li.find("a")
            if not a:
                continue
            href = a.get("href") or ""
            title_el = a.select_one(".title p") or a.find("p")
            date_el = a.select_one(".date")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            date_text = date_el.get_text(strip=True) if date_el else ""
            date_text = date_text.replace("NEW!", "").strip()
            pub_dt = parse_jp_date(date_text)
            link = urljoin(url, href)
            raw_entries.append((title, link, pub_dt))

    elif mode == "karumai":
        # 軽米町: ul.news-list 配下に div.bg_h3 と div.ctg_detail_list が交互
        h3_blocks = soup.select("ul.news-list > div.bg_h3")
        date_blocks = soup.select("ul.news-list > div.ctg_detail_list")
        for h3, dt in zip(h3_blocks, date_blocks):
            a = h3.select_one("h3 a") or h3.find("a")
            if not a:
                continue
            href = a.get("href") or ""
            title = a.get_text(strip=True)
            date_el = dt.select_one(".ctg_detail_list_date") or dt
            date_text = date_el.get_text(strip=True)
            pub_dt = parse_jp_date(date_text)
            link = urljoin(url, href)
            raw_entries.append((title, link, pub_dt))

    else:
        print(f"[error] {name}: unknown mode '{mode}'")
        return items

    n_old = 0
    n_total = len(raw_entries)
    for title, link, pub_dt in raw_entries:
        title = unicodedata.normalize("NFKC", title).strip()
        link = canonical_url(link)
        if not title or not link:
            continue
        if pub_dt is None:
            pub_dt = datetime.now(timezone.utc)
        if pub_dt < age_cutoff:
            n_old += 1
            continue

        items.append({
            "title": title,
            "url": link,
            "source": name,
            "source_host": host_of(link),
            "published": pub_dt.isoformat(),
            "body": title,
        })

    suffix = f" (skipped {n_old} older than {MAX_AGE_DAYS}d)" if n_old else ""
    print(f"[fetch] {name} (scrape): {n_total} entries, kept {len(items)}{suffix}")
    return items


def fetch_one(src: dict, age_cutoff: datetime) -> list[dict]:
    t = src.get("type", "rss")
    if t == "rss":
        return fetch_rss(src, age_cutoff)
    if t == "nsls_json":
        return fetch_nsls_json(src, age_cutoff)
    if t == "html_scrape":
        return fetch_html_scrape(src, age_cutoff)
    print(f"[warn] unknown source type '{t}' for {src.get('name')}")
    return []


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
    n_skip_excluded = 0
    for src in sources:
        items = fetch_one(src, age_cutoff)
        for it in items:
            url = it["url"]
            if url in already_seen:
                n_skip_archived += 1
                continue
            if url in seen_urls:
                continue
            # 楽待 (rakumachi.jp) は不動産投資物件の売買広告のため除外
            title_lower = it["title"].lower()
            if "rakumachi" in title_lower or "楽待" in it["title"]:
                n_skip_excluded += 1
                continue
            seen_urls.add(url)
            all_items.append(it)

    out = DATA_DIR / "raw_items.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for it in all_items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(f"[done] {len(all_items)} new items (skipped {n_skip_archived} already archived, {n_skip_excluded} excluded) → {out}")


if __name__ == "__main__":
    main()
