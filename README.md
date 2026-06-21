# gallery-dl-wrapper

A tiny Python CLI wrapper around `gallery-dl` that:

- Uses a **repo-local** `config.json` (and ignores any global `~/.config/gallery-dl/config.json`)
- Lets you group target URLs by provider (ex: `twitter`, `instagram`) inside `sites.json`
- Supports a `--dry-run` mode to print commands without downloading
- Keeps downloads + state **repo-local** (no cross-drive references)

## Requirements

- Python 3.12+
- Poetry

## Install

```bash
poetry install
```

## Config

Create a local config and keep secrets out of git:

```bash
cp config.json.example config.json
```

Suggested `config.json` structure:

- gallery-dl options live under `extractor`
- wrapper-managed URL lists live in `sites.json`

```json
{
  "extractor": {
    "base-directory": "./data",
    "archive": "./state/archive.sqlite3",

    "twitter": {
      "cookies": "./state/twitter-cookies.txt"
    },

    "instagram": {
      "cookies": "./state/instagram-cookies.txt"
    }
  }
}
```

Suggested `sites.json` structure:

```json
{
  "twitter": {
    "host": "x.com",
    "path_suffix": "media",
    "sites": [
      { "name": "someuser", "username": "someuser" },
      { "name": "anotheruser", "username": "anotheruser" }
    ]
  },
  "instagram": {
    "host": "www.instagram.com",
    "path_suffix": "posts/",
    "sites": [
      { "name": "someuser", "username": "someuser" },
      { "name": "anotheruser", "username": "anotheruser" }
    ]
  }
}
```

### Notes

- `extractor.archive` enables incremental runs (re-run safely without re-downloading already archived items).
- Some sites require authentication; using a cookies file is typically the most reliable approach.
- The wrapper runs `gallery-dl` with `--ignore-config` so your global gallery-dl config cannot interfere.

## Repo layout

- `src/gallery_dl_wrapper/` - CLI code
- `tests/` - tests
- `config.json.example` - safe config template
- `config.json` - ignored (personal creds)
- `data/` - downloads (ignored)
- `state/` - archive + cookies (ignored)

## Run examples

### 1) First-time setup

```bash
poetry install
cp config.json.example config.json
mkdir -p state data
```

### 2) Verify what would run (no downloads)

```bash
poetry run gdw --dry-run
```

### 3) Run everything configured (all providers)

```bash
poetry run gdw
```

### 4) Run only one provider's list

```bash
poetry run gdw --provider twitter
poetry run gdw --provider instagram
```

### 5) Run a single URL (bypasses provider lists)

```bash
poetry run gdw "https://x.com/someuser/media"
poetry run gdw "https://www.instagram.com/someuser/posts/"
```

### 6) One-off URL with an alternate config file

```bash
poetry run gdw --config ./config.json "https://example.com/some/gallery"
```

### 7) Twitter cookies placement

If your config points to `./state/twitter-cookies.txt`, put the exported cookies file there:

```bash
mkdir -p state
cp /path/to/exported/twitter-cookies.txt ./state/twitter-cookies.txt
poetry run gdw --provider twitter
```

### 8) Manual smoke test (ignore global gallery-dl config)

The wrapper always uses `--ignore-config`, but you can verify manually:

```bash
poetry run gallery-dl --ignore-config --config ./config.json -K "https://x.com/someuser/media"
```
