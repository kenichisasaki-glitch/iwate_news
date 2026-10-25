# 40_build_html.py — archive.jsonl から site/index.html を生成
# AI判定結果（カテゴリ・市町村）を活かした表示
# アーカイブは累積成長、表示は直近 DISPLAY_DAYS 日まで（環境変数で調整可）

import os
import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

ROOT = Path(os.getenv("IWATE_ROOT", ".")).resolve()
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
SITE_DIR.mkdir(parents=True, exist_ok=True)

SITE_TITLE = "岩手県 不動産まとめサイト"
SITE_SUBTITLE = "（毎日7:00自動更新・AI峻別）"
SITE_DESC = '<a href="https://www.greo-jp.com/" target="_blank">GREO合同会社が運営するまとめサイトです。</a>'
MAX_ITEMS = 1500
DISPLAY_DAYS = int(os.getenv("DISPLAY_DAYS", "180"))

CATEGORY_ORDER = [
    "店舗・商業施設",
    "工場・物流",
    "住宅・マンション",
    "地価・統計",
    "都市計画・再開発",
    "用地・公売",
    "跡地・転用",
    "観光・宿泊",
    "その他",
]

# 市町村プルダウンの並び（県の市町村コード順）。ここにない値（岩手県全域・不明など）は末尾
MUNI_ORDER = [
    "盛岡市", "宮古市", "大船渡市", "花巻市", "北上市", "久慈市", "遠野市", "一関市",
    "陸前高田市", "釜石市", "二戸市", "八幡平市", "奥州市", "滝沢市",
    "雫石町", "葛巻町", "岩手町", "紫波町", "矢巾町", "西和賀町", "金ケ崎町", "平泉町",
    "住田町", "大槌町", "山田町", "岩泉町", "田野畑村", "普代村", "軽米町", "野田村",
    "九戸村", "洋野町", "一戸町",
    "岩手県全域", "岩手県",
]

# 市町村の絞り込みと、表示中の記事のCSV出力（ブラウザ側で動作。JSが無効でも全件が日付順で表示される）
SCRIPT = """
(function(){
  var days=Array.prototype.slice.call(document.querySelectorAll('#list .day'));
  var items=Array.prototype.slice.call(document.querySelectorAll('#list .item'));
  var muniSel=document.getElementById('muni'), countEl=document.getElementById('shown');
  function visible(){return items.filter(function(el){return !el.hidden;});}
  function render(){
    var muni=muniSel.value;
    items.forEach(function(el){el.hidden=!!muni&&el.dataset.muni!==muni;});
    days.forEach(function(d){d.hidden=!d.querySelector('.item:not([hidden])');});
    countEl.textContent=visible().length;
  }
  function q(v){return '"'+String(v==null?'':v).replace(/"/g,'""')+'"';}
  muniSel.addEventListener('change',render);
  document.getElementById('csv').addEventListener('click',function(){
    var rows=[['公開日','市町村','カテゴリ','タイトル','出典','URL']];
    visible().forEach(function(el){var d=el.dataset,a=el.querySelector('h3 a');
      rows.push([d.date,d.muni,d.cat,a.textContent,d.src,a.href]);});
    var csv='\\ufeff'+rows.map(function(r){return r.map(q).join(',');}).join('\\r\\n');
    var a=document.createElement('a');
    a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
    var t=new Date(), m=muniSel.value;
    a.download='iwate_news_'+(m?m+'_':'')+t.getFullYear()+('0'+(t.getMonth()+1)).slice(-2)+('0'+t.getDate()).slice(-2)+'.csv';
    document.body.appendChild(a);a.click();a.remove();
  });
  render();
})();
"""


