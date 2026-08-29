#!/usr/bin/env python3
"""かん報 紙面生成スクリプト

feeds.json の購読リストを巡回して記事を集め、興味キーワードで
スコアリングし、docs/index.html に「今日の紙面」を書き出す。

使い方:
    python3 build.py            # 直近24時間分で紙面を生成
    python3 build.py --hours 48 # 集める期間を変えたいとき
"""

import argparse
import concurrent.futures
import html
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).parent
JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) kanpo/1.0"
FETCH_TIMEOUT = 20

# ---------------------------------------------------------------- 記事収集


ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"


def parse_date(text):
    """RFC822（RSS）とISO8601（Atom）の両方の日付をJSTに変換する。"""
    if not text:
        return None
    text = text.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(text)
    except Exception:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST)


def first_text(el, *tags):
    for t in tags:
        node = el.find(t)
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    return ""


def parse_feed_xml(raw):
    """RSS 2.0 / Atom / RSS 1.0 (RDF) を標準ライブラリだけで読む。

    返り値: [{title, link, date_text, summary, source_name}, ...]
    """
    root = ET.fromstring(raw)
    tag = root.tag.lower()
    items = []

    if tag.endswith("rss"):  # RSS 2.0（購読ブログ・Google Newsの大半）
        for it in root.findall("./channel/item"):
            items.append(
                {
                    "title": first_text(it, "title"),
                    "link": first_text(it, "link"),
                    "date_text": first_text(it, "pubDate", DC + "date"),
                    "summary": first_text(it, "description"),
                    "source_name": first_text(it, "source"),
                }
            )
    elif tag.endswith("feed"):  # Atom（Googleアラート・YouTube）
        for it in root.findall(ATOM + "entry"):
            link = ""
            for ln in it.findall(ATOM + "link"):
                if ln.get("rel") in (None, "alternate"):
                    link = ln.get("href", "")
                    break
            items.append(
                {
                    "title": first_text(it, ATOM + "title"),
                    "link": link,
                    "date_text": first_text(it, ATOM + "published", ATOM + "updated"),
                    "summary": first_text(it, ATOM + "summary", ATOM + "content"),
                    "source_name": "",
                }
            )
    elif tag.endswith("rdf"):  # RSS 1.0
        for it in root.findall(RSS1 + "item"):
            items.append(
                {
                    "title": first_text(it, RSS1 + "title"),
                    "link": first_text(it, RSS1 + "link"),
                    "date_text": first_text(it, DC + "date"),
                    "summary": first_text(it, RSS1 + "description"),
                    "source_name": "",
                }
            )
    return items


def sanitize_xml(raw):
    """規格違反のXMLをパース前に修復する（Goodpatch等の生アンパサンド対策）。"""
    raw = re.sub(rb"&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)", b"&amp;", raw)
    raw = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", raw)  # XML禁止の制御文字
    return raw


def clean_link(link):
    """Googleアラートのリダイレクト包みURLから本来のURLを取り出す。"""
    if link.startswith("https://www.google.com/url"):
        q = parse_qs(urlparse(link).query)
        return q.get("url", [link])[0]
    return link


def fetch_feed(source):
    """1本のフィードを取得して記事リストを返す。失敗したらエラー情報を返す。"""
    req = urllib.request.Request(source["url"], headers={"User-Agent": USER_AGENT})
    raw_items = None
    for attempt in (1, 2):  # 一時的なエラーに備えて1回だけ再試行
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as res:
                raw = res.read()
            raw_items = parse_feed_xml(sanitize_xml(raw))
            break
        except Exception as e:
            if attempt == 2:
                return {"source": source, "error": str(e)[:120], "entries": []}
            time.sleep(2)

    entries = []
    for it in raw_items:
        title = strip_html(it["title"])
        link = clean_link(it["link"])
        if not title or not link:
            continue

        # Google News 系はタイトル末尾に「 - 媒体名」が付くので分離する
        src_name = source["name"]
        gn_source = strip_html(it["source_name"])
        if gn_source and title.endswith(" - " + gn_source):
            title = title[: -(len(gn_source) + 3)].strip()
            src_name = gn_source

        excerpt = strip_html(it["summary"])
        # 抜粋がタイトルの繰り返しだけなら捨てる（Google News・アラート対策）
        if excerpt and (excerpt.startswith(title[:20]) or len(excerpt) < 15):
            excerpt = ""

        entries.append(
            {
                "title": title,
                "link": link,
                "published": parse_date(it["date_text"]),
                "excerpt": excerpt[:110],
                "src": src_name,
            }
        )
    return {"source": source, "error": None, "entries": entries}


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------ スコアリング


