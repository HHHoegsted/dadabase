import json
import os
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SUBREDDIT = os.getenv("SUBREDDIT", "fadervittigheder").strip().lstrip("r/")
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "100"))
FETCH_INTERVAL_SECONDS = int(os.getenv("FETCH_INTERVAL_SECONDS", str(24 * 60 * 60)))
PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "reddit_posts.sqlite3"
USER_AGENT = os.getenv(
    "USER_AGENT",
    "dada-base-reddit-scraper/1.0 (local homelab service)",
)


shutdown_event = threading.Event()
state_lock = threading.Lock()
last_run = {
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "inserted": 0,
    "updated": 0,
    "error": None,
}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_from_timestamp(value):
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat()


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with connect() as db:
        db.execute(
            """
            create table if not exists posts (
                id text primary key,
                subreddit text not null,
                title text not null,
                author text,
                selftext text,
                url text,
                permalink text not null,
                score integer,
                num_comments integer,
                over_18 integer not null default 0,
                created_utc text,
                fetched_at text not null,
                raw_json text not null
            )
            """
        )
        db.execute(
            "create index if not exists idx_posts_created_utc on posts(created_utc desc)"
        )


def reddit_listing_url():
    return f"https://www.reddit.com/r/{SUBREDDIT}.json?limit={FETCH_LIMIT}"


