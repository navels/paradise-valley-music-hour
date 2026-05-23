#!/usr/bin/env python3
"""
Generate a podcast RSS feed from a webpage that contains MP3 links.

Default target:
  http://media.voiceofvashon.org/audio/Paradise/

Usage:
  python3 generate_podcast_rss.py > paradise.xml
  python3 generate_podcast_rss.py --page http://media.voiceofvashon.org/audio/Paradise/ > paradise.xml
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from email.utils import format_datetime
from mutagen.mp3 import MP3
from xml.etree.ElementTree import Element, SubElement, indent, tostring


DEFAULT_PAGE = "http://media.voiceofvashon.org/audio/Paradise/"
DEFAULT_SITE = "https://voiceofvashon.org"
DEFAULT_TITLE = "Paradise Valley Music Hour"
DEFAULT_DESC = "Welcome to the Paradise Valley Music Hour, your gateway to the vibrant sounds of the Pacific Northwest and beyond. Join me for an exclusive showcase of the region’s latest talents alongside timeless classics from well-known artists."
DEFAULT_IMAGE_URL = 'https://navels.github.io/paradise-valley-music-hour/paradise-artwork.jpg'
DEFAULT_AUTHOR = "Lee Nave"
DEFAULT_OWNER_NAME = "Lee Nave"
DEFAULT_OWNER_EMAIL = "spotify-navels@sneakemail.com"
DEFAULT_METADATA_PATH = "audio_metadata.json"

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

def fetch_head(url: str) -> dict[str, str]:
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; podcast-rss-generator/1.0)"
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return dict(resp.headers.items())

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

def format_duration(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def load_metadata_cache(path: str) -> dict[str, dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}

    if not isinstance(data, dict):
        raise ValueError(f"Expected object at {path}")

    cache: dict[str, dict[str, str]] = {}
    for url, value in data.items():
        if isinstance(url, str) and isinstance(value, dict):
            cache[url] = {
                k: str(v) for k, v in value.items() if k in {"duration", "length"}
            }
    return cache

def save_metadata_cache(path: str, cache: dict[str, dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2, sort_keys=True)
        f.write("\n")

def get_enclosure_length(mp3_url: str) -> str | None:
    try:
        headers = fetch_head(mp3_url)
    except Exception:
        return None

    length = headers.get("Content-Length")
    if not length:
        return None

    try:
        return str(int(length))
    except ValueError:
        return None

def download_file(url: str) -> tuple[str, int]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; podcast-rss-generator/1.0)"
        },
    )
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        with urllib.request.urlopen(req, timeout=60) as resp:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        return tmp.name, tmp.tell()

def get_itunes_duration(local_mp3_path: str) -> str:
    audio = MP3(local_mp3_path)
    return format_duration(round(audio.info.length))

def get_episode_metadata(
    mp3_url: str,
    metadata_cache: dict[str, dict[str, str]],
) -> dict[str, str]:
    cached = metadata_cache.get(mp3_url)
    if cached and cached.get("duration") and cached.get("length"):
        return cached

    local_path = None
    try:
        local_path, file_size = download_file(mp3_url)
        metadata = {
            "duration": get_itunes_duration(local_path),
            "length": get_enclosure_length(mp3_url) or str(file_size),
        }
    finally:
        if local_path and os.path.exists(local_path):
            os.unlink(local_path)

    metadata_cache[mp3_url] = metadata
    return metadata

def build_rss(
    mp3_urls: list[str],
    feed_title: str,
    feed_desc: str,
    site_url: str,
    image_url: str | None,
    author: str,
    owner_name: str,
    owner_email: str,
    metadata_cache: dict[str, dict[str, str]],
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

    latest_pub_dt = next(
        (
            pub_dt
            for pub_dt, _ in items
            if pub_dt.year != 1970
        ),
        dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc),
    )

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
    SubElement(channel, "lastBuildDate").text = format_datetime(latest_pub_dt)

    # iTunes-ish extras (safe even if you don't care)
    SubElement(channel, "itunes:author").text = author
    owner = SubElement(channel, "itunes:owner")
    SubElement(owner, "itunes:name").text = owner_name
    SubElement(owner, "itunes:email").text = owner_email
    SubElement(channel, "itunes:explicit").text = "no"
    SubElement(channel, "itunes:type").text = "episodic"
    if image_url:
        it_img = SubElement(channel, "itunes:image")
        it_img.set("href", image_url)

    for pub_dt, url in items:
        item = SubElement(channel, "item")
        metadata = get_episode_metadata(url, metadata_cache)

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
        if metadata.get("length"):
            enc.set("length", metadata["length"])

        SubElement(item, "itunes:episode").text = str(ep)
        SubElement(item, "itunes:episodeType").text = "full"
        if metadata.get("duration"):
            SubElement(item, "itunes:duration").text = metadata["duration"]

    indent(rss, space="  ")
    xml_bytes = tostring(rss, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default=DEFAULT_PAGE, help="Web page URL that contains MP3 links")
    ap.add_argument("--site", default=DEFAULT_SITE, help="Channel <link> value (home/site URL)")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="Podcast title")
    ap.add_argument("--desc", default=DEFAULT_DESC, help="Podcast description")
    ap.add_argument("--image", default=DEFAULT_IMAGE_URL, help="Optional artwork image URL")
    ap.add_argument("--author", default=DEFAULT_AUTHOR, help="iTunes author")
    ap.add_argument("--owner-name", default=DEFAULT_OWNER_NAME, help="iTunes owner name")
    ap.add_argument("--owner-email", default=DEFAULT_OWNER_EMAIL, help="iTunes owner email")
    ap.add_argument("--metadata", default=DEFAULT_METADATA_PATH, help="Path to cached audio metadata JSON")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of episodes in the feed")
    args = ap.parse_args()

    page_html = fetch(args.page)
    mp3_urls = extract_mp3_urls(page_html, base_url=args.page)

    if not mp3_urls:
        print(f"ERROR: No .mp3 links found on {args.page}", file=sys.stderr)
        return 2

    metadata_cache = load_metadata_cache(args.metadata)

    rss_xml = build_rss(
        mp3_urls=mp3_urls,
        feed_title=args.title,
        feed_desc=args.desc,
        site_url=args.site,
        image_url=args.image,
        author=args.author,
        owner_name=args.owner_name,
        owner_email=args.owner_email,
        metadata_cache=metadata_cache,
        limit=args.limit,
    )

    save_metadata_cache(args.metadata, metadata_cache)
    sys.stdout.write(rss_xml)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
