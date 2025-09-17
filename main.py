import os
import sys
import json
import requests
import yaml
import streamlit as st
import pandas as pd
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

STATE_FILE = Path.home() / ".raid_scout_state.json"


# ---------- HELPERS ----------
def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        st.error("raid_config.yml not found")
    except yaml.YAMLError as e:
        st.error("Failed to parse raid_config.yml")
        st.exception(e)
    except Exception as e:
        st.exception(e)

    cfg.setdefault("twitch", {})
    cfg.setdefault("raid", {})
    cfg.setdefault("channels", [])
    return cfg


def build_session(total=5, backoff=2, statuses=(429, 502, 503, 504), methods=("GET","POST")):
    s = requests.Session()
    r = Retry(
        total=total,
        backoff_factor=backoff,
        status_forcelist=statuses,
        allowed_methods=frozenset(m.upper() for m in methods),
    )
    adapter = HTTPAdapter(max_retries=r)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def show_http_error(prefix: str, e: requests.HTTPError):
    code = e.response.status_code if e.response is not None else "?"
    st.error(f"{prefix}: HTTP {code}")
    with st.expander("Details", expanded=False):
        st.code((e.response.text if e.response is not None else "")[:2000])
    st.exception(e)
    st.stop()


def parse_json(resp: requests.Response, context: str):
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        st.error(f"{context}: invalid JSON")
        with st.expander("Details", expanded=False):
            st.code(resp.text[:2000])
        st.exception(e)
        st.stop()


def get_twitch_token(client_id, client_secret):
    session = build_session()
    try:
        resp = session.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        show_http_error("Fetching app token failed", e)
    except requests.RequestException as e:
        st.error("Network error contacting Twitch (auth)")
        st.exception(e)
        st.stop()

    data = parse_json(resp, "Token response")
    try:
        return data["access_token"]
    except KeyError as e:
        st.error("Token response missing 'access_token'")
        with st.expander("Details", expanded=False):
            st.code(resp.text[:2000])
        st.exception(e)
        st.stop()


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def fetch_live_streams(raid_targets, client_id, token):
    session = build_session()
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    live = {}

    # local chunk helper (or use your existing one)
    def chunked(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i:i+n]

    try:
        for batch in chunked([rt.lower() for rt in raid_targets], 100):
            params = [("user_login", target) for target in batch]
            resp = session.get(STREAMS_URL, headers=headers, params=params, timeout=15)
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                show_http_error("Helix Get Streams failed", e)

            payload = parse_json(resp, "Streams response")
            for s in payload.get("data", []):
                name = (s.get("user_login") or "").lower()
                live[name] = {
                    "name": name,
                    "title": s.get("title", ""),
                    "game": s.get("game_name", ""),
                    "viewers": s.get("viewer_count", 0),
                    "started_at": s.get("started_at", ""),
                }
    except requests.RequestException as e:
        st.error("Network error contacting Twitch (streams)")
        st.exception(e)
        st.stop()

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


def parse_started_at(ts: str):
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def uptime_hours(ts: str):
    dt = parse_started_at(ts)
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


# ---------- UI ----------
st.set_page_config(page_title="Raid Scout", page_icon="🎯", layout="wide")
st.title("🎯 JenAndAliona's Raid Scout")

with st.sidebar:
    st.markdown("**Config**")
    cfg_path = st.text_input("Path to raid_config.yml", value="raid_config.yml")
    client_id = os.getenv("TWITCH_CLIENT_ID") or st.text_input("TWITCH_CLIENT_ID", type="password")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET") or st.text_input("TWITCH_CLIENT_SECRET", type="password")
    run_btn = st.button("Refresh")

config = load_config(cfg_path)
channels = [ c.get("name").lower() for c in config.get("channels", []) if (c.get("name")) ]
raid_config = config.get("raid", {})
st.markdown(
    f"- **Cooldown**: {raid_config.get('cooldown_hours',0)}h · **Long stream**: ≥ {raid_config.get('long_stream_hours',4)}h · **Targets**: {', '.join(channels) or '—'}"
)

if run_btn or True:
    if not client_id or not client_secret:
        st.warning("Provide TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (env or sidebar)")
    elif not channels:
        st.warning("No channels in config")
    else:
        try:
            token = get_twitch_token(client_id, client_secret)
            live = fetch_live_streams(channels, client_id, token)
            if not live:
                st.info("No configured channels are live")
            else:
                state = load_state()
                choice, ranked = pick_target(live, config, state)
                pick = choice

                # Suggested
                st.subheader("Suggested raid target")
                st.markdown(f"**/raid `{pick['name']}`**")
                st.caption(
                    f"Priority {pick['priority']} · Uptime {format_uptime(pick['uptime'])} · "
                    f"{pick.get('viewers',0)} viewers · {pick.get('game','')}"
                )
                if st.button(f"Mark raided: {pick['name']}"):
                    s = load_state()
                    mark_raided(pick["name"], s)
                    st.success(f"Marked {pick['name']} as raided.")

                # Table
                rows = [
                    {
                        "Login": r["name"],
                        "Uptime": format_uptime(r["uptime"]),
                        "Viewers": r.get("viewers", 0),
                        "Game": r.get("game", ""),
                        "Title": r.get("title", ""),
                        "Priority": r["priority"],
                    }
                    for r in ranked
                ]
                table = pd.DataFrame(rows)
                st.dataframe(table, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Error: {e}")
