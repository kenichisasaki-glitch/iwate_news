# 03_build_html.py  (feeds.txt + meta description scrape + 強化フィルタ)
import re
import html
import time
import socket
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path

import feedparser
from bs4 import BeautifulSoup

# --- paths ---
ROOT = Path(r"C:\iwate_news")
CONFIG_DIR = ROOT / "config"
SITE_DIR = ROOT / "site"
SITE_DIR.mkdir(parents=True, exist_ok=True)

SITE_TITLE = "岩手県 不動産ニュースまとめ（見出し＋リンク）"
SITE_DESC  = "岩手×不動産・土地・建設・都市計画の新着情報をRSSから自動抽出（要約なし・軽量MVP）"
MAX_ITEMS = 300

# --- timeouts & scraping options ---
socket.setdefaulttimeout(6)  # network default timeout (seconds)
USE_PAGE_SCRAPE = True       # ページ本文は見に行かない。meta description のみ
MAX_SCRAPE_PER_RUN = 10      # 1回の実行で実ページを見に行く上限
SCRAPE_TIMEOUT = 5           # meta取得のタイムアウト（秒）

# --- keywords (調整はここで。全角スペースは入れないこと) ---
# トピック系（不動産・都市計画など）
TOPIC_KEYWORDS = [
    "不動産","地価","地価調査","公示地価","路線価","固定資産税","地価指数",
    "住宅","空き家","空家","賃貸","分譲","マンション","戸建","団地",
    "用地","用地取得","収用","保留地","造成","宅地","宅地造成","区画","区画整理",
    "都市計画","用途地域","市街化","地区計画","立地適正化","再開発","再整備",
    "PFI","PPP","土地","建物","老朽化","建て替え","建替","物件","着工"
    "竣工","解体","開業","閉業","開店","閉店","用途変更","売却","譲渡","利活用",
    "店舗","工場","観光","ホテル","経済効果","統計","推移","土地","建物"
]
# 地名（岩手ローカル）
GEO_KEYWORDS = [
    "岩手","盛岡","花巻","北上","奥州","一関","二戸","久慈","宮古","大船渡","陸前高田","滝沢",
    "遠野","葛巻","岩泉","山田","普代","野田","洋野","住田","八幡平","紫波","矢巾","金ケ崎","金ヶ崎","雫石","大槌","田野畑","軽米","岩手町","九戸","西和賀","平泉"
]
# 除外語（スポーツ・天気・純エンタ等。必要に応じて増減）
NEGATIVE_KEYWORDS = [
    "高校野球","プロ野球","Jリーグ","サッカー","ラグビー","バレーボール","バスケットボール","テニス","ゴルフ",
    "コンサート","ライブ","音楽","舞台","イベント情報",
    "天気","気温","猛暑","寒波","降雪","台風","地震速報"
]

# --- feeds fallback (feeds.txt が無い/空のとき) ---
DEFAULT_FEEDS = [
    "https://www.pref.iwate.jp/news.rss",
    "https://www.city.morioka.iwate.jp/news.rss",
]