def keyword_patterns(keywords):
    """キーワード定義をマッチ用の正規表現に変換する。

    英字だけのキーワード（AI/UI/UX等）は単語の切れ目を見る。
    そうしないと PORTRAIT の「ai」みたいな偶然の一致まで拾ってしまう。
    """
    pats = []
    for kw in keywords:
        word = kw["word"]
        if re.fullmatch(r"[A-Za-z/]+", word):
            pat = re.compile(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])")
        else:
            pat = re.compile(re.escape(word))
        pats.append({"word": word, "weight": kw["weight"], "pat": pat})
    return pats


def score_article(article, patterns, title_only=False):
    """タイトル一致は重み×2、本文抜粋の一致は重み×1。

    Googleアラート/Google News系は抜粋にノイズ（無関係な文章）が
    混ざりやすいので title_only=True でタイトルだけを見る。
    """
    score = 0
    matched = []
    for p in patterns:
        in_title = bool(p["pat"].search(article["title"]))
        in_excerpt = (not title_only) and bool(p["pat"].search(article["excerpt"]))
        if in_title:
            score += p["weight"] * 2
        elif in_excerpt:
            score += p["weight"]
        if in_title or in_excerpt:
            matched.append(p["word"])
    article["score"] = score
    article["matched"] = matched


def stars(score):
    for threshold, n in ((8, 5), (5, 4), (3, 3), (1, 2)):
        if score >= threshold:
            return "★" * n + "☆" * (5 - n)
    return "★☆☆☆☆"


# -------------------------------------------------------------- HTML生成


def esc(s):
    return html.escape(s or "", quote=True)


def fmt_time(dt):
    return f"{dt.month}月{dt.day}日 {dt:%H:%M}" if dt else ""


def tags_html(article):
    if not article["matched"]:
        return ""
    chips = "".join(f'<span class="tag">{esc(w)}</span>' for w in article["matched"])
    return f'　　<span class="tags">{chips}</span>'


def meta_html(article, with_score=False):
    score = f'　　<span class="score">注目度 {stars(article["score"])}</span>' if with_score else ""
    return (
        f'<p class="meta"><span class="src">{esc(article["src"])}</span>'
        f'　{fmt_time(article["published"])}{score}{tags_html(article)}</p>'
    )


def front_html(top3):
    if not top3:
        return ""
    top = top3[0]
    excerpt = f'<p class="excerpt">{esc(top["excerpt"])}……</p>' if top["excerpt"] else ""
    parts = [
        '  <section class="card">',
        '    <article class="front-top">',
        '      <span class="kicker">一面</span>' + tags_html(top).replace("　　", " ", 1),
        f'      <a href="{esc(top["link"])}" target="_blank" rel="noopener">'
        f'<h2 class="headline">{esc(top["title"])}</h2></a>',
        f"      {excerpt}",
        f'      {meta_html(top, with_score=True)}',
        "    </article>",
    ]
    subs = top3[1:3]
    if subs:
        parts.append('    <div class="front-subs">')
        for a in subs:
            sub_excerpt = f'<p class="excerpt">{esc(a["excerpt"])}……</p>' if a["excerpt"] else ""
            parts += [
                "      <article>",
                f'        <a href="{esc(a["link"])}" target="_blank" rel="noopener">'
                f'<h3 class="headline">{esc(a["title"])}</h3></a>',
                f"        {sub_excerpt}",
                f"        {meta_html(a)}",
                "      </article>",
            ]
        parts.append("    </div>")
    parts.append("  </section>")
    return "\n".join(parts)


def section_html(section, articles, visible, maximum):
    if maximum:  # nullなら全件掲載
        articles = articles[:maximum]
    parts = [
        '  <section class="card section">',
        '    <div class="section-head">'
        f'<h2><span class="material-symbols-outlined">{esc(section["icon"])}</span>{esc(section["title"])}</h2>'
        f'<span class="count">{len(articles)}件</span></div>',
    ]
    if not articles:
        parts.append('    <p class="empty-note">今朝は新着なしやで。</p>')
    for i, a in enumerate(articles):
        extra = " extra" if i >= visible else ""
        excerpt = f'<p class="excerpt">{esc(a["excerpt"])}……</p>' if a["excerpt"] else ""
        parts += [
            f'    <article class="item{extra}">',
            f'      <a href="{esc(a["link"])}" target="_blank" rel="noopener">'
            f'<h3 class="headline">{esc(a["title"])}</h3></a>',
            f"      {excerpt}",
            f"      {meta_html(a)}",
            "    </article>",
        ]
    hidden = max(0, len(articles) - visible)
    if hidden:
        parts.append(
            f'    <button class="more-btn" data-count="{hidden}">もっとみる（あと{hidden}件）▼</button>'
        )
    parts.append("  </section>")
    return "\n".join(parts)


