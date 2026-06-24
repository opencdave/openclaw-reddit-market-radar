#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP = "reddit-market-radar"
ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".openclaw" / APP
DB_PATH = STATE_DIR / "radar.sqlite3"
ENV_PATH = Path.home() / ".openclaw" / "reddit-market-radar.env"

DEFAULT_SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "SecurityAnalysis",
    "ValueInvesting",
    "options",
    "shortsqueeze",
    "pennystocks",
    "biotechplays",
    "SPACs",
]

STOPWORDS = {
    "A", "AI", "ALL", "AM", "AN", "ARE", "ATH", "ATM", "BE", "BIG", "BUY", "BY", "CEO",
    "CFO", "DD", "DIY", "DO", "DTE", "EPS", "ETF", "EU", "EV", "FDA", "FED", "FOMO",
    "FOR", "GDP", "GO", "IPO", "IRA", "IRS", "IT", "IV", "JOB", "LOL", "MACD", "ME",
    "MOM", "NAV", "NEW", "NO", "NOT", "NOW", "NYSE", "OF", "ON", "ONE", "OR", "OTC",
    "PE", "PEG", "PM", "PPI", "PR", "CEO", "PUT", "QE", "QQQ", "RH", "SEC", "SELL",
    "TA", "THE", "TO", "USA", "USD", "US", "VC", "YOLO", "YOU",
}

