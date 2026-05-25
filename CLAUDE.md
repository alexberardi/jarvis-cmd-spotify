# Spotify

Spotify package for Jarvis

## Development

```bash
jdt test .              # Run Pantry-compatible tests
jdt test . -v           # Verbose output
jdt test . --install-deps  # Auto-install pip deps before testing
jdt validate .          # Fast manifest-only check
jdt manifest .          # Regenerate manifest from code
jdt deploy local .      # Install to local node
```

## Package Structure

This is a Jarvis package with the following components:

- **spotify** — Voice command (IJarvisCommand) (`commands/spotify/command.py`)

## Key Rules

- **Logging**: Use the `try: from jarvis_log_client` pattern (see existing stubs)
- **Errors**: Never raise from `run()` — return `CommandResponse.error_response()` or `DeviceControlResult(success=False, error="...")`
- **Data access**: Use `JarvisStorage` for secrets and persistent data, never raw SQLite/SQLAlchemy
- **Shared code**: If you add shared modules, name the directory `spotify_shared/` (not `shared/`, `lib/`, `helpers/` — those collide on sys.path after install)
- **`context_data["message"]`**: This key is what gets spoken aloud by TTS

## Architectural invariant

**Playback control goes through the local go-librespot HTTP API, not through
Spotify's Web API.** The Web API is reserved for search and read-only
metadata. This split is load-bearing — the prior versions of this package
that drove playback through `PUT /me/player/play`/`/pause`/`/next` etc. paid
constant 5xx pain because Spotify's Connect API is unreliable for
non-first-party devices. Don't reintroduce Web API calls for any of:
`play / pause / next / previous / volume / shuffle / repeat / now_playing`.
The local API is `localhost:3678` (see `spotify_shared/local_client.py`).

## Manifest

The `jarvis_package.yaml` declares:
- **secrets** — credentials/config the user must provide (shown in mobile app settings)
- **packages** — pip dependencies installed on the node
- **components** — entry points for each component (used by the installer)

Run `jdt manifest . --non-interactive` to regenerate the manifest from your code after making changes.

## Testing

`jdt test` checks three things:
1. Manifest is valid (schema, semver, categories, paths exist)
2. Static analysis passes (correct base class, required methods, no dangerous imports)
3. Import succeeds and properties return correct types

All stubs pass out of the box — if tests break after your changes, check the error messages for what to fix.

## SDK Quick Reference

### CommandResponse
```python
CommandResponse.success_response(context_data={"message": "spoken text"}, wait_for_input=False)
CommandResponse.error_response(error_details="what went wrong")
CommandResponse.follow_up_response(context_data={"message": "what else?"})
```

### JarvisStorage
```python
from jarvis_command_sdk import JarvisStorage
storage = JarvisStorage("spotify")
api_key = storage.get_secret("MY_API_KEY", scope="integration")
storage.save(key="cache", data={"result": "value"})
data = storage.get(key="cache")
```

### JarvisParameter
```python
JarvisParameter(name="city", param_type="string", required=True, description="City name")
JarvisParameter(name="count", param_type="int", required=False, default="5", description="How many")
JarvisParameter(name="unit", param_type="string", enum_values=["imperial", "metric"], description="Units")
```

### JarvisSecret
```python
JarvisSecret(key="API_KEY", description="Service API key", scope="integration",
             value_type="string", is_sensitive=True, required=True, friendly_name="API Key")
```
