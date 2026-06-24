# Reddit Market Radar

Read-only ticker attention radar for finance subreddits. This is not a trading bot and does not place orders.

## What It Does

- Fetches posts from finance subreddits through Reddit OAuth.
- Extracts likely ticker mentions from titles/selftext.
- Stores posts, mentions, and run history in SQLite.
- Scores current attention against recent local history.
- Writes text and JSON reports under `~/.openclaw/reddit-market-radar/reports/`.

## Architecture

This is a local Python script that polls Reddit through OAuth from the user's own machine. It does not post, comment, vote, message users, run a bot account, host a public service, or redistribute Reddit content.

## Setup

Anonymous Reddit JSON is often blocked. Use Reddit OAuth.

```bash
cd openclaw-reddit-market-radar
./radar.py init-env
```

Then edit:

```text
/Users/openc/.openclaw/reddit-market-radar.env
```

Set:

```text
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=OpenClawMarketRadar/0.1 by u/your_reddit_username
```

Create the Reddit app at:

```text
https://www.reddit.com/prefs/apps
```

Check local config without calling Reddit:

```bash
./radar.py check-config
```

## Run

```bash
./radar.py scan
```

Latest report:

```bash
./radar.py latest
```

## Default Subreddits

```text
r/wallstreetbets
r/stocks
r/investing
r/SecurityAnalysis
r/ValueInvesting
r/options
r/shortsqueeze
r/pennystocks
r/biotechplays
r/SPACs
```

## Interpretation

The score is attention velocity, not investment quality. Use it to decide what to investigate, not what to buy.
