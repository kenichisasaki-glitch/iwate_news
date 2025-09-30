# 00_test_rss.py
import feedparser

FEED = "https://www.pref.iwate.jp/news.rss"  # 岩手県の新着情報RSS（まずは1本だけ）

d = feedparser.parse(FEED)
for e in d.entries[:20]:  # とりあえず20件まで確認
    title = (e.get("title") or "").strip()
    link  = e.get("link") or ""
    print(f"- {title}\n  {link}\n")
