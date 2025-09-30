# 02_summarize_rss.py  ←フルリセット版
import os
import re
import html
import feedparser

# --- RSS（まずは2本。あとで増やせます） ---
FEEDS = [
    "https://www.pref.iwate.jp/news.rss",
    "https://www.city.morioka.iwate.jp/news.rss",
]

# --- 不動産・都市計画まわりの抽出キーワード ---
KEYWORDS = [
    "不動産","地価","地価調査","公示地価","路線価","固定資産税","地価指数",
    "住宅","空き家","空家","賃貸","分譲","マンション","戸建","団地",
    "用地","用地取得","収用","保留地","造成","宅地","宅地造成","区画","区画整理",
    "都市計画","用途地域","市街化","地区計画","立地適正化","再開発","再整備",
    "入札","公募","公示","公告","PFI","PPP",
    "道路","道路拡幅","区画道路","駅前","駅周辺","土地区画整理",
    "岩手","盛岡","花巻","北上","奥州","一関","二戸","久慈","宮古","大船渡","陸前高田","滝沢",
]

SUMMARY_LEN = 120  # 目安

def contains_keywords(text: str) -> bool:
    t = text or ""
    return any(k in t for k in KEYWORDS)

def clean_html(s: str) -> str:
    s = html.unescape(s or "")
    return re.sub(r"<[^>]+>", "", s)

def extract_body(entry) -> str:
    """
    RSSエントリから本文候補をできるだけ拾ってテキスト化
    優先: content[0].value → summary/description → ""
    """
    if entry.get("content") and isinstance(entry["content"], list) and entry["content"]:
        val = entry["content"][0].get("value") or ""
        if val.strip():
            return clean_html(val)
    val = entry.get("summary") or entry.get("description") or ""
    if val.strip():
        return clean_html(val)
    return ""

def fallback_summary(title: str, body: str) -> str:
    base = (body or title or "").strip()
    base = re.sub(r"\s+", " ", base)
    return base[:SUMMARY_LEN]

def summarize_ja(title: str, body: str, url: str) -> str:
    """
    日本語要約（約120字）。本文が薄いRSSでもできるだけ差異を出す。
    タイトル繰り返しを避けるための再生成ガード付き。
    """
    base = (body or "").strip()
    base = re.sub(r"\s+", " ", base)
    api_key = os.getenv("OPENAI_API_KEY", "")

    def _prompt(avoid_repetition: bool) -> str:
        rules = [
            "日本語で専門家向けに事実ベース、約120字。",
            "地名・主体・金額・面積・期日など固有情報を含める（可能なら）。",
            "不要: 絵文字・感想・推測。",
        ]
        if avoid_repetition:
            rules.append("絶対にタイトルの文言を繰り返さない。タイトルと異なる言い換え要約にする。")
        return (
            "以下は岩手県の不動産・土地・建設・都市計画に関するニュース素材です。\n"
            + "\n".join(f"- {r}" for r in rules) + "\n\n"
            f"【タイトル】{title}\n"
            f"【本文候補】{base or '(本文情報が乏しい)'}\n"
            f"【URL】{url}\n"
        )

    # API未設定ならフォールバック
    if not api_key:
        return f"[NO-API] {fallback_summary(title, base or title)}"

    # .env（あれば）読み込み
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # 1回目生成
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "あなたは日本語の要約編集者です。"},
                {"role": "user", "content": _prompt(avoid_repetition=False)},
            ],
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()

        # タイトルと同じ/酷似なら、もう一度だけ再生成
        normalized_title = re.sub(r"\s+", "", title)
        normalized_text  = re.sub(r"\s+", "", text)
        if normalized_text == normalized_title or normalized_title in normalized_text[:max(20, len(normalized_title)+5)]:
            resp2 = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": "あなたは日本語の要約編集者です。"},
                    {"role": "user", "content": _prompt(avoid_repetition=True)},
                ],
                temperature=0.2,
            )
            text = (resp2.choices[0].message.content or "").strip()

        return text[:SUMMARY_LEN * 2]
    except Exception:
        return f"[FALLBACK] {fallback_summary(title, base or title)}"

def main():
    total_entries = 0
    kept = []

    for feed_url in FEEDS:
        d = feedparser.parse(feed_url)
        for e in d.entries:
            total_entries += 1
            title = (e.get("title") or "").strip()
            link  = e.get("link") or ""
            body  = extract_body(e)
            haystack = f"{title}\n{body}"

            if contains_keywords(haystack):  # ←ここがキーワード抽出
                ai = summarize_ja(title, body, link)
                kept.append((title, ai, link))

    print(f"全取得件数: {total_entries}件")
    print(f"抽出件数   : {len(kept)}件\n")

    for title, ai, link in kept[:50]:
        print(f"■ {title}\n・要約: {ai}\n・URL: {link}\n")

if __name__ == "__main__":
    main()