TICKER_RE = re.compile(r"(?<![A-Za-z])\$?([A-Z]{1,5})(?![A-Za-z])")


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    user_agent: str
    device_id: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> Config:
    load_env_file()
    return Config(
        client_id=os.environ.get("REDDIT_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("REDDIT_CLIENT_SECRET", "").strip(),
        user_agent=os.environ.get(
            "REDDIT_USER_AGENT",
            "OpenClawMarketRadar/0.1 by local-user",
        ).strip(),
        device_id=os.environ.get("REDDIT_DEVICE_ID", "DO_NOT_TRACK_THIS_DEVICE").strip(),
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists runs (
            id integer primary key autoincrement,
            started_at text not null,
            finished_at text,
            subreddits text not null,
            sorts text not null,
            post_count integer not null default 0,
            mention_count integer not null default 0,
            error text
        );

        create table if not exists posts (
            id text primary key,
            subreddit text not null,
            title text not null,
            author text,
            created_utc real,
            score integer,
            num_comments integer,
            url text,
            permalink text,
            selftext text,
            fetched_at text not null
        );

        create table if not exists mentions (
            run_id integer not null,
            ticker text not null,
            post_id text not null,
            subreddit text not null,
            weight real not null,
            snippet text not null,
            primary key (run_id, ticker, post_id),
            foreign key(run_id) references runs(id),
            foreign key(post_id) references posts(id)
        );

        create index if not exists idx_mentions_ticker on mentions(ticker);
        create index if not exists idx_mentions_run on mentions(run_id);
        create index if not exists idx_posts_subreddit on posts(subreddit);
        """
    )
    conn.commit()


def http_json(url: str, headers: dict[str, str], data: bytes | None = None, timeout: float = 20) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"{type(exc).__name__} from {url}: {exc}") from exc


def reddit_token(config: Config) -> str:
    if not config.client_id:
        raise RuntimeError(f"Missing REDDIT_CLIENT_ID in {ENV_PATH}")

    if config.client_secret:
        credentials = f"{config.client_id}:{config.client_secret}".encode("utf-8")
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
    else:
        credentials = f"{config.client_id}:".encode("utf-8")
        body = urllib.parse.urlencode(
            {
                "grant_type": "https://oauth.reddit.com/grants/installed_client",
                "device_id": config.device_id,
            }
        ).encode("utf-8")

    payload = http_json(
        "https://www.reddit.com/api/v1/access_token",
        headers={
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": config.user_agent,
        },
        data=body,
        timeout=15,
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Reddit OAuth did not return an access_token: {payload}")
    return token


def reddit_listing(token: str, config: Config, subreddit: str, sort: str, limit: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"limit": limit, "raw_json": 1})
    url = f"https://oauth.reddit.com/r/{subreddit}/{sort}?{query}"
    payload = http_json(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": config.user_agent,
            "Accept": "application/json",
        },
        timeout=20,
    )
    return [item["data"] for item in payload.get("data", {}).get("children", []) if item.get("kind") == "t3"]


def clean_snippet(text: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[: limit - 1] + "…" if len(compact) > limit else compact


def extract_tickers(title: str, selftext: str) -> set[str]:
    haystacks = [title, selftext[:2000]]
    found: set[str] = set()
    for text in haystacks:
        for match in TICKER_RE.finditer(text or ""):
            ticker = match.group(1).upper()
            if ticker in STOPWORDS:
                continue
            if len(ticker) == 1 and f"${ticker}" not in text:
                continue
            found.add(ticker)
    return found


def mention_weight(post: dict[str, Any], ticker: str) -> float:
    title = post.get("title") or ""
    score = max(0, int(post.get("score") or 0))
    comments = max(0, int(post.get("num_comments") or 0))
    title_bonus = 1.2 if re.search(rf"(?<![A-Za-z])\$?{re.escape(ticker)}(?![A-Za-z])", title) else 0.0
    return round(1.0 + title_bonus + math.log10(score + 1) * 0.55 + math.log10(comments + 1) * 0.65, 3)


def upsert_post(conn: sqlite3.Connection, post: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into posts (
            id, subreddit, title, author, created_utc, score, num_comments, url, permalink, selftext, fetched_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
            subreddit=excluded.subreddit,
            title=excluded.title,
            author=excluded.author,
            created_utc=excluded.created_utc,
            score=excluded.score,
            num_comments=excluded.num_comments,
            url=excluded.url,
            permalink=excluded.permalink,
            selftext=excluded.selftext,
            fetched_at=excluded.fetched_at
        """,
        (
            post.get("id"),
            post.get("subreddit"),
            post.get("title") or "",
            post.get("author"),
            post.get("created_utc"),
            post.get("score"),
            post.get("num_comments"),
            post.get("url"),
            "https://www.reddit.com" + (post.get("permalink") or ""),
            post.get("selftext") or "",
            now_iso(),
        ),
    )


def run_scan(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    subreddits = [s.strip().lstrip("r/") for s in args.subreddits.split(",") if s.strip()]
    sorts = [s.strip() for s in args.sorts.split(",") if s.strip()]
    started = now_iso()
    cursor = conn.execute(
        "insert into runs(started_at, subreddits, sorts) values (?, ?, ?)",
        (started, ",".join(subreddits), ",".join(sorts)),
    )
    run_id = int(cursor.lastrowid)
    post_count = 0
    mention_count = 0
    error = None

    try:
        token = reddit_token(config)
        seen_posts: set[str] = set()
        for subreddit in subreddits:
            for sort in sorts:
                posts = reddit_listing(token, config, subreddit, sort, args.limit)
                time.sleep(args.sleep)
                for post in posts:
                    post_id = post.get("id")
                    if not post_id or post_id in seen_posts:
                        continue
                    seen_posts.add(post_id)
                    upsert_post(conn, post)
                    post_count += 1
                    text = f"{post.get('title') or ''}\n{post.get('selftext') or ''}"
                    tickers = extract_tickers(post.get("title") or "", post.get("selftext") or "")
                    for ticker in sorted(tickers):
                        weight = mention_weight(post, ticker)
                        conn.execute(
                            """
                            insert or ignore into mentions(run_id, ticker, post_id, subreddit, weight, snippet)
                            values (?, ?, ?, ?, ?, ?)
                            """,
                            (run_id, ticker, post_id, post.get("subreddit"), weight, clean_snippet(text)),
                        )
                        mention_count += 1
        conn.execute(
            "update runs set finished_at=?, post_count=?, mention_count=? where id=?",
            (now_iso(), post_count, mention_count, run_id),
        )
        conn.commit()
    except Exception as exc:
        error = str(exc)
        conn.execute("update runs set finished_at=?, error=? where id=?", (now_iso(), error, run_id))
        conn.commit()

    report = build_report(conn, run_id, top=args.top)
    report["error"] = error
    report["db"] = str(DB_PATH)
    report["run_id"] = run_id
    report["post_count"] = post_count
    report["mention_count"] = mention_count
    write_report(report)
    return report


def build_report(conn: sqlite3.Connection, run_id: int, top: int = 15) -> dict[str, Any]:
    current = conn.execute(
        """
        select ticker, count(*) as mentions, round(sum(weight), 3) as weight
        from mentions
        where run_id=?
        group by ticker
        order by weight desc, mentions desc
        """,
        (run_id,),
    ).fetchall()

    previous_runs = [
        row[0]
        for row in conn.execute(
            "select id from runs where id < ? and error is null order by id desc limit 30",
            (run_id,),
        ).fetchall()
    ]

    history: dict[str, tuple[float, int]] = {}
    if previous_runs:
        placeholders = ",".join("?" for _ in previous_runs)
        rows = conn.execute(
            f"""
            select ticker, avg(weight), max(weight)
            from (
              select run_id, ticker, sum(weight) as weight
              from mentions
              where run_id in ({placeholders})
              group by run_id, ticker
            )
            group by ticker
            """,
            previous_runs,
        ).fetchall()
        history = {ticker: (avg or 0.0, max_seen or 0.0) for ticker, avg, max_seen in rows}

    entries = []
    for ticker, mentions, weight in current:
        avg, max_seen = history.get(ticker, (0.0, 0.0))
        spike = round(float(weight) / max(1.0, float(avg)), 2)
        novelty = ticker not in history
        posts = conn.execute(
            """
            select p.subreddit, p.title, p.score, p.num_comments, p.permalink, m.snippet, m.weight
            from mentions m
            join posts p on p.id=m.post_id
            where m.run_id=? and m.ticker=?
            order by m.weight desc
            limit 3
            """,
            (run_id, ticker),
        ).fetchall()
        entries.append(
            {
                "ticker": ticker,
                "mentions": int(mentions),
                "weight": float(weight or 0),
                "baseline_avg_weight": round(float(avg), 3),
                "baseline_max_weight": round(float(max_seen), 3),
                "spike": spike,
                "novel": novelty,
                "posts": [
                    {
                        "subreddit": subreddit,
                        "title": title,
                        "score": score,
                        "comments": comments,
                        "url": permalink,
                        "snippet": snippet,
                        "weight": post_weight,
                    }
                    for subreddit, title, score, comments, permalink, snippet, post_weight in posts
                ],
            }
        )

    entries.sort(key=lambda item: (item["novel"], item["spike"], item["weight"]), reverse=True)
    return {
        "generated_at": now_iso(),
        "top": entries[:top],
    }


def format_report(report: dict[str, Any], max_chars: int = 6000) -> str:
    lines = [f"Reddit Market Radar — {report['generated_at']}"]
    if report.get("error"):
        lines += [
            "",
            "ERROR",
            report["error"],
            "",
            f"Config file: {ENV_PATH}",
            "Create a Reddit app and set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET to enable scans.",
        ]
        return "\n".join(lines)[:max_chars]

    if not report.get("top"):
        lines.append("")
        lines.append("No ticker mentions found in this run.")
        return "\n".join(lines)

    for index, item in enumerate(report["top"], start=1):
        flag = "NEW" if item["novel"] else f"{item['spike']}x"
        lines.append("")
        lines.append(
            f"{index}. {item['ticker']} — {item['mentions']} mentions, weight {item['weight']:.1f}, signal {flag}"
        )
        if item["baseline_avg_weight"]:
            lines.append(f"   baseline avg/max weight: {item['baseline_avg_weight']}/{item['baseline_max_weight']}")
        for post in item["posts"]:
            lines.append(
                f"   - r/{post['subreddit']}: {post['title']} "
                f"(score {post['score']}, comments {post['comments']})"
            )
            lines.append(f"     {post['url']}")

    lines += [
        "",
        "Reminder: this is attention radar, not a buy/sell signal. Verify catalysts, float, filings, liquidity, and price action separately.",
    ]
    text = "\n".join(lines)
    return text[: max_chars - 20] + "\n…[truncated]" if len(text) > max_chars else text


def write_report(report: dict[str, Any]) -> None:
    reports = STATE_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (reports / f"{stamp}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (reports / f"{stamp}.txt").write_text(format_report(report, max_chars=12000), encoding="utf-8")
    (STATE_DIR / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (STATE_DIR / "latest.txt").write_text(format_report(report, max_chars=12000), encoding="utf-8")


def init_env() -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ENV_PATH.exists():
        print(f"Exists: {ENV_PATH}")
        return
    ENV_PATH.write_text(
        "\n".join(
            [
                "# Reddit Market Radar credentials",
                "# Create a Reddit app at https://www.reddit.com/prefs/apps",
                "# Use a script/web app for client_credentials, then fill these in.",
                "REDDIT_CLIENT_ID=",
                "REDDIT_CLIENT_SECRET=",
                "REDDIT_USER_AGENT=OpenClawMarketRadar/0.1 by u/your_reddit_username",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ENV_PATH.chmod(0o600)
    print(f"Created: {ENV_PATH}")


def check_config() -> int:
    config = load_config()
    missing: list[str] = []
    warnings: list[str] = []

    if not config.client_id:
        missing.append("REDDIT_CLIENT_ID")
    if not config.user_agent or "your_reddit_username" in config.user_agent:
        missing.append("REDDIT_USER_AGENT")
    if not config.client_secret:
        warnings.append("REDDIT_CLIENT_SECRET is blank; installed-client OAuth flow will be used.")

    print(f"Config file: {ENV_PATH}")
    print(f"REDDIT_CLIENT_ID: {'set' if config.client_id else 'missing'}")
    print(f"REDDIT_CLIENT_SECRET: {'set' if config.client_secret else 'blank'}")
    print(f"REDDIT_USER_AGENT: {'set' if config.user_agent and 'your_reddit_username' not in config.user_agent else 'missing/placeholder'}")
    for warning in warnings:
        print(f"Warning: {warning}")
    if missing:
        print("Missing required config: " + ", ".join(missing), file=sys.stderr)
        return 2
    return 0


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Scan finance subreddits for ticker attention spikes.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Run a scan and print a text report.")
    scan.add_argument("--subreddits", default=",".join(DEFAULT_SUBREDDITS))
    scan.add_argument("--sorts", default="new,hot,rising")
    scan.add_argument("--limit", type=int, default=50)
    scan.add_argument("--sleep", type=float, default=0.35)
    scan.add_argument("--top", type=int, default=15)
    scan.add_argument("--max-chars", type=int, default=6000)

    sub.add_parser("init-env", help=f"Create {ENV_PATH}")
    sub.add_parser("check-config", help="Validate Reddit OAuth config without making network calls.")

    latest = sub.add_parser("latest", help="Print latest report.")
    latest.add_argument("--max-chars", type=int, default=6000)

    args = parser.parse_args()
    if args.command == "init-env":
        init_env()
        return 0
    if args.command == "check-config":
        return check_config()
    if args.command == "latest":
        path = STATE_DIR / "latest.txt"
        if not path.exists():
            print("No report yet. Run: radar.py scan")
            return 1
        print(path.read_text(encoding="utf-8")[: args.max_chars])
        return 0

    report = run_scan(args)
    text = format_report(report, max_chars=args.max_chars)
    print(text)
    return 2 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