def iso_to_ymd_jst(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def load_archive() -> list[dict]:
    items = []
    path = DATA_DIR / "archive.jsonl"
    if not path.exists():
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def filter_recent(items: list[dict], days: int) -> list[dict]:
    if days <= 0:
        return items
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for it in items:
        try:
            dt = datetime.fromisoformat(it["published"].replace("Z", "+00:00"))
            if dt >= cutoff:
                out.append(it)
        except Exception:
            out.append(it)
    return out


def build_html(items: list[dict]) -> str:
    css = """
    body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,"Noto Sans JP",sans-serif;line-height:1.6;margin:20px;max-width:1100px;margin:20px auto;padding:0 16px;}
    header{margin-bottom:16px}
    h1{font-size:1.45rem;margin:0}
    .desc{color:#555;margin:4px 0 8px}
    .date{margin-top:22px;font-weight:700;border-bottom:1px solid #ddd;padding-bottom:4px}
    .item{border:1px solid #eee;border-radius:12px;padding:10px 12px;margin:10px 0}
    .item h3{margin:0 0 6px;font-size:1.02rem;line-height:1.4}
    .meta{font-size:.85rem;color:#666;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    .badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.75rem;color:#fff;background:#0a58ca}
    .badge.muni{background:#198754}
    .badge.cat-店舗・商業施設{background:#dc3545}
    .badge.cat-工場・物流{background:#fd7e14}
    .badge.cat-住宅・マンション{background:#0dcaf0}
    .badge.cat-地価・統計{background:#6f42c1}
    .badge.cat-都市計画・再開発{background:#20c997}
    .badge.cat-用地・公売{background:#ffc107;color:#000}
    .badge.cat-跡地・転用{background:#adb5bd}
    .badge.cat-観光・宿泊{background:#e83e8c}
    .badge.cat-その他{background:#6c757d}
    a{color:#0a58ca;text-decoration:none}
    a:hover{text-decoration:underline}
    footer{color:#777;font-size:.85rem;margin-top:24px;text-align:center}
    .toolbar{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #ddd;padding:8px 0;display:flex;gap:8px 14px;flex-wrap:wrap;align-items:center;font-size:.9rem}
    .toolbar select,.toolbar button{font-size:.9rem;padding:3px 8px}
    .toolbar .count{color:#555}
    [hidden]{display:none!important}
    @media (max-width:600px){.toolbar{position:static}}
    """

    muni_counts = defaultdict(int)
    for it in items:
        muni_counts[(it.get("judgment") or {}).get("municipality") or "不明"] += 1
    munis = sorted(
        muni_counts,
        key=lambda m: (MUNI_ORDER.index(m) if m in MUNI_ORDER else len(MUNI_ORDER), m),
    )
    muni_options = "".join(
        f'<option value="{html.escape(m)}">{html.escape(m)}（{muni_counts[m]}）</option>' for m in munis
    )
    toolbar = (
        '<div class="toolbar">'
        f'<label>市町村 <select id="muni"><option value="">すべて</option>{muni_options}</select></label>'
        '<span class="count">表示 <b id="shown">0</b> 件</span>'
        '<button type="button" id="csv">CSV出力</button>'
        "</div>"
    )

    items_sorted = sorted(items, key=lambda x: x.get("published", ""), reverse=True)[:MAX_ITEMS]

    groups = defaultdict(list)
    for it in items_sorted:
        day = iso_to_ymd_jst(it["published"]) or "日付不明"
        groups[day].append(it)

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ja">',
        '<meta charset="utf-8">',
        f"<title>{html.escape(SITE_TITLE)}</title>",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta name="description" content="{html.escape(SITE_TITLE + SITE_SUBTITLE)}">',
        f"<style>{css}</style>",
        "<body>",
        "<header>",
        f"<h1>{SITE_TITLE}<br>{SITE_SUBTITLE}</h1>",
        f'<div class="desc">{SITE_DESC}</div>',
        "</header>",
        toolbar,
        '<div id="list">',
    ]

    for day in sorted(groups.keys(), reverse=True):
        parts.append(f'<section class="day"><div class="date">📅 {day}</div>')
        day_items = sorted(
            groups[day],
            key=lambda x: CATEGORY_ORDER.index((x.get("judgment") or {}).get("category") or "その他")
                          if ((x.get("judgment") or {}).get("category") or "その他") in CATEGORY_ORDER
                          else len(CATEGORY_ORDER),
        )
        for it in day_items:
            j = it.get("judgment") or {}
            title = html.escape(it["title"] or "(無題)")
            url = html.escape(it["url"] or "#")
            raw_src = it.get("source") or ""
            if raw_src.startswith("Google:"):
                raw_src = "Google ニュース"
            elif raw_src.startswith("PR TIMES:"):
                raw_src = "PR TIMES"
            src = html.escape(raw_src)
            cat = j.get("category") or "その他"
            muni = j.get("municipality")
            cat_badge = f'<span class="badge cat-{html.escape(cat)}">{html.escape(cat)}</span>' if cat else ""
            muni_badge = f'<span class="badge muni">{html.escape(muni)}</span>' if muni else ""
            data_attrs = (
                f'data-date="{html.escape(day)}" '
                f'data-muni="{html.escape(muni or "不明")}" data-cat="{html.escape(cat)}" '
                f'data-src="{src}"'
            )
            parts.append(
                f'<div class="item" {data_attrs}>'
                f'<h3><a href="{url}" target="_blank" rel="noopener">{title}</a></h3>'
                f'<div class="meta">{cat_badge}{muni_badge}<span>出典: {src}</span></div>'
                f"</div>"
            )
        parts.append("</section>")

    parts += [
        "</div>",
        f"<script>{SCRIPT}</script>",
        '<footer><a href="https://www.greo-jp.com/" target="_blank">Operated by GREO</a></footer>',
        "</body></html>",
    ]
    return "\n".join(parts)


def main():
    archive = load_archive()
    recent = filter_recent(archive, DISPLAY_DAYS)
    print(f"[build] archive={len(archive)} → display(last {DISPLAY_DAYS}d)={len(recent)}")
    out = SITE_DIR / "index.html"
    out.write_text(build_html(recent), encoding="utf-8")
    print(f"[done] → {out}")


if __name__ == "__main__":
    main()
