# 01_filter_rss.py
import re
import html
import feedparser

# --- 収集するRSS（まずは2本。あとで増やします） ---
FEEDS = [
    "https://www.pref.iwate.jp/news.rss",            # 岩手県：新着
    "https://www.city.morioka.iwate.jp/news.rss",    # 盛岡市：新着
]

# --- 不動産・都市計画まわりの抽出キーワード（必要に応じて後で調整） ---
KEYWORDS = [
    # 価格・税
    "不動産","地価","地価調査","公示地価","路線価","固定資産税","地価指数",
    # 住宅・空き家
    "住宅","空き家","空家","賃貸","分譲","マンション","戸建","団地",
    # 用地・整備
    "用地","用地取得","収用","保留地","造成","宅地","宅地造成","区画","区画整理",
    # 計画・規制
    "都市計画","用途地域","市街化","地区計画","立地適正化","再開発","再整備",
    # 入札・公募
    "入札","公募","公示","公告","PFI","PPP",
    # 道路・インフラ（地価・供給に影響）
    "道路","道路拡幅","区画道路","駅前","駅周辺","土地区画整理",
    # 岩手ローカル地名（念のため）
    "岩手","盛岡","花巻","北上","奥州","一関","二戸","久慈","宮古","大船渡","陸前高田","滝沢",
]

def contains_keywords(text: str) -> bool:
    t = text or ""
    return any(k in t for k in KEYWORDS)

def clean_html(s: str) -> str:
    # RSSのsummaryにHTMLが入っている場合があるので簡易除去
    s = html.unescape(s or "")
    return re.sub(r"<[^>]+>", "", s)

def main():
    kept = []
    for feed_url in FEEDS:
        d = feedparser.parse(feed_url)
        for e in d.entries:
            title = (e.get("title") or "").strip()
            link  = e.get("link") or ""
            summ  = clean_html(e.get("summary") or e.get("description") or "")

            haystack = f"{title}\n{summ}"
            if contains_keywords(haystack):
                kept.append((title, link))

    # 結果を表示（まずは確認用）
    print(f"抽出件数: {len(kept)}件\n")
    for title, link in kept[:50]:  # 多すぎると読みにくいので最大50件表示
        print(f"- {title}\n  {link}\n")

if __name__ == "__main__":
    main()