# ---------------------------------------------------------------- メイン


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None, help="何時間分の記事を集めるか")
    args = ap.parse_args()

    config = json.loads((BASE / "feeds.json").read_text(encoding="utf-8"))
    meta = config["meta"]
    hours = args.hours or meta["window_hours"]

    now = datetime.now(JST)
    since = now - timedelta(hours=hours)

    # ---- 収集（並列） ----
    jobs = [
        {"section": sec["id"], **src}
        for sec in config["sections"]
        for src in sec["sources"]
    ]
    print(f"📡 {len(jobs)}本のフィードを巡回中…", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(fetch_feed, jobs))

    errors = [r for r in results if r["error"]]
    for r in errors:
        print(f"⚠️  取得失敗: {r['source']['name']} → {r['error']}", file=sys.stderr)

    # ---- 期間フィルタ＋重複除去＋スコアリング ----
    patterns = keyword_patterns(config["keywords"])
    seen_links, seen_titles = set(), set()
    by_section = {sec["id"]: [] for sec in config["sections"]}
    total = 0

    for r in results:
        for a in r["entries"]:
            if a["published"] and a["published"] < since:
                continue
            if a["published"] and a["published"] > now + timedelta(hours=1):
                continue  # 未来日付のゴミ対策
            if not a["published"]:
                continue  # 日付なしは朝刊に載せない
            # 重複判定はタイトル先頭28文字（末尾の「...」切り詰め違いを吸収）
            key_t = re.sub(r"\s+", "", a["title"])
            key_t = re.sub(r"[.…]+$", "", key_t)[:28]
            if a["link"] in seen_links or key_t in seen_titles:
                continue
            seen_links.add(a["link"])
            seen_titles.add(key_t)
            url = r["source"]["url"]
            noisy = "news.google.com" in url or "alerts/feeds" in url
            if noisy:
                a["excerpt"] = ""  # アラート系の抜粋は断片的で汚いので載せない
            score_article(a, patterns, title_only=noisy)
            by_section[r["source"]["section"]].append(a)
            total += 1

    # スコア降順 → 同点なら新しい順
    for arts in by_section.values():
        arts.sort(key=lambda a: a["published"], reverse=True)
        arts.sort(key=lambda a: -a["score"])

    # ---- 一面（全セクション横断でスコア上位3本） ----
    all_articles = [a for arts in by_section.values() for a in arts]
    all_articles.sort(key=lambda a: a["published"], reverse=True)
    all_articles.sort(key=lambda a: -a["score"])
    top3 = all_articles[:3]
    top_links = {a["link"] for a in top3}

    # ---- HTML組み立て ----
    launch = datetime.strptime(meta["launch_date"], "%Y-%m-%d").date()
    issue_no = max(1, (now.date() - launch).days + 1)
    date_line = f"{now.year}年{now.month}月{now.day}日　{WEEKDAYS_JA[now.weekday()]}曜日"

    sections_html = "\n\n".join(
        section_html(
            sec,
            [a for a in by_section[sec["id"]] if a["link"] not in top_links],
            meta["section_visible"],
            meta["section_max"],
        )
        for sec in config["sections"]
    )
    crawl = (
        f"今朝の巡回：フィード {len(jobs)}本（うち取得失敗 {len(errors)}本） ／ "
        f"拾った記事 {total}件　＜ {since.month}月{since.day}日 {since:%H:%M} 〜 "
        f"{now.month}月{now.day}日 {now:%H:%M} ＞"
    )

    page = (BASE / "templates" / "page.html").read_text(encoding="utf-8")
    page = (
        page.replace("%%TITLE_DATE%%", f"{now.year}年{now.month}月{now.day}日 朝刊")
        .replace("%%DATE_LINE%%", date_line)
        .replace("%%ISSUE%%", f"第{issue_no}号")
        .replace("%%TAGLINE%%", meta["tagline"])
        .replace("%%CRAWL_NOTE%%", crawl)
        .replace("%%FRONT%%", front_html(top3))
        .replace("%%SECTIONS%%", sections_html)
        .replace("%%GENERATED_AT%%", f"{now:%H:%M}")
    )

    out = BASE / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"✅ 刷り上がり: {out}（記事{total}件・一面{len(top3)}本）", file=sys.stderr)


if __name__ == "__main__":
    main()
