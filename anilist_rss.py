#!/usr/bin/env python3
"""
AniList airing RSS feed generator.

Generates an RSS feed of anime episodes as they air, using AniList's GraphQL API.
No third-party dependencies (Python 3.8+).

Examples:
  # Static file for your Watching list (run from cron every ~30 min)
  python anilist_rss.py --user YourAniListName --out airing.xml

  # Serve it over HTTP at http://localhost:8080/feed.xml
  python anilist_rss.py --user YourAniListName --serve 8080

  # Include upcoming episodes (next 7 days) as well as already-aired ones
  python anilist_rss.py --user YourAniListName --upcoming 7 --out airing.xml
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from xml.sax.saxutils import escape

API_URL = "https://graphql.anilist.co"

USER_LIST_QUERY = """
query ($user: String, $statuses: [MediaListStatus]) {
  MediaListCollection(userName: $user, type: ANIME, status_in: $statuses) {
    lists { entries { mediaId } }
  }
}
"""

SCHEDULE_QUERY = """
query ($page: Int, $from: Int, $to: Int, $ids: [Int]) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    airingSchedules(airingAt_greater: $from, airingAt_lesser: $to,
                    mediaId_in: $ids, sort: TIME) {
      id
      airingAt
      episode
      media {
        id
        siteUrl
        isAdult
        format
        episodes
        title { romaji english }
        coverImage { large }
      }
    }
  }
}
"""


def gql(query, variables, retries=3):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "anilist-rss/1.0",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            if "errors" in data:
                raise RuntimeError(data["errors"][0].get("message", "AniList error"))
            return data["data"]
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                time.sleep(int(e.headers.get("Retry-After", 30)))
                continue
            if e.code == 404:
                raise RuntimeError("AniList user not found (is the list public?)")
            raise
    raise RuntimeError("AniList rate limit: giving up after retries")


def get_user_media_ids(user, statuses):
    data = gql(USER_LIST_QUERY, {"user": user, "statuses": statuses})
    ids = set()
    for lst in data["MediaListCollection"]["lists"]:
        for entry in lst["entries"]:
            ids.add(entry["mediaId"])
    return sorted(ids)


def fetch_schedule(time_from, time_to, media_ids=None):
    items, page = [], 1
    while True:
        variables = {"page": page, "from": time_from, "to": time_to}
        if media_ids is not None:
            variables["ids"] = media_ids
        data = gql(SCHEDULE_QUERY, variables)["Page"]
        items.extend(data["airingSchedules"])
        if not data["pageInfo"]["hasNextPage"]:
            break
        page += 1
    return items


def build_rss(schedules, title_pref, user, allow_adult, feed_url=""):
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    entries = []

    for s in sorted(schedules, key=lambda x: x["airingAt"], reverse=True):
        m = s["media"]
        if m.get("isAdult") and not allow_adult:
            continue
        t = m["title"]
        name = (t.get(title_pref) or t.get("romaji") or t.get("english") or "Unknown")
        ep = s["episode"]
        total = m.get("episodes")
        aired = s["airingAt"] <= now_ts
        airs_dt = datetime.fromtimestamp(s["airingAt"], tz=timezone.utc)

        status = "aired" if aired else "airs"
        suffix = " (FINAL)" if total and ep == total else ""
        item_title = f"{name} – Episode {ep}{suffix} {status}"

        cover = (m.get("coverImage") or {}).get("large", "")
        img = f'<img src="{escape(cover)}" alt=""/><br/>' if cover else ""
        desc = (
            f"{img}<b>{escape(name)}</b> episode {ep}"
            f"{f' of {total}' if total else ''} "
            f"{'aired' if aired else 'airs'} at "
            f"{airs_dt.strftime('%Y-%m-%d %H:%M UTC')}."
        )

        entries.append(
            f"""    <item>
      <title>{escape(item_title)}</title>
      <link>{escape(m['siteUrl'])}</link>
      <guid isPermaLink="false">anilist-airing-{s['id']}</guid>
      <pubDate>{format_datetime(airs_dt)}</pubDate>
      <description>{escape(desc)}</description>
    </item>"""
        )

    feed_title = "AniList Airing"
    link = feed_url or "https://anilist.co"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape(feed_title)}</title>
    <link>{escape(link)}</link>
    <description>New anime episodes as they air, from AniList</description>
    <lastBuildDate>{format_datetime(now)}</lastBuildDate>
    <ttl>30</ttl>
{chr(10).join(entries)}
  </channel>
</rss>
"""


def generate(args):
    now = int(time.time())
    time_from = now - int(args.days * 86400)
    time_to = now + int(args.upcoming * 86400)

    # Only ever show anime that are on the user's list
    media_ids = get_user_media_ids(args.user, args.statuses)
    print(f"Found {len(media_ids)} anime on your list "
          f"with status {', '.join(args.statuses)}", file=sys.stderr)
    if not media_ids:
        print("Nothing on those lists. Check the username, that your list is "
              "public, and try --statuses CURRENT PLANNING.", file=sys.stderr)
        return build_rss([], args.title, args.user, args.adult)

    schedules = fetch_schedule(time_from, time_to, media_ids)
    print(f"Found {len(schedules)} episodes in the time window "
          f"({args.days} days back, {args.upcoming} days ahead)", file=sys.stderr)
    return build_rss(schedules, args.title, args.user, args.adult)


def serve(args):
    cache = {"ts": 0, "body": b""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/", "/feed.xml"):
                self.send_error(404)
                return
            try:
                if time.time() - cache["ts"] > args.cache * 60:
                    cache["body"] = generate(args).encode("utf-8")
                    cache["ts"] = time.time()
            except Exception as e:
                self.send_error(502, f"AniList error: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(cache["body"])))
            self.end_headers()
            self.wfile.write(cache["body"])

        def log_message(self, *a):
            pass

    print(f"Serving feed at http://localhost:{args.serve}/feed.xml (Ctrl+C to stop)")
    HTTPServer(("0.0.0.0", args.serve), Handler).serve_forever()


def main():
    p = argparse.ArgumentParser(description="AniList airing RSS feed generator")
    p.add_argument("--user", required=True,
                   help="Your AniList username (list must be public); feed only shows anime on your list")
    p.add_argument("--statuses", nargs="+", default=["CURRENT"],
                   choices=["CURRENT", "PLANNING", "COMPLETED", "DROPPED", "PAUSED", "REPEATING"],
                   help="List statuses to include (default: CURRENT)")
    p.add_argument("--days", type=float, default=2, help="Days of past episodes to include (default 2)")
    p.add_argument("--upcoming", type=float, default=0, help="Days of future episodes to include (default 0)")
    p.add_argument("--title", choices=["romaji", "english"], default="romaji", help="Title language")
    p.add_argument("--adult", action="store_true", help="Include adult titles")
    p.add_argument("--out", default="anilist_airing.xml", help="Output file (static mode)")
    p.add_argument("--serve", type=int, metavar="PORT", help="Serve over HTTP instead of writing a file")
    p.add_argument("--cache", type=int, default=15, help="Minutes to cache in serve mode (default 15)")
    args = p.parse_args()

    try:
        if args.serve:
            serve(args)
        else:
            xml = generate(args)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(xml)
            print(f"Wrote {args.out}")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
