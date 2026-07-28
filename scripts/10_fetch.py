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


# 見出し末尾の媒体名（「- 四国新聞」「(時事通信)」等）にマッチするパターン
_MEDIA_SEG = (
    r"[^-|()]{0,30}?(新聞|ニュース|放送|テレビ|通信|経済|NEWS|DIGITAL|デジタル|"
    r"オンライン|タイムス|日報|日日|ファイナンス|dメニュー|47NEWS|めんこい|"
    r"PR TIMES|PRTIMES|ドットコム|クロステック|Web)[^-|()]{0,15}"
)


def title_key(title: str) -> str:
    """媒体名サフィックスを除去し記号を潰した、記事同定用のキー。
    共同通信・時事通信の配信記事が地方紙サイト経由で別URL・同一見出しのまま
    何日も流入し続けるため、URLではなく見出しで同一記事を判定する"""
    s = unicodedata.normalize("NFKC", title or "").strip()
    for _ in range(3):
        s2 = re.sub(rf"\s*[-|/]\s*{_MEDIA_SEG}\s*$", "", s)
        s2 = re.sub(rf"\s*\({_MEDIA_SEG}\)\s*$", "", s2)
        if s2 == s:
            break
        s = s2
    return re.sub(r"[\s\[\]【】「」『』()|・,，、。.\-_/:：=!?！?？]+", "", s).lower()


def load_known() -> tuple[set[str], set[str]]:
    """既知のURL集合と見出しキー集合を返す。
    アーカイブ + 判定キャッシュ + 重複削除済み記録(dropped.jsonl)を対象とし、
    一度削除した記事が別URLで再流入しても fetch 段階で弾けるようにする"""
    seen_urls = set()
    seen_keys = set()
    for path in [DATA_DIR / "archive.jsonl", DATA_DIR / "dropped.jsonl"]:
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        j = json.loads(line)
                        seen_urls.add(j["url"])
                        k = title_key(j.get("title", ""))
                        if len(k) >= 10:
                            seen_keys.add(k)
                    except Exception:
                        pass
    cache_path = DATA_DIR / "cache" / "judged_cache.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            seen_urls.update(cache.keys())
        except Exception:
            pass
    return seen_urls, seen_keys


def main():
    from datetime import timedelta
    age_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    sources = load_sources()
    already_seen, known_keys = load_known()
    print(f"[start] {len(sources)} sources, {len(already_seen)} URLs / {len(known_keys)} title-keys known, MAX_AGE_DAYS={MAX_AGE_DAYS}")

    all_items = []
    seen_urls = set()
    n_skip_archived = 0
    n_skip_excluded = 0
    n_skip_title = 0
    for src in sources:
        items = fetch_one(src, age_cutoff)
        for it in items:
            url = it["url"]
            if url in already_seen:
                n_skip_archived += 1
                continue
            if url in seen_urls:
                continue
            tkey = title_key(it.get("title", ""))
            if len(tkey) >= 10 and tkey in known_keys:
                n_skip_title += 1
                continue
            # 楽待 (rakumachi.jp) は不動産投資物件の売買広告のため除外
            title_lower = it["title"].lower()
            if "rakumachi" in title_lower or "楽待" in it["title"]:
                n_skip_excluded += 1
                continue
            seen_urls.add(url)
            if len(tkey) >= 10:
                known_keys.add(tkey)
            all_items.append(it)

    out = DATA_DIR / "raw_items.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for it in all_items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(f"[done] {len(all_items)} new items (skipped {n_skip_archived} already archived, {n_skip_title} same-title, {n_skip_excluded} excluded) → {out}")


if __name__ == "__main__":
    main()