def fetch_reddit_listing():
    request = urllib.request.Request(
        reddit_listing_url(),
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def upsert_posts(listing):
    fetched_at = now_iso()
    inserted = 0
    updated = 0
    children = listing.get("data", {}).get("children", [])

    with connect() as db:
        for child in children:
            post = child.get("data", {})
            post_id = post.get("id")
            permalink = post.get("permalink")
            title = post.get("title")
            if not post_id or not permalink or not title:
                continue

            exists = db.execute("select 1 from posts where id = ?", (post_id,)).fetchone()
            db.execute(
                """
                insert into posts (
                    id, subreddit, title, author, selftext, url, permalink, score,
                    num_comments, over_18, created_utc, fetched_at, raw_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    title = excluded.title,
                    author = excluded.author,
                    selftext = excluded.selftext,
                    url = excluded.url,
                    permalink = excluded.permalink,
                    score = excluded.score,
                    num_comments = excluded.num_comments,
                    over_18 = excluded.over_18,
                    fetched_at = excluded.fetched_at,
                    raw_json = excluded.raw_json
                """,
                (
                    post_id,
                    post.get("subreddit") or SUBREDDIT,
                    title,
                    post.get("author"),
                    post.get("selftext"),
                    post.get("url"),
                    permalink,
                    post.get("score"),
                    post.get("num_comments"),
                    1 if post.get("over_18") else 0,
                    utc_from_timestamp(post.get("created_utc")),
                    fetched_at,
                    json.dumps(post, ensure_ascii=False),
                ),
            )
            if exists:
                updated += 1
            else:
                inserted += 1

    return inserted, updated


def run_fetch():
    started_at = now_iso()
    with state_lock:
        last_run.update(
            {
                "started_at": started_at,
                "finished_at": None,
                "ok": None,
                "inserted": 0,
                "updated": 0,
                "error": None,
            }
        )

    try:
        listing = fetch_reddit_listing()
        inserted, updated = upsert_posts(listing)
        with state_lock:
            last_run.update(
                {
                    "finished_at": now_iso(),
                    "ok": True,
                    "inserted": inserted,
                    "updated": updated,
                    "error": None,
                }
            )
        print(f"Fetched r/{SUBREDDIT}: inserted={inserted} updated={updated}", flush=True)
    except Exception as exc:
        with state_lock:
            last_run.update(
                {
                    "finished_at": now_iso(),
                    "ok": False,
                    "inserted": 0,
                    "updated": 0,
                    "error": str(exc),
                }
            )
        print(f"Fetch failed: {exc}", flush=True)


def scheduler():
    run_fetch()
    while not shutdown_event.wait(FETCH_INTERVAL_SECONDS):
        run_fetch()


def post_rows(limit=50):
    with connect() as db:
        rows = db.execute(
            """
            select id, subreddit, title, author, selftext, url, permalink, score,
                   num_comments, over_18, created_utc, fetched_at
            from posts
            order by created_utc desc, fetched_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def status_payload():
    with state_lock:
        run = dict(last_run)
    return {
        "subreddit": f"r/{SUBREDDIT}",
        "source_url": reddit_listing_url(),
        "fetch_interval_seconds": FETCH_INTERVAL_SECONDS,
        "database": str(DB_PATH),
        "last_run": run,
    }


def json_response(handler, payload, status=HTTPStatus.OK):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_escape(value):
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def index_html():
    posts = post_rows()
    status = status_payload()
    cards = []
    for post in posts:
        reddit_url = f"https://www.reddit.com{post['permalink']}"
        selftext = html_escape(post.get("selftext"))
        excerpt = f"<p>{selftext[:500]}</p>" if selftext else ""
        cards.append(
            f"""
            <article>
              <h2><a href="{html_escape(reddit_url)}">{html_escape(post["title"])}</a></h2>
              {excerpt}
              <footer>
                {html_escape(post.get("created_utc"))} · score {post.get("score") or 0} ·
                {post.get("num_comments") or 0} comments · u/{html_escape(post.get("author"))}
              </footer>
            </article>
            """
        )

    last_run_text = json.dumps(status["last_run"], ensure_ascii=False)
    content = "\n".join(cards) if cards else "<p>No posts have been fetched yet.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>r/{html_escape(SUBREDDIT)} scraper</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: Canvas;
      color: CanvasText;
    }}
    body {{
      max-width: 920px;
      margin: 0 auto;
      padding: 32px 20px 56px;
      line-height: 1.5;
    }}
    header {{
      border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
      margin-bottom: 24px;
      padding-bottom: 20px;
    }}
    h1 {{
      font-size: clamp(1.8rem, 4vw, 3rem);
      margin: 0 0 8px;
    }}
    nav a {{
      margin-right: 16px;
    }}
    article {{
      border-bottom: 1px solid color-mix(in srgb, CanvasText 14%, transparent);
      padding: 18px 0;
    }}
    h2 {{
      font-size: 1.15rem;
      margin: 0 0 8px;
    }}
    a {{
      color: LinkText;
    }}
    footer, .status {{
      color: color-mix(in srgb, CanvasText 68%, transparent);
      font-size: 0.9rem;
    }}
    code {{
      overflow-wrap: anywhere;
    }}
  </style>
</head>
<body>
  <header>
    <h1>r/{html_escape(SUBREDDIT)}</h1>
    <p class="status">Source: <code>{html_escape(status["source_url"])}</code></p>
    <p class="status">Last run: <code>{html_escape(last_run_text)}</code></p>
    <nav>
      <a href="/api/posts">Posts JSON</a>
      <a href="/api/status">Status JSON</a>
      <a href="/api/refresh">Refresh now</a>
    </nav>
  </header>
  <main>{content}</main>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = index_html().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/status":
            json_response(self, status_payload())
            return

        if parsed.path == "/api/posts":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["50"])[0])
            json_response(self, post_rows(max(1, min(limit, 200))))
            return

        if parsed.path == "/api/refresh":
            threading.Thread(target=run_fetch, daemon=True).start()
            json_response(self, {"ok": True, "message": "Refresh started"})
            return

        json_response(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)


def stop_server(signum, frame):
    shutdown_event.set()


def main():
    init_db()
    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)

    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("", PORT), Handler)
    server.timeout = 1
    print(f"Serving r/{SUBREDDIT} scraper on http://localhost:{PORT}", flush=True)

    while not shutdown_event.is_set():
        server.handle_request()
    server.server_close()


if __name__ == "__main__":
    main()
