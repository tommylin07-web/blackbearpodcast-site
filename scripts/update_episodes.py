#!/usr/bin/env python3
"""Pull new Black Bear episodes from the wavpub RSS into the static site.

The live site is the GitHub Pages source itself (HTML on main, legacy Pages
build). There is no Astro project in this repository. This script only adds
RSS items published after the newest episode already on the site, using the
feed's title, date, show notes, artwork, audio, and link. It does not rewrite
existing episode copy.

Dates follow the pages already on the site: the calendar day is the pubDate
converted to UTC (the 2026-07-21 00:07 +0800 episode is labeled 2026年7月20日).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

RSS_URL = "https://s1.proxy.wavpub.com/blackbearpodcasting.xml"
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
SITE = "https://blackbearpodcast.com"
# Card blurbs on the existing homepage are 72 characters plus an ellipsis.
BLURB_LIMIT = 72
PLAY = (
    '<span class="ep-box-play" aria-hidden="true">'
    '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"></path></svg></span>'
)
ROOT = Path(__file__).resolve().parent.parent


@dataclass
class FeedItem:
    title: str
    published: datetime
    link: str
    guid: str
    enclosure: str
    enclosure_type: str
    duration_seconds: int | None
    image: str
    notes_html: str


@dataclass
class SiteEpisode:
    number: int
    title: str
    enclosure: str
    guid: str


def fetch_rss(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "blackbearpodcast-site-updater"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def parse_duration(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return int(float(raw))
    parts = raw.split(":")
    if not all(re.fullmatch(r"\d+", part) for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return hours * 3600 + minutes * 60 + seconds
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds
    if len(numbers) == 1:
        return numbers[0]
    return None


def format_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def utc_day(when: datetime) -> datetime:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def display_date(when: datetime) -> str:
    day = utc_day(when)
    return f"{day.year}年{day.month}月{day.day}日"


def iso_date(when: datetime) -> str:
    day = utc_day(when)
    return day.date().isoformat()


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'\b{name}="([^"]*)"', tag, flags=re.I)
    if match:
        return match.group(1)
    match = re.search(rf"\b{name}='([^']*)'", tag, flags=re.I)
    return match.group(1) if match else ""


def _img_tag(match: re.Match[str]) -> str:
    src = _attr(match.group(0), "src")
    return f'<img src="{src}">' if src else ""


def _a_tag(match: re.Match[str]) -> str:
    href = _attr(match.group(0), "href")
    if not href:
        return "<a>"
    return f'<a href="{href}">'


def clean_notes(raw: str) -> str:
    """Strip feed presentation attributes and keep the show-note text.

    The newest baked episode (ep/195) is this transform of its RSS description:
    drop inline styles, keep paragraphs, breaks, links, and images.
    """
    text = raw.strip()
    text = re.sub(r"<span>\s*<br\s*/?>\s*</span>", "<br>", text, flags=re.I)
    text = re.sub(r"<img\b[^>]*>", _img_tag, text, flags=re.I)
    text = re.sub(r"<a\b[^>]*>", _a_tag, text, flags=re.I)
    for tag in ("p", "strong", "b", "em", "i", "blockquote"):
        text = re.sub(rf"<{tag}\b[^>]*>", f"<{tag}>", text, flags=re.I)
    text = re.sub(r"<hr\b[^>]*>", "<hr>", text, flags=re.I)
    text = re.sub(r"</?span\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "<br>", text, flags=re.I)
    return text


def plain_text(notes_html: str) -> str:
    parts: list[str] = []
    for chunk in re.findall(r"<p>(.*?)</p>", notes_html, flags=re.S):
        chunk = re.sub(r"<br>", "", chunk, flags=re.I)
        chunk = re.sub(r"<[^>]+>", "", chunk)
        chunk = html.unescape(chunk).strip()
        if chunk:
            parts.append(chunk)
    if parts:
        return "".join(parts)
    fallback = re.sub(r"<[^>]+>", "", notes_html)
    return html.unescape(fallback).strip()


def blurb(notes_html: str, limit: int = BLURB_LIMIT) -> str:
    text = plain_text(notes_html)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def parse_feed(xml_bytes: bytes) -> list[FeedItem]:
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise SystemExit("RSS has no channel")
    items: list[FeedItem] = []
    for node in channel.findall("item"):
        title = (node.findtext("title") or "").strip()
        published_raw = (node.findtext("pubDate") or "").strip()
        if not title or not published_raw:
            continue
        published = parsedate_to_datetime(published_raw)
        enclosure = node.find("enclosure")
        enclosure_url = (enclosure.attrib.get("url") or "").strip() if enclosure is not None else ""
        enclosure_type = (enclosure.attrib.get("type") or "").strip() if enclosure is not None else ""
        image = node.find(f"{{{ITUNES}}}image")
        image_url = (image.attrib.get("href") or "").strip() if image is not None else ""
        items.append(
            FeedItem(
                title=title,
                published=published,
                link=(node.findtext("link") or "").strip(),
                guid=(node.findtext("guid") or "").strip(),
                enclosure=enclosure_url,
                enclosure_type=enclosure_type,
                duration_seconds=parse_duration(node.findtext(f"{{{ITUNES}}}duration") or ""),
                image=image_url,
                notes_html=clean_notes(node.findtext("description") or ""),
            )
        )
    return items


def read_site_episodes() -> list[SiteEpisode]:
    episodes: list[SiteEpisode] = []
    ep_root = ROOT / "ep"
    for path in ep_root.iterdir():
        if not path.is_dir() or not path.name.isdigit():
            continue
        page = (path / "index.html").read_text(encoding="utf-8")
        title_match = re.search(r'<h1 class="ep-page-title">(.*?)</h1>', page)
        source_match = re.search(r'<source src="([^"]+)"', page) or re.search(
            r'<audio\b[^>]*\bsrc="([^"]+)"', page
        )
        guid_match = re.search(r'<meta name="blackbear-guid" content="([^"]*)"', page)
        if not title_match or not source_match:
            raise SystemExit(f"Could not read title or audio from ep/{path.name}")
        episodes.append(
            SiteEpisode(
                number=int(path.name),
                title=html.unescape(title_match.group(1)),
                enclosure=html.unescape(source_match.group(1)),
                guid=html.unescape(guid_match.group(1)) if guid_match else "",
            )
        )
    episodes.sort(key=lambda episode: episode.number)
    return episodes


def audio_type(item: FeedItem) -> str:
    kind = item.enclosure_type.lower()
    url = item.enclosure.lower()
    if "mpeg" in kind or url.endswith(".mp3"):
        return "audio/mpeg"
    return "audio/mp4"


def episode_code(number: int) -> str:
    return f"EP{number:03d}"


def load_chrome(reference_html: str) -> tuple[str, str]:
    nav = re.search(r"<nav id=\"site-nav\">.*?</nav>", reference_html, flags=re.S)
    footer = re.search(r"<footer>.*?</script>", reference_html, flags=re.S)
    if not nav or not footer:
        raise SystemExit("Could not read nav or footer from the reference episode")
    return nav.group(0), footer.group(0)


def render_episode_page(
    item: FeedItem,
    number: int,
    prev_episode: SiteEpisode | None,
    next_episode: tuple[int, str] | None,
    nav: str,
    footer: str,
) -> str:
    if re.search(r"</div|<script", item.notes_html, flags=re.I):
        raise SystemExit(f"Show notes for {item.title!r} contain markup this page cannot embed")
    code = episode_code(number)
    summary = blurb(item.notes_html)
    escaped_title = html.escape(item.title, quote=True)
    escaped_summary = html.escape(summary, quote=True)
    cover = item.image or f"{SITE}/logo.png"
    if "'" in cover or '"' in cover:
        raise SystemExit(f"Artwork URL has a quote and cannot be embedded safely: {cover}")
    day = iso_date(item.published)
    shown = display_date(item.published)
    duration = (
        f" <span>·</span> <span>时长 {format_duration(item.duration_seconds)}</span>"
        if item.duration_seconds is not None
        else ""
    )
    if prev_episode is None:
        raise SystemExit(f"Refusing to create {code} without a previous episode")
    prev_html = (
        f'<a href="/ep/{prev_episode.number}/"> '
        f'<div class="pager-dir">← 上一期</div> '
        f'<div class="pager-title">{html.escape(prev_episode.title, quote=True)}</div> </a>'
    )
    if next_episode is None:
        next_html = "<span></span>"
    else:
        next_number, next_title = next_episode
        next_html = (
            f'<a href="/ep/{next_number}/" class="pager-next"> '
            f'<div class="pager-dir">下一期 →</div> '
            f'<div class="pager-title">{html.escape(next_title, quote=True)}</div> </a>'
        )
    source_link = ""
    if item.link:
        label = "🔶 喜马拉雅" if "ximalaya.com" in item.link else "🔗 本期链接"
        source_link = (
            f'<a class="platform-link" href="{html.escape(item.link, quote=True)}" '
            f'target="_blank" rel="noopener">{label}</a> '
        )
    linked = {
        "@context": "https://schema.org",
        "@type": "PodcastEpisode",
        "name": item.title,
        "episodeNumber": number,
        "datePublished": day,
        "description": summary,
        "url": f"{SITE}/ep/{number}/",
        "image": cover,
        "associatedMedia": {"@type": "MediaObject", "contentUrl": item.enclosure},
        "partOfSeries": {
            "@type": "PodcastSeries",
            "name": "黑熊电台 BlackBear Podcast",
            "url": f"{SITE}/",
        },
    }
    if item.link:
        linked["sameAs"] = item.link
    linked_json = json.dumps(linked, ensure_ascii=False, separators=(",", ":"))
    guid = html.escape(item.guid, quote=True)
    page_title = f"{escaped_title} | 黑熊电台 {code}"
    return (
        '<!DOCTYPE html><html lang="zh-CN"> <head><meta charset="UTF-8">'
        f'<meta name="blackbear-guid" content="{guid}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>{page_title}</title>"
        f'<meta name="description" content="{escaped_summary}">'
        f'<link rel="canonical" href="{SITE}/ep/{number}/">'
        f'<meta property="og:title" content="{page_title}">'
        f'<meta property="og:description" content="{escaped_summary}">'
        '<meta property="og:type" content="article">'
        f'<meta property="og:url" content="{SITE}/ep/{number}/">'
        f'<meta property="og:image" content="{html.escape(cover, quote=True)}">'
        '<meta name="twitter:card" content="summary_large_image">'
        '<link rel="icon" href="/logo.svg" type="image/svg+xml">'
        '<link rel="alternate" type="application/rss+xml" title="黑熊电台 BlackBear Podcast" '
        'href="https://s1.proxy.wavpub.com/blackbearpodcasting.xml">'
        '<link rel="sitemap" href="/sitemap-index.xml">'
        f'<script type="application/ld+json">{linked_json}</script>'
        '<link rel="stylesheet" href="/_astro/Base.DhU08HZ0.css"></head> <body> '
        f"{nav}   <main class=\"ep-page\"> "
        f'<div class="ep-breadcrumb"> <a href="/">首页</a> / <a href="/#episodes">节目归档</a> / <span>{code}</span> </div> '
        '<header class="ep-page-header"> '
        f'<img class="ep-page-cover" src="{html.escape(cover, quote=True)}" alt="{escaped_title}" loading="eager"> '
        f"<div> <div class=\"ep-page-label\">{code}</div> "
        f'<h1 class="ep-page-title">{escaped_title}</h1> '
        f'<div class="ep-page-meta"> <span>{shown}</span>{duration} </div> </div> </header> '
        '<div class="ep-page-player"> '
        f'<audio controls preload="metadata"> <source src="{html.escape(item.enclosure, quote=True)}" type="{audio_type(item)}">\n'
        f'你的浏览器不支持音频播放，请<a href="{html.escape(item.enclosure, quote=True)}">下载收听</a>。\n'
        "</audio> </div> "
        '<section class="ep-notes"> <h2>本期节目</h2> '
        f'<div class="ep-notes-content">{item.notes_html}</div> </section>  '
        f'<nav class="ep-pager"> {prev_html} {next_html} </nav> '
        '<div class="platform-links" style="display:flex;gap:0.75rem;flex-wrap:wrap;margin-top:1.5rem"> '
        f"{source_link}"
        '<a class="platform-link" href="https://www.xiaoyuzhoufm.com/podcast/5e509e58418a84a046b2d765" target="_blank" rel="noopener">🌌 小宇宙</a> '
        '<a class="platform-link" href="https://open.spotify.com/show/1Frw3r94vPApZZ4nIIP1zZ" target="_blank" rel="noopener">🎵 Spotify</a> '
        '<a class="platform-link" href="https://podcasts.apple.com/cn/podcast/id1443987212" target="_blank" rel="noopener">🎙️ Apple 播客</a> '
        '<a class="platform-link" href="https://s1.proxy.wavpub.com/blackbearpodcasting.xml" target="_blank" rel="noopener">📡 RSS</a> '
        "</div> </main>  "
        f"{footer} </body> </html>"
    )


def homepage_card(number: int, shown: str, title: str, summary: str, cover: str) -> str:
    return (
        f'<a class="ep-box" href="/ep/{number}/"> '
        f"<div class=\"ep-box-cover\" style=\"background-image:url('{cover}')\"> {PLAY} </div> "
        '<div class="ep-box-body"> <div class="ep-meta"> '
        f'<span class="ep-date">{html.escape(shown)}</span> </div> '
        f'<div class="ep-box-title">{html.escape(title)}</div> '
        f'<div class="ep-box-desc">{html.escape(summary, quote=True)}</div> '
        "</div> </a>"
    )


def archive_card(number: int, shown: str, title: str, summary: str, cover: str) -> str:
    return (
        f'<a class="ep-box episode-card" href="/ep/{number}/" '
        f'data-title="{html.escape(title, quote=True)}" '
        f'data-desc="{html.escape(summary, quote=True)}" data-tag=""> '
        f"<div class=\"ep-box-cover\" style=\"background-image:url('{cover}')\"> {PLAY} </div> "
        '<div class="ep-box-body"> <div class="ep-meta"> '
        f'<span class="ep-date">{html.escape(shown)}</span> </div> '
        f'<div class="ep-box-title">{html.escape(title)}</div> '
        f'<div class="ep-box-desc">{html.escape(summary, quote=True)}</div> '
        "</div> </a>"
    )


def existing_homepage_cards(index_html: str) -> dict[int, str]:
    cards: dict[int, str] = {}
    for match in re.finditer(r'<a class="ep-box" href="/ep/(\d+)/">.*?</a>', index_html, flags=re.S):
        cards[int(match.group(1))] = match.group(0)
    if len(cards) < 6:
        raise SystemExit("Homepage does not have the expected six episode cards")
    return cards


def replace_homepage_cards(index_html: str, cards: list[str]) -> str:
    updated, count = re.subn(
        r'(<div class="ep-matrix reveal">).*?(</div> <div class="ep-more reveal">)',
        lambda match: f"{match.group(1)} {''.join(cards)}{match.group(2)}",
        index_html,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise SystemExit("Could not find the homepage episode list")
    return updated


def point_latest_button(index_html: str, number: int) -> str:
    updated, count = re.subn(
        r'(<a href="/ep/)\d+(/" class="btn btn-primary">)',
        rf"\g<1>{number}\2",
        index_html,
        count=1,
    )
    if count != 1:
        raise SystemExit("Could not find the homepage latest-episode button")
    return updated


def replace_count(text: str, old: str, new: str, phrases: list[str]) -> str:
    for phrase in phrases:
        previous = phrase.format(n=old)
        replacement = phrase.format(n=new)
        if previous not in text:
            raise SystemExit(f"Could not find episode count phrase: {previous}")
        text = text.replace(previous, replacement)
    return text


def link_next_episode(page: str, number: int, title: str) -> str:
    match = re.search(r"<nav class=\"ep-pager\">(.*?)</nav>", page, flags=re.S)
    if not match:
        raise SystemExit("Previous episode is missing its pager")
    inner = match.group(1)
    if 'class="pager-next"' in inner:
        raise SystemExit("Previous episode already links to a newer episode")
    if "<span></span>" not in inner:
        raise SystemExit("Previous episode pager has no empty next slot")
    link = (
        f'<a href="/ep/{number}/" class="pager-next"> '
        f'<div class="pager-dir">下一期 →</div> '
        f'<div class="pager-title">{html.escape(title, quote=True)}</div> </a>'
    )
    inner = inner.replace("<span></span>", link, 1)
    return page[: match.start()] + "<nav class=\"ep-pager\">" + inner + "</nav>" + page[match.end() :]


def insert_sitemap(numbers: list[int]) -> None:
    path = ROOT / "sitemap-0.xml"
    text = path.read_text(encoding="utf-8")
    needle = f"<url><loc>{SITE}/episodes/</loc></url>"
    if needle not in text:
        raise SystemExit("Sitemap is missing the episodes index URL")
    chunk = "".join(f"<url><loc>{SITE}/ep/{number}/</loc></url>" for number in numbers if f"/ep/{number}/" not in text)
    if chunk:
        text = text.replace(needle, chunk + needle, 1)
        path.write_text(text, encoding="utf-8")


def select_new_items(feed: list[FeedItem], existing: list[SiteEpisode]) -> list[FeedItem]:
    enclosures = {episode.enclosure for episode in existing}
    guids = {episode.guid for episode in existing if episode.guid}
    titles = {episode.title for episode in existing}
    matched = [
        item.published
        for item in feed
        if item.enclosure in enclosures or (item.guid and item.guid in guids)
    ]
    if not matched:
        raise SystemExit("RSS did not match any episode already on the site; not adding anything")
    anchor = max(matched)
    fresh: list[FeedItem] = []
    for item in feed:
        if item.published <= anchor:
            continue
        if not item.enclosure:
            print(f"Skipping {item.title!r}: RSS item has no audio enclosure", file=sys.stderr)
            continue
        if item.enclosure in enclosures or (item.guid and item.guid in guids) or item.title in titles:
            continue
        fresh.append(item)
    fresh.sort(key=lambda item: item.published)
    return fresh


def apply(feed: list[FeedItem], dry_run: bool) -> int:
    existing = read_site_episodes()
    if not existing:
        raise SystemExit("No episode pages found")
    fresh = select_new_items(feed, existing)
    if not fresh:
        print("No new episodes.")
        return 0
    for item in fresh:
        print(f"New: {iso_date(item.published)} {item.title}")
    if dry_run:
        return 0

    previous = existing[-1]
    reference = (ROOT / "ep" / str(previous.number) / "index.html").read_text(encoding="utf-8")
    nav, footer = load_chrome(reference)
    created: list[tuple[int, FeedItem]] = []
    number = previous.number
    prior: SiteEpisode | None = previous
    for offset, item in enumerate(fresh):
        number += 1
        nxt = None
        if offset + 1 < len(fresh):
            nxt = (number + 1, fresh[offset + 1].title)
        page = render_episode_page(item, number, prior, nxt, nav, footer)
        target = ROOT / "ep" / str(number)
        target.mkdir(parents=True, exist_ok=False)
        (target / "index.html").write_text(page, encoding="utf-8")
        created.append((number, item))
        prior = SiteEpisode(number, item.title, item.enclosure, item.guid)
        print(f"Wrote ep/{number}/")

    first_number, first_item = created[0]
    previous_path = ROOT / "ep" / str(previous.number) / "index.html"
    previous_html = previous_path.read_text(encoding="utf-8")
    previous_path.write_text(
        link_next_episode(previous_html, first_number, first_item.title),
        encoding="utf-8",
    )

    index_path = ROOT / "index.html"
    index_html = index_path.read_text(encoding="utf-8")
    kept = existing_homepage_cards(index_html)
    ordered = [episode.number for episode in existing] + [n for n, _ in created]
    top = list(reversed(ordered[-6:]))
    cards: list[str] = []
    for episode_number in top:
        if episode_number in kept:
            cards.append(kept[episode_number])
            continue
        item = next(episode for n, episode in created if n == episode_number)
        cards.append(
            homepage_card(
                episode_number,
                display_date(item.published),
                item.title,
                blurb(item.notes_html),
                item.image,
            )
        )
    index_html = replace_homepage_cards(index_html, cards)
    index_html = point_latest_button(index_html, created[-1][0])
    old_count = re.search(r'<span class="stat-num">(\d+)</span>', index_html)
    if not old_count:
        raise SystemExit("Homepage is missing the episode count")
    new_count = str(len(existing) + len(created))
    index_html = replace_count(
        index_html,
        old_count.group(1),
        new_count,
        [
            "。{n} 期节目，",
            '<span class="stat-num">{n}</span>',
            "查看全部 {n} 期节目",
            '<span class="sp-num">{n}期</span>',
        ],
    )
    index_path.write_text(index_html, encoding="utf-8")

    archive_path = ROOT / "episodes" / "index.html"
    archive_html = archive_path.read_text(encoding="utf-8")
    archive_count = re.search(r"共 (\d+) 期。", archive_html)
    if not archive_count:
        raise SystemExit("Archive page is missing the episode count")
    new_cards = "".join(
        archive_card(
            episode_number,
            display_date(item.published),
            item.title,
            blurb(item.notes_html),
            item.image,
        )
        for episode_number, item in reversed(created)
    )
    marker = '<div class="ep-matrix reveal" id="episodes-grid"> '
    if marker not in archive_html:
        raise SystemExit("Could not find the archive grid")
    archive_html = archive_html.replace(marker, marker + new_cards, 1)
    archive_html = replace_count(
        archive_html,
        archive_count.group(1),
        new_count,
        ["全部 {n} 期节目归档", "共 {n} 期。"],
    )
    archive_path.write_text(archive_html, encoding="utf-8")
    insert_sitemap([episode_number for episode_number, _ in created])
    print(f"Homepage now leads with ep/{created[-1][0]}/ ({created[-1][1].title})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print new episodes without writing")
    parser.add_argument("--rss", type=Path, help="Read RSS from a file instead of the network")
    args = parser.parse_args()
    xml_bytes = args.rss.read_bytes() if args.rss else fetch_rss(RSS_URL)
    return apply(parse_feed(xml_bytes), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
