# Host — Spotify → panel bridge

Polls Spotify for your currently playing track, downloads the album art, resizes
it to 64×64, packs it as RGB565, and POSTs it to the panel firmware. Runs as a
single-replica k8s pod.

## 1. Create a Spotify app

1. Go to <https://developer.spotify.com/dashboard> and create an app.
2. Note the **Client ID** and **Client Secret**.
3. Add this exact Redirect URI to the app settings:
   `http://127.0.0.1:8888/callback`

## 2. Mint a refresh token (once, on your laptop)

The pod is headless, so do the interactive OAuth once locally:

```bash
cd host
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SPOTIFY_CLIENT_ID=...
export SPOTIFY_CLIENT_SECRET=...
python auth_bootstrap.py        # opens a browser; approve; it prints a token
```

Copy the printed **refresh token** — it's long-lived and what the pod uses.

## 3. (Optional) Run locally to test end-to-end

With the panel flashed and reachable:

```bash
cp .env.example .env            # fill in the 3 Spotify values + PANEL_HOST
set -a && source .env && set +a
python -m spotify_matrix.main
```

Play something on Spotify; the panel should update within a few seconds.

You can also test the panel alone (no Spotify) with:

```bash
python send_test.py <panel-ip>         # color bars
python send_test.py <panel-ip> image cover.jpg
python send_test.py <panel-ip> clear
```

## 4. Deploy to k8s

```bash
# Build & push the image to your registry.
docker build -t REGISTRY/spotify-matrix:latest .
docker push REGISTRY/spotify-matrix:latest

# Create the secret (don't commit the filled-in file).
cp k8s/secret.example.yaml k8s/secret.yaml   # edit in your 3 values
kubectl apply -f k8s/secret.yaml

# Set image + PANEL_HOST (the ESP32's static LAN IP) in k8s/deployment.yaml,
# then deploy.
kubectl apply -f k8s/deployment.yaml
kubectl logs -f deploy/spotify-matrix
```

The pod must be able to reach the panel's IP on your LAN (see the top-level
README's notes on LAN reachability and NetworkPolicies).

## Configuration (env vars)

| Var                     | Default | Meaning                                           |
| ----------------------- | ------- | ------------------------------------------------- |
| `SPOTIFY_CLIENT_ID`     | (req)   | required                                          |
| `SPOTIFY_CLIENT_SECRET` | (req)   | required                                          |
| `SPOTIFY_REFRESH_TOKEN` | (req)   | required (from `auth_bootstrap.py`)               |
| `PANEL_HOST`            | (req)   | required; panel IP/host, no scheme                |
| `POLL_INTERVAL`         | `4`     | seconds between Spotify polls                     |
| `ART_BRIGHTNESS`        | `1.0`   | 0-1 flat pre-scale on art                         |
| `POWER_LIMIT`           | `0.5`   | 0-1 max avg current; dims bright frames (USB-C)   |
| `IDLE_TIMEOUT`          | `1800`  | seconds to hold last cover after stop, then blank |
| `LOG_LEVEL`             | `INFO`  | logging level                                     |

## How it behaves

- **Playing:** shows the current album cover; only redraws on track change.
- **Stopped/paused:** keeps the last cover on screen for `IDLE_TIMEOUT` seconds
  (30 min by default), then blanks to zero light.
- **Idle past the timeout, or nothing played yet this session:** blank panel
  (an all-off frame, so no light).
- Track with no art blanks rather than showing a stale cover.
- Handles Spotify token expiry/rotation and transient API errors without dying.
- Works for podcast episodes too (uses the show/episode art).
