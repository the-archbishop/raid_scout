# Raid Scout (Streamlit)

A tiny Streamlit web app to pick a Twitch **raid** target from a curated list.  
It checks who’s live, shows avatars, uptime, viewers, last raided time, and suggests the best target based on your rules. You can override with one click and mark a raid as done.

---
## Requirements

- Python **3.9+**
- Packages: `streamlit`, `requests`, `pyyaml`, `urllib3`

### Quick install

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install streamlit requests pyyaml urllib3
```

---

## Twitch setup (one-time)

1. Create a Twitch application: https://dev.twitch.tv/console/apps  
   - **OAuth Redirect URL**: you’re using the client-credentials flow, so any placeholder works (e.g., `https://localhost`).
2. Copy your **Client ID** and **Client Secret**.

---

## Configure secrets

Use either environment variables **or** Streamlit secrets.

### Option A — `.streamlit/secrets.toml` (recommended)

```
your_project/
├─ main.py
└─ .streamlit/
   └─ secrets.toml
```

**.streamlit/secrets.toml**
```toml
TWITCH_CLIENT_ID = "your_id_here"
TWITCH_CLIENT_SECRET = "your_secret_here"
```

> Do not commit this file. Add `.streamlit/secrets.toml` to `.gitignore`.  
> On Streamlit Cloud: paste the same TOML into **App → Settings → Secrets**.

### Option B — environment variables

```bash
export TWITCH_CLIENT_ID=your_id_here
export TWITCH_CLIENT_SECRET=your_secret_here
```

---

## Configure channels

Create a `raid_config.yml` next to `main.py`:

```yaml
raid:
  cooldown_hours: 168        # soft cooldown window (e.g., 7 days)
  long_stream_hours: 2       # treat streams ≥ this as "long" for de-prioritization

channels:
  - name: streamer1
    priority: 1
  - name: streamer2
    priority: 2
  - name: streamer3
    priority: 3
  # add more...
```

Notes:
- `priority`: lower number = higher priority.
- You can add/remove channels anytime; the app reads this file on launch.

---

## Run it

```bash
streamlit run main.py
```

Open the URL Streamlit prints (usually http://localhost:8501).

---

## Using the app

- **Suggested raid target**: shows the current best pick with `/raid <name>`.
- **Override**: Click **Raid** next to any streamer in the list to set them as the suggestion; click **Clear override** to revert to the automatic pick.
- **Mark raided**: records a timestamp in `~/.raid_scout_state.json`.
- **Live channels list** shows:
  - **Streamer / Title** with avatar
  - **Uptime**, **Viewers**, **Game**, **Last Raided**
  - **Raid** button per row to override raid target

**State file**
- Stored at: `~/.raid_scout_state.json`
- Contains the `last_raids` map (login → ISO timestamp).  
  Delete this file to “reset” cooldowns.

---

## How ranking works

We rank every **live** channel using five signals, then sort with a deterministic tie-break chain (earlier items win):

1. **Cooldown** — channels *not* within `cooldown_hours` come first.
2. **Stream length** — streams **under** `long_stream_hours` come first.
3. **Priority** — lower `priority` number beats higher (1 > 2 > 3).
4. **Uptime** — shorter uptime wins.
5. **Viewers** — fewer viewers wins (final tiebreaker).

**Signals computed per channel**

- `cooldown`: `true` if last raided within `cooldown_hours` (soft penalty; still eligible).
- `long_stream`: `true` if `uptime >= long_stream_hours`.
- `priority`: integer from `raid_config.yml` (lower = higher priority).
- `uptime`: hours since `started_at`.
- `viewers`: current viewer count.

**Equivalent sort key (ascending)**

```
(cooldown, long_stream, priority, uptime, viewers)
```

> If everyone’s on cooldown, rule 1 ties and rules 2–5 decide. If multiple fields tie, the next field breaks the tie, in order.

---
## Project structure (suggested)

```
.
├─ main.py
├─ raid_config.yml
└─ .streamlit/
   └─ secrets.toml
```

---

## License

MIT (or your preferred license).

---

## Credits

Built by Bishop for **Jen & Aliona**. Uses the Twitch Helix API and Streamlit.
