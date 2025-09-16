import logging
import os
import sys
import json
import time
import random
import requests
import yaml
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

STATE_FILE = Path.home() / ".raid_scout_state.json"
print(STATE_FILE)

# Logging setup
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not cfg["twitch"]["client_id"] or not cfg["twitch"]["client_secret"]:
        logging.error("Missing Twitch client_id/client_secret. Set them or use env:TWITCH_CLIENT_ID/SECRET.", file=sys.stderr)
        sys.exit(1)
    return cfg


def get_twitch_token(client_id, client_secret):
    session = requests.Session()
    # Retry at 15s, 30s, 60s, 120s, 240s
    retries = Retry(
        total=5,
        backoff_factor=15,
        status_forcelist=[404, 429, 502, 503, 504],
        allowed_methods=["POST"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('https://', adapter)
    resp = session.request(
        method="POST",
        url=TOKEN_URL,
        headers=None,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15
    )
    if resp.status_code == 200:
        data = resp.json()
        return data["access_token"]


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def fetch_live_streams(raid_targets, client_id, token):
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    live = {}
    for batch in chunked([rt.lower() for rt in raid_targets], 100):
        params = [("user_login", target) for target in batch]  # reset per batch
        resp = requests.get(STREAMS_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        for stream in resp.json().get("data", []):
            name = stream.get("user_login", "").lower()
            live[name] = {
                "name": name,
                "title": stream.get("title", ""),
                "game": stream.get("game_name", ""),
                "viewers": stream.get("viewer_count", 0),
                "started_at": stream.get("started_at", "")
            }
    return live


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def is_on_cooldown(login, state, cooldown_hours):
    if cooldown_hours <= 0:
        return False
    info = state.get("last_raids", {}).get(login)
    if not info:
        return False
    last = datetime.fromisoformat(info)
    return datetime.utcnow() - last < timedelta(hours=cooldown_hours)


def mark_raided(login, state):
    state.setdefault("last_raids", {})[login] = datetime.utcnow().isoformat()
    save_state(state)


def _parse_started_at(ts: str):
    if not ts:
        return None
    try:
        # 'Z' => UTC; fromisoformat needs '+00:00'
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def uptime_hours(ts: str):
    dt = _parse_started_at(ts)
    if not dt:
        return None
    # ensure aware UTC math
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def format_uptime(hours):
    if hours is None:
        return "?"
    total_m = int(round(hours * 60))
    h, m = divmod(total_m, 60)
    return f"{h:02d}:{m:02d}"


def pick_target(live_dict, config, state):
    raid_config = config.get("raid", {})
    strategy = raid_config.get("pick_strategy", "priority_then_viewers")
    cooldown_hours = int(raid_config.get("cooldown_hours", 0))
    long_stream_hours = float(raid_config.get("long_stream_hours", 4))

    meta_by_login = {c["name"].lower(): c for c in config["channels"]}

    def _last_raided_at(login):
        ts = state.get("last_raids", {}).get(login)
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return None

    candidates = []
    for name, stream in live_dict.items():
        meta = meta_by_login.get(name, {})
        on_cd = is_on_cooldown(name, state, cooldown_hours)
        last_dt = _last_raided_at(name)
        uptime = uptime_hours(stream.get("started_at"))
        is_long = (uptime is not None) and (uptime >= long_stream_hours)

        candidates.append(
            {
                "name": name,
                "priority": int(meta.get("priority", 9999)),
                "cooldown": bool(on_cd),
                "uptime": uptime,
                "long_stream": is_long,
                "last_raided_at": last_dt.isoformat() if last_dt else None,
                **stream,
            }
        )
    
    if not candidates:
        return None, []
    
    def sort_key(c):
        # Time since last raid
        if c["last_raided_at"]:
            age_hours = (datetime.utcnow() - datetime.fromisoformat(c["last_raided_at"])).total_seconds() / 3600.0
        else:
            age_hours = float("inf")  # never raided before
        age_order = -age_hours if c["cooldown"] else 0  # only matters inside cooldown group

        # Ordering:
        #   1) not on cooldown first
        #   2) not long-running first
        #   3) lower priority number first
        #   4) shorter uptime first
        #   5) higher viewers last (as final tiebreaker)
        return (
            c["cooldown"],                 # False (0) beats True (1)
            c["long_stream"],              # False (0) beats True (1)
            c["priority"],                 # lower number = higher priority
            age_order,                     # older raid first within cooldown group
            c["uptime"] if c["uptime"] is not None else float("inf"),
            c.get("viewers", 10**9)        # fewer viewers as final tiebreaker
        )          
    
    ranked = sorted(candidates, key=sort_key)

    if strategy == "viewers_only":
        ranked.sort(key=lambda x: x["viewers"])
    if strategy == "priority_only":
        ranked.sort(key=lambda x: x["priority"])

    return ranked[0], ranked


def format_table(rows):
    if not rows:
        return ""
    # simple fixed-width table for terminal
    headers = ["Streamer", "Uptime", "Viewers", "Game", "Title"]
    col1 = max(5, max(len(r["name"]) for r in rows))
    col2 = 7
    col3 = 7
    col4 = min(24, max(4, *(len((r["game"] or "")[:24]) for r in rows)))
    lines = []
    lines.append(f"{headers[0]:<{col1}}  {headers[1]:>{col2}}  {headers[2]:>{col3}}  {headers[3]:<{col4}}  {headers[4]}")
    lines.append("-" * (col1 + col2 + col3 + col4 + 8 + 40))
    for r in rows:
        game = (r["game"] or "")[:col4]
        title = (r["title"] or "").replace("\n", " ")
        uptime = format_uptime(r.get("uptime"))
        lines.append(f"{r['name']:<{col1}}  {uptime:>{col2}}  {r['viewers']:>{col3}}  {game:<{col4}}  {title}")
    return "\n".join(lines)


def main():
    config = load_config("raid_config.yml")
    client_id = config["twitch"]["client_id"]
    client_secret = config["twitch"]["client_secret"]

    token = get_twitch_token(client_id, client_secret)

    raid_targets = [c["name"] for c in config["channels"]]
    live_dict = fetch_live_streams(raid_targets, client_id, token)

    if not live_dict:
        logging.warning("None of the raid targets are live.")
        sys.exit(0)

    state = load_state()
    choice, ranked = pick_target(live_dict, config, state)
    print("\nCurrently live from list:\n")
    print(format_table(ranked))
    print()

    if choice:
        print(f"SUGGESTED RAID TARGET: {choice['name']}")
        print(f"Command: /raid {choice['name']}")
        # record the raid choice
        state = load_state()  # reload real state in case we used --no-cooldown
        mark_raided(choice["name"], state)


if __name__ == "__main__":
    main()
