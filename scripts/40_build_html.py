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

# 市町村ソート用の並び（県の市町村コード順）。ここにない値（岩手県全域・不明など）は末尾
MUNI_ORDER = [
    "盛岡市", "宮古市", "大船渡市", "花巻市", "北上市", "久慈市", "遠野市", "一関市",
    "陸前高田市", "釜石市", "二戸市", "八幡平市", "奥州市", "滝沢市",
    "雫石町", "葛巻町", "岩手町", "紫波町", "矢巾町", "西和賀町", "金ケ崎町", "平泉町",
    "住田町", "大槌町", "山田町", "岩泉町", "田野畑村", "普代村", "軽米町", "野田村",
    "九戸村", "洋野町", "一戸町",
    "岩手県全域", "岩手県",
]

# 絞り込み・並び替え・CSV出力（ブラウザ側で動作。JSが無効でも従来どおり日付順で表示される）
SCRIPT = """
(function(){
  var list=document.getElementById('list');
  var items=Array.prototype.slice.call(list.querySelectorAll('.item'));
  var order=JSON.parse(document.getElementById('muni-order').textContent);
  var sortSel=document.getElementById('sort'), muniSel=document.getElementById('muni');
  var countEl=document.getElementById('selcount'), csvBtn=document.getElementById('csv');
  function rank(m){var i=order.indexOf(m);return i<0?order.length:i;}
  function header(text){var d=document.createElement('div');d.className='date';d.textContent=text;return d;}
  function render(){
    var byMuni=sortSel.value==='muni', muni=muniSel.value;
    document.body.classList.toggle('by-muni',byMuni);
    var shown=items.filter(function(el){return !muni||el.dataset.muni===muni;});
    shown.sort(function(a,b){
      if(byMuni){var r=rank(a.dataset.muni)-rank(b.dataset.muni);if(r)return r;
        if(a.dataset.muni!==b.dataset.muni)return a.dataset.muni<b.dataset.muni?-1:1;}
      return a.dataset.date<b.dataset.date?1:a.dataset.date>b.dataset.date?-1:(+a.dataset.idx)-(+b.dataset.idx);
    });
    list.textContent='';
    var last=null;
    shown.forEach(function(el){
      var key=byMuni?el.dataset.muni:el.dataset.date;
      if(key!==last){list.appendChild(header(byMuni?'📍 '+key:'📅 '+key));last=key;}
      list.appendChild(el);
    });
    document.getElementById('shown').textContent=shown.length;
  }
  function checked(){return items.filter(function(el){return el.querySelector('.pick').checked;});}
  function updateCount(){var n=checked().length;countEl.textContent=n;csvBtn.disabled=!n;}
  function q(v){return '"'+String(v==null?'':v).replace(/"/g,'""')+'"';}
  sortSel.addEventListener('change',render);
  muniSel.addEventListener('change',render);
  list.addEventListener('change',function(e){if(e.target.classList.contains('pick'))updateCount();});
  document.getElementById('all').addEventListener('click',function(){
    list.querySelectorAll('.pick').forEach(function(c){c.checked=true;});updateCount();});
  document.getElementById('none').addEventListener('click',function(){
    items.forEach(function(el){el.querySelector('.pick').checked=false;});updateCount();});
  csvBtn.addEventListener('click',function(){
    var rows=[['公開日','市町村','カテゴリ','タイトル','出典','URL']];
    checked().sort(function(a,b){return a.dataset.date<b.dataset.date?1:a.dataset.date>b.dataset.date?-1:0;})
      .forEach(function(el){var d=el.dataset,a=el.querySelector('h3 a');
        rows.push([d.date,d.muni,d.cat,a.textContent,d.src,a.href]);});
    var csv='\\ufeff'+rows.map(function(r){return r.map(q).join(',');}).join('\\r\\n');
    var a=document.createElement('a');
    a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
    var t=new Date();
    a.download='iwate_news_'+t.getFullYear()+('0'+(t.getMonth()+1)).slice(-2)+('0'+t.getDate()).slice(-2)+'.csv';
    document.body.appendChild(a);a.click();a.remove();
  });
  render();updateCount();
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
    .item{display:flex;gap:10px;align-items:flex-start}
    .item .pick{margin-top:5px;width:17px;height:17px;flex:none}
    .item .body{flex:1;min-width:0}
    .item .d{display:none}
    .by-muni .item .d{display:inline}
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
        '<label>並び順 <select id="sort"><option value="date">日付順</option>'
        '<option value="muni">市町村順</option></select></label>'
        f'<label>市町村 <select id="muni"><option value="">すべて</option>{muni_options}</select></label>'
        '<span class="count">表示 <b id="shown">0</b> 件</span>'
        '<button type="button" id="all">表示中をすべて選択</button>'
        '<button type="button" id="none">選択解除</button>'
        '<button type="button" id="csv" disabled>選択をCSV出力（<span id="selcount">0</span>件）</button>'
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

    idx = 0
    for day in sorted(groups.keys(), reverse=True):
        parts.append(f'<div class="date">📅 {day}</div>')
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
                f'data-idx="{idx}" data-date="{html.escape(day)}" '
                f'data-muni="{html.escape(muni or "不明")}" data-cat="{html.escape(cat)}" '
                f'data-src="{src}"'
            )
            idx += 1
            parts.append(
                f'<div class="item" {data_attrs}>'
                f'<input type="checkbox" class="pick" aria-label="選択">'
                f'<div class="body">'
                f'<h3><a href="{url}" target="_blank" rel="noopener">{title}</a></h3>'
                f'<div class="meta">{cat_badge}{muni_badge}<span>出典: {src}</span>'
                f'<span class="d">📅 {html.escape(day)}</span></div>'
                f"</div></div>"
            )

    parts += [
        "</div>",
        f'<script type="application/json" id="muni-order">{json.dumps(MUNI_ORDER, ensure_ascii=False)}</script>',
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