def read_feeds_from_txt(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = None
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = path.read_text(encoding="utf-8", errors="ignore")

    urls, seen = [], set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not (s.lower().startswith("http://") or s.lower().startswith("https://")):
            continue
        if s not in seen:
            seen.add(s)
            urls.append(s)
    return urls

def clean_html(s: str) -> str:
    s = html.unescape(s or "")
    return re.sub(r"<[^>]+>", "", s)

def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""

def to_iso(dt) -> str:
    if dt:
        return datetime(*dt[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()

def iso_to_ymd_jst(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""

def count_hits(text: str, words: list[str]) -> int:
    if not text:
        return 0
    return sum(1 for w in words if w in text)

def is_iwate_gov_domain(netloc: str) -> bool:
    # 自治体ドメインや岩手関連ドメインは地名スコア代替として扱う
    nl = (netloc or "").lower()
    return nl.endswith(".iwate.jp") or nl.endswith(".lg.jp") or ("iwate" in nl)

def filter_match(text: str, netloc: str) -> bool:
    # 除外語が含まれるなら即NG
    if any(w in (text or "") for w in NEGATIVE_KEYWORDS):
        return False
    topic = count_hits(text, TOPIC_KEYWORDS)
    geo   = count_hits(text, GEO_KEYWORDS)
    # 受理ルール：
    # 1) トピック1以上 かつ (地名1以上 or 岩手系ドメイン)
    if topic >= 1 and (geo >= 1 or is_iwate_gov_domain(netloc)):
        return True
    # 2) トピックが2語以上ヒット（地名なしでも通す）…拾い漏れ対策
    if topic >= 2:
        return True
    return False

def _fetch_meta_description(url: str, timeout: int = SCRAPE_TIMEOUT) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html_bytes = resp.read()
        soup = BeautifulSoup(html_bytes, "html.parser")
        tag = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        return (tag.get("content") or "").strip() if tag else ""
    except Exception:
        return ""

def fetch_items(feeds: list[str]):
    items = []
    scraped = 0
    total_entries = 0

    for feed_url in feeds:
        print(f"[fetch] {feed_url}")
        start = time.time()
        try:
            d = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[error] {feed_url} -> {e}")
            continue
        took = time.time() - start
        print(f"[ok] {feed_url} entries={len(d.entries)} {took:.1f}s")

        for e in d.entries:
            total_entries += 1
            title = (e.get("title") or "").strip()
            link  = e.get("link") or ""
            netloc = host_of(link)

            # RSS内の本文候補
            body = ""
            if e.get("content") and isinstance(e["content"], list) and e["content"]:
                body = clean_html(e["content"][0].get("value") or "")
            if not body:
                body = clean_html(e.get("summary") or e.get("description") or "")

            # 本文が薄い時だけ meta description を見る（上限つき）
            if USE_PAGE_SCRAPE and not body and link and scraped < MAX_SCRAPE_PER_RUN:
                meta = _fetch_meta_description(link, timeout=SCRAPE_TIMEOUT)
                if meta:
                    body = meta
                    scraped += 1
                    print(f"[scrape] {link} -> meta description captured")

            haystack = f"{title}\n{body}"

            if filter_match(haystack, netloc):
                # 日付
                pub = None
                for key in ("published_parsed", "updated_parsed"):
                    if e.get(key):
                        pub = to_iso(e.get(key))
                        break
                if not pub:
                    pub = to_iso(None)

                items.append({
                    "title": title,
                    "url": link,
                    "source": netloc,
                    "published": pub,
                })

    items.sort(key=lambda x: x["published"], reverse=True)
    print(f"[sum] total_entries={total_entries}, extracted={len(items)}, scraped={scraped}")
    return items[:MAX_ITEMS]

def build_html(items):
    css = """
    body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,"Noto Sans JP",sans-serif;line-height:1.6;margin:20px;}
    header{margin-bottom:16px}
    h1{font-size:1.45rem;margin:0}
    .desc{color:#555;margin:4px 0 12px}
    .date{margin-top:22px;font-weight:700}
    .item{border:1px solid #eee;border-radius:12px;padding:10px 12px;margin:10px 0}
    .item h3{margin:0 0 6px;font-size:1.02rem}
    .meta{font-size:.85rem;color:#666}
    a{color:#0a58ca;text-decoration:none}
    a:hover{text-decoration:underline}
    footer{color:#777;font-size:.85rem;margin-top:24px}
    """
    groups = {}
    for it in items:
        day = iso_to_ymd_jst(it["published"]) or "日付不明"
        groups.setdefault(day, []).append(it)

    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"ja\">",
        "<meta charset=\"utf-8\">",
        f"<title>{html.escape(SITE_TITLE)}</title>",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<meta name=\"description\" content=\"{html.escape(SITE_DESC)}\">",
        f"<style>{css}</style>",
        "<body>",
        "<header>",
        f"<h1>{html.escape(SITE_TITLE)}</h1>",
        f"<div class=\"desc\">{html.escape(SITE_DESC)}</div>",
        "</header>",
    ]
    for day in sorted(groups.keys(), reverse=True):
        parts.append(f"<div class=\"date\">📅 {day}</div>")
        for it in groups[day]:
            title = html.escape(it["title"] or "(無題)")
            url   = html.escape(it["url"] or "#")
            src   = html.escape(it["source"] or "")
            parts.append(
                f"<div class=\"item\">"
                f"<h3><a href=\"{url}\" target=\"_blank\" rel=\"noopener\">{title}</a></h3>"
                f"<div class=\"meta\">出典: {src}</div>"
                f"</div>"
            )
    parts += [
        "<footer>自動生成（見出し＋リンクのみ, feeds.txt 設定 / meta description 参照）/ β版</footer>",
        "</body></html>",
    ]
    out = SITE_DIR / "index.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out

def main():
    feeds = read_feeds_from_txt(CONFIG_DIR / "feeds.txt")
    if not feeds:
        feeds = DEFAULT_FEEDS
        print(f"[info] feeds.txt が見つからない/空のためデフォルトFEEDS({len(feeds)})を使用")
    else:
        print(f"[info] feeds.txt から {len(feeds)} 本のRSSを読み込み")
    items = fetch_items(feeds)
    out = build_html(items)
    print(f"生成: {out}（{len(items)}件）")

if __name__ == "__main__":
    main()
