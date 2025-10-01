# 04_build_html_simple.py — 超シンプル版（全文貼り替え用）
# ・タイトル＋RSS本文(あれば)のみで判定（スクレイピングなし）
# ・feeds.txt でフィード別に「追加(+) / 上書き(=) / ALL（全件）」対応
# ・ALL でも 3列目の除外語は有効
# ・日本語の表記ゆれに強くするため NFKC 正規化

import os
import re
import html
import time
import socket
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path

import feedparser

# ==== パス・基本設定 ====
ROOT = Path(os.getenv("IWATE_ROOT", ".")).resolve()
CONFIG_DIR = ROOT / "config"
SITE_DIR = ROOT / "site"
SITE_DIR.mkdir(parents=True, exist_ok=True)

SITE_TITLE_TEXT = "岩手県 不動産まとめサイト（毎日7:00自動更新）"  # ← タブ表示用（改行なし）
SITE_TITLE_HTML = "岩手県 不動産まとめサイト<br>（毎日7:00自動更新）"  # ← ページ見出し用
SITE_DESC  = '<a href="https://www.greo-jp.com/" target="_blank">GREO合同会社が運営するまとめサイトです。</a>'
MAX_ITEMS = 1000

socket.setdefaulttimeout(6)  # ネットワーク全体の安全タイムアウト（秒）

# ==== グローバル語（ベース：ユーザー指定）====
GLOBAL_INCLUDE = [
    "不動産","地価","地価調査","公示地価","路線価","固定資産税","地価指数",
    "住宅","空き家","空家","賃貸","分譲","マンション","戸建","団地",
    "用地","用地取得","収用","保留地","造成","宅地","宅地造成","区画","区画整理",
    "都市計画","用途地域","市街化","地区計画","立地適正化","再開発","再整備","再生",
    "PFI","PPP","土地","建物","老朽化","建て替え","建替","物件","着工","閉館",
    "竣工","解体","開業","閉業","開店","閉店","用途変更","売却","譲渡","利活用",
    "店舗","工場","観光","ホテル","経済効果","統計","推移","施設","老人ホーム",
    "建築","閉校","廃校","統廃合","跡地","旅館","温泉","改修","ショッピングセンター",
]

GLOBAL_EXCLUDE = [
    # ノイズになりがちな話題
    "暴風","雷","台風","被害","火災","全焼","半焼","焼け跡","クマ","グマ",
    "猛暑","天候","天気"
]

# ==== デフォルトFEEDS（feeds.txtが空/無いとき）====
DEFAULT_FEEDS = [
    "https://www.pref.iwate.jp/news.rss",
    "https://www.city.morioka.iwate.jp/news.rss",
]

# ==== ユーティリティ ====
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

def norm(s: str) -> str:
    # 全角/半角・濁点などを統一してから小文字化
    s = unicodedata.normalize("NFKC", s or "")
    return s.lower()

def any_hit(text_lc: str, words: list[str]) -> bool:
    if not words:
        return False
    for w in set(words):
        w = norm(w)
        if w and w in text_lc:
            return True
    return False

def none_hit(text_lc: str, words: list[str]) -> bool:
    if not words:
        return True
    for w in set(words):
        w = norm(w)
        if w and w in text_lc:
            return False
    return True

# ==== feeds.txt の読み込み ====
# 1行:  URL | <含める語spec> | <除外語spec>
# <spec>:
#   "= 語 語 ..."  → 上書き（グローバル無視）
#   "+ 語 語 ..."  → 追加（グローバルに足す）
#   "& 語 語 ..."  → 両方（GLOBAL_INCLUDE と当該語の両方にヒットで採用） ← 追加
#   "語 語 ..."    → 追加（接頭辞なしは + と同じ）
#   "ALL" / "*" / "ALL!" → このRSSは全件通す（2列目に書く）。ただし3列目の除外語は有効
def read_feeds_with_rules(path: Path):
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

    def parse_spec(spec: str):
        spec = spec.strip()
        mode = "add"  # add / override / both
        if not spec:
            return mode, []
        head = spec[:1]
        if head == "=":
            mode = "override"; spec = spec[1:].strip()
        elif head in {"+", "-"}:
            mode = "add"; spec = spec[1:].strip()
        elif head == "&":  # ← 最小差分: both モードを追加
            mode = "both"; spec = spec[1:].strip()
        words = [w for w in spec.split() if w]
        return mode, words

    feeds = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        url = parts[0]
        inc_spec = parts[1] if len(parts) >= 2 else ""
        exc_spec = parts[2] if len(parts) >= 3 else ""

        inc_mode, inc_words = parse_spec(inc_spec)
        exc_mode, exc_words = parse_spec(exc_spec)

        inc_upper = inc_spec.strip().upper()
        pass_all = inc_upper in ("ALL", "*", "ALL!")

        # ALLでも exc_words は保持（inc は無視）
        feeds.append({
            "url": url,
            "pass_all": pass_all,
            "inc_mode": "override" if pass_all else inc_mode,
            "inc_words": [] if pass_all else inc_words,
            "exc_mode": exc_mode,
            "exc_words": exc_words,
        })
    return feeds

