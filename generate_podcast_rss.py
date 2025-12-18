#!/usr/bin/env python3
"""
Generate a podcast RSS feed from a webpage that contains MP3 links.

Default target:
  https://voiceofvashon.org/show/paradise-valley-music-hour/

Usage:
  python3 generate_podcast_rss.py > paradise.xml
  python3 generate_podcast_rss.py --page https://voiceofvashon.org/audio/Paradise/ > paradise.xml
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
import urllib.parse
import urllib.request
from email.utils import format_datetime
from xml.etree.ElementTree import Element, SubElement, tostring


DEFAULT_PAGE = "https://voiceofvashon.org/audio/Paradise/"
DEFAULT_SITE = "https://voiceofvashon.org"
DEFAULT_TITLE = "Paradise Valley Music Hour"
DEFAULT_DESC = "Welcome to the Paradise Valley Music Hour, your gateway to the vibrant sounds of the Pacific Northwest and beyond. Join me for an exclusive showcase of the region’s latest talents alongside timeless classics from well-known artists."
DEFAULT_IMAGE_URL = 'https://navels.github.io/paradise-valley-music-hour/paradise-artwork.jpg'

MP3_RE = re.compile(r'href=["\']([^"\']+\.mp3)["\']', re.IGNORECASE)

DATE_IN_NAME_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')

EP_RE = re.compile(
    r'(?P<date>\d{4}-\d{2}-\d{2}).*?Ep(?P<ep>\d+)',
    re.IGNORECASE,
)

def parse_episode_info(mp3_url: str):
    filename = urllib.parse.urlparse(mp3_url).path.rsplit("/", 1)[-1]
    name = filename.rsplit(".", 1)[0]

    m = EP_RE.search(name)
    if not m:
        return None

    date = dt.date.fromisoformat(m.group("date"))
    ep = int(m.group("ep"))
    return date, ep

def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; podcast-rss-generator/1.0)"
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")

def absolutize(url: str, base: str) -> str:
    return urllib.parse.urljoin(base, url)

def extract_mp3_urls(page_html: str, base_url: str) -> list[str]:
    urls = []
    seen = set()

    for m in MP3_RE.finditer(page_html):
        raw = html.unescape(m.group(1))
        abs_url = absolutize(raw, base_url)

        # Basic sanity check: keep only http(s)
        if not abs_url.startswith(("http://", "https://")):
            continue

        if abs_url not in seen:
            seen.add(abs_url)
            urls.append(abs_url)

    return urls

def guess_pubdate_from_url(mp3_url: str) -> dt.datetime | None:
    """
    Attempts to find YYYY-MM-DD in the filename and use it as pubDate (UTC midnight).
    """
    path = urllib.parse.urlparse(mp3_url).path
    filename = path.rsplit("/", 1)[-1]
    m = DATE_IN_NAME_RE.search(filename)
    if not m:
        return None
    try:
        d = dt.date.fromisoformat(m.group(1))
        return dt.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=dt.timezone.utc)
    except ValueError:
        return None

def human_title_from_url(mp3_url: str) -> str:
    path = urllib.parse.urlparse(mp3_url).path
    filename = path.rsplit("/", 1)[-1]
    name = filename.rsplit(".", 1)[0]

    # Light cleanup: underscores to spaces, collapse spaces
    name = name.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name

def build_rss(
    mp3_urls: list[str],
    feed_title: str,
    feed_desc: str,
    site_url: str,
    image_url: str | None,
    limit: int | None,
) -> str:
    # Sort newest-first by guessed date, then by URL
    items = []
    for url in mp3_urls:
        pub = guess_pubdate_from_url(url)
        items.append((pub or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc), url))

    items.sort(key=lambda t: (t[0], t[1]), reverse=True)

    if limit is not None:
        items = items[:limit]

    rss = Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = feed_title
    SubElement(channel, "link").text = site_url
    SubElement(channel, "description").text = feed_desc
    SubElement(channel, "language").text = "en-us"
    SubElement(channel, "generator").text = "generate_podcast_rss.py"
    SubElement(channel, "lastBuildDate").text = format_datetime(dt.datetime.now(dt.timezone.utc))

    # iTunes-ish extras (safe even if you don't care)
    SubElement(channel, "itunes:explicit").text = "no"
    SubElement(channel, "itunes:type").text = "episodic"
    if image_url:
        it_img = SubElement(channel, "itunes:image")
        it_img.set("href", image_url)

    for pub_dt, url in items:
        item = SubElement(channel, "item")

        info = parse_episode_info(url)
        
        if info:
            date, ep = info
            title = f"Ep. {ep} — {date.strftime('%b %d, %Y')}"
            description = (
                "Paradise Valley Music Hour.\n"
                f"Originally aired {date.strftime('%B %d, %Y')} on Voice of Vashon."
            )
        else:
            title = human_title_from_url(url)
            description = f"Audio: {url}"

        SubElement(item, "title").text = title
        SubElement(item, "link").text = url
        SubElement(item, "guid").text = url
        SubElement(item, "description").text = description

        if pub_dt.year != 1970:
            SubElement(item, "pubDate").text = format_datetime(pub_dt)

        # enclosure requires url + type; length can be omitted if unknown
        enc = SubElement(item, "enclosure")
        enc.set("url", url)
        enc.set("type", "audio/mpeg")

        SubElement(item, "itunes:episode").text = str(ep)
        SubElement(item, "itunes:episodeType").text = "full"

    xml_bytes = tostring(rss, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default=DEFAULT_PAGE, help="Web page URL that contains MP3 links")
    ap.add_argument("--site", default=DEFAULT_SITE, help="Channel <link> value (home/site URL)")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="Podcast title")
    ap.add_argument("--desc", default=DEFAULT_DESC, help="Podcast description")
    ap.add_argument("--image", default=DEFAULT_IMAGE_URL, help="Optional artwork image URL")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of episodes in the feed")
    args = ap.parse_args()

    page_html = fetch(args.page)
    mp3_urls = extract_mp3_urls(page_html, base_url=args.page)

    if not mp3_urls:
        print(f"ERROR: No .mp3 links found on {args.page}", file=sys.stderr)
        return 2

    rss_xml = build_rss(
        mp3_urls=mp3_urls,
        feed_title=args.title,
        feed_desc=args.desc,
        site_url=args.site,
        image_url=args.image,
        limit=args.limit,
    )

    sys.stdout.write(rss_xml)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
