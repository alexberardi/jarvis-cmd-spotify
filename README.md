# Spotify for Jarvis

Stream Spotify on a Jarvis node with voice control. Search and play tracks,
artists, albums, or playlists; pause, skip, control volume, shuffle, and
repeat.

Audio plays locally on the node via [`spotifyd`](https://github.com/Spotifyd/spotifyd) —
the binary auto-installs from upstream releases on first use, so there's
nothing to install manually. If a Bluetooth speaker is paired with the node,
playback automatically routes to it (via PulseAudio's `PULSE_SINK`).

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

| File | Purpose |
|------|---------|
| `commands/spotify/command.py` | Voice command entry point — pure dispatch, no subprocess/os imports |
| `spotify_shared/installer.py` | Auto-downloads the right `spotifyd` binary for the platform |
| `spotify_shared/spotifyd_manager.py` | Launches `spotifyd` as a subprocess with Bluetooth audio routing |
| `spotify_shared/web_client.py` | Spotify Web API client (search + playback control) |
| `spotify_shared/auth.py` | OAuth refresh-token exchange |

`spotifyd` is launched in Zeroconf discovery mode, so the user pairs once
from their phone's Spotify app. Credentials are then cached in
`~/.jarvis/spotify/cache/` and re-used on every restart.

## Development

```bash
jdt test .              # validation
jdt test . -v           # verbose
jdt deploy local .      # install on the local node
```
