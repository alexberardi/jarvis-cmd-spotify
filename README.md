# Spotify for Jarvis

Stream Spotify on a Jarvis node with voice control. Search and play tracks,
artists, albums, or playlists; pause, skip, control volume, shuffle, and
repeat.

Audio plays locally on the node via [`go-librespot`](https://github.com/devgianlu/go-librespot) —
the binary auto-installs from upstream releases on first use, so there's
nothing to install manually. If a Bluetooth speaker is paired with the
node, playback automatically routes to it (via PulseAudio's `PULSE_SINK`).

## Voice examples

| Phrase | Action |
|--------|--------|
| "Play Radiohead on Spotify" | Searches and plays the artist Radiohead |
| "Play my Discover Weekly playlist" | Plays the playlist |
| "Pause" / "Stop the music" | Pauses playback |
| "Skip" / "Next song" | Next track |
| "Previous" / "Go back" | Previous track |
| "Set volume to 60" | Volume = 60% |
| "Turn on shuffle" / "Shuffle off" | Toggles shuffle |
| "Repeat this song" / "Turn off repeat" | Repeat one / off |
| "What's playing?" | Reads the current track |

## Setup

1. **Spotify Premium account** — playback APIs require Premium.
2. **Create a Spotify Developer App** at
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   - Redirect URI: `com.jarvis.app://oauthredirect`
   - Tick "Web API"
3. **Paste the Client ID** in the package's settings on the Jarvis mobile app
   (`SPOTIFY_CLIENT_ID`) and tap "Authenticate with Spotify".
4. **Pair the node**: open Spotify on your phone, tap the Devices icon, and
   select **Jarvis** (or whatever you set in `SPOTIFY_DEVICE_NAME`). Pairing
   only happens once.

## Architecture

```
                       ┌───────────────────────────────────────┐
                       │ Spotify Web API (api.spotify.com)     │
                       └─────────────────────▲─────────────────┘
                                             │ search / metadata only
                                             │ (read-only, reliable)
                                             │
voice ──► command.py ─────────────────────────┤
                                             │ play / pause / skip /
                                             │ prev / volume / shuffle /
                                             │ repeat / now_playing
                                             ▼
                       ┌───────────────────────────────────────┐
                       │ go-librespot (this node, port 3678)   │
                       │ POST /player/play uri=spotify:track:… │
                       │ POST /player/pause                    │
                       │ GET  /status                          │
                       └─────────────────────┬─────────────────┘
                                             │ Spotify Connect
                                             │ (decoded PCM)
                                             ▼
                                          PulseAudio → speaker / BT
```

| File | Purpose |
|------|---------|
| `commands/spotify/command.py` | Voice command entry point — dispatches actions |
| `spotify_shared/installer.py` | Downloads the right `go-librespot` binary for the platform |
| `spotify_shared/go_librespot_manager.py` | Owns the daemon's process lifecycle (PID, start/stop/restart) |
| `spotify_shared/local_client.py` | HTTP client for go-librespot's localhost API (the control path) |
| `spotify_shared/web_client.py` | Spotify Web API client (search + user playlists) |
| `spotify_shared/auth.py` | OAuth refresh-token exchange |
| `agents/spotify_keepalive/agent.py` | Keeps the daemon alive + refreshes OAuth tokens proactively |

### Why two clients?

Every previous version of this package drove playback through Spotify's
Web API, which 5xx'd constantly when the target Connect device was our
own librespot. Cutting Spotify's cloud out of the control path — and only
hitting it for search/metadata — removed the entire class of "play
succeeded, audio didn't" failures. Music Assistant does the same thing
internally; this package borrows that architecture so a self-hosted node
gets the same reliability without needing MA in the loop.

`go-librespot` is launched in Zeroconf discovery mode, so the user pairs
once from their phone's Spotify app. Credentials are then cached in
`~/.jarvis/spotify/go-librespot/` and re-used on every restart.

## Development

```bash
jdt test .              # validation
jdt test . -v           # verbose
jdt deploy local .      # install on the local node
```