# ==== アイテム抽出 → HTML ====
def fetch_items(feed_rules: list[dict]):
    items = []
    total_entries = 0

    if not feed_rules:
        feed_rules = [{"url": u, "pass_all": False,
                       "inc_mode":"add","inc_words":[],
                       "exc_mode":"add","exc_words":[]} for u in DEFAULT_FEEDS]
        print(f"[info] feeds.txt が無い/空 → デフォルト{len(feed_rules)}本で実行")

    for fr in feed_rules:
        url = fr["url"]
        pass_all = fr.get("pass_all", False)

        # inc/exc 決定。ALL時は inc を使わず、exc は活かす
        if pass_all:
            inc = []
            exc = fr["exc_words"] if fr["exc_mode"] == "override" else (GLOBAL_EXCLUDE + fr["exc_words"])
        else:
            inc_mode = fr.get("inc_mode", "add")
            if inc_mode == "override":
                inc = fr["inc_words"]
            elif inc_mode == "both":
                # 両方ヒットは accept 判定で処理するので、ここではフィード固有語のみを保持
                inc = fr["inc_words"]
            else:
                inc = GLOBAL_INCLUDE + fr["inc_words"]
            exc = fr["exc_words"] if fr["exc_mode"] == "override" else (GLOBAL_EXCLUDE + fr["exc_words"])

        print(f"[fetch] {url} {'(ALL)' if pass_all else ''}")
        start = time.time()
        try:
            d = feedparser.parse(url)
        except Exception as e:
            print(f"[error] {url} → {e}")
            continue
        print(f"[ok] {url} entries={len(d.entries)} {time.time()-start:.1f}s")

        for e in d.entries:
            total_entries += 1
            title = (e.get("title") or "").strip()
            link  = e.get("link") or ""
            # RSSの本文/要約（あれば）
            body = ""
            if e.get("content") and isinstance(e["content"], list) and e["content"]:
                body = clean_html(e["content"][0].get("value") or "")
            if not body:
                body = clean_html(e.get("summary") or e.get("description") or "")

            hay = f"{title}\n{body}"
            hay_lc = norm(hay)

            # 受理判定：
            #  - ALL: 除外語だけチェック
            #  - both: GLOBAL_INCLUDE と inc(フィード固有語) の両方にヒット かつ 非除外
            #  - add/override: 従来通り (inc にヒット) かつ 非除外
            if pass_all:
                accept = none_hit(hay_lc, exc)
            else:
                if fr.get("inc_mode") == "both":
                    accept = any_hit(hay_lc, GLOBAL_INCLUDE) and any_hit(hay_lc, inc) and none_hit(hay_lc, exc)
                else:
                    accept = any_hit(hay_lc, inc) and none_hit(hay_lc, exc)

            if not accept:
                continue

            # 日付
            pub = None
            for key in ("published_parsed", "updated_parsed"):
                if e.get(key):
                    pub = to_iso(e.get(key))
                    break
            if not pub:
                pub = to_iso(None)

            print("APPEND:", title, link)

            items.append({
                "title": title,
                "url": link,
                "source": host_of(link),
                "published": pub,
            })

    items.sort(key=lambda x: x["published"], reverse=True)
    print(f"[sum] total_entries={total_entries}, extracted={len(items)}")
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

    # 日付ごとにグルーピング
    groups = {}
    for it in items:
        day = iso_to_ymd_jst(it["published"]) or "日付不明"
        groups.setdefault(day, []).append(it)

    # 最終更新表示用（ローカルタイムゾーンで表示）
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"ja\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        f"<title>{html.escape(SITE_TITLE_TEXT)}</title>",  # タブ用（改行なし）
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<meta name=\"description\" content=\"{html.escape(SITE_DESC)}\">",
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        "<header>",
        f"<h1>{SITE_TITLE_HTML}</h1>",                      # 見出し用（改行ありOK）
        f'<div class="desc">{SITE_DESC}</div>',
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
        f'<footer>最終更新: {now_str}（JST）｜ <a href="https://www.greo-jp.com/" target="_blank">Operated by GREO</a></footer>',
        "</body></html>",
    ]

    # HTML文字列を一度作って、index と archive の両方へ書き出し
    html_text = "\n".join(parts)

    # 最新
    out = SITE_DIR / "index.html"
    out.write_text(html_text, encoding="utf-8")

    # アーカイブ（削除なしで貯める）
    archive_dir = SITE_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_name = datetime.now().strftime("%Y-%m-%d") + ".html"
    (archive_dir / archive_name).write_text(html_text, encoding="utf-8")

    return out

def main():
    feeds_path = CONFIG_DIR / "feeds.txt"
    feed_rules = read_feeds_with_rules(feeds_path)
    items = fetch_items(feed_rules)
    out = build_html(items)
    print(f"生成: {out}（{len(items)}件）")

if __name__ == "__main__":
    main()
