import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from tqdm import tqdm


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_config(config_path: Path) -> dict[str, Any]:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read config: {e}", file=sys.stderr)
        raise SystemExit(2)


def _resolve_base_directory(cfg: dict[str, Any], config_path: Path) -> Path:
    """
    Resolve extractor.base-directory, treating relative paths as repo-relative
    (relative to the config.json location). Default: ./data
    """
    repo_root = config_path.parent
    extractor_cfg = cfg.get("extractor", {})
    base_dir_str = "./data"

    if isinstance(extractor_cfg, dict):
        bd = extractor_cfg.get("base-directory")
        if isinstance(bd, str) and bd.strip():
            base_dir_str = bd.strip()

    base_dir = Path(base_dir_str)
    if not base_dir.is_absolute():
        base_dir = (repo_root / base_dir).resolve()
    return base_dir


def _args_has_flag(argv: list[str], *flags: str) -> bool:
    return any(a in flags for a in argv)


def _passthrough_sets_base_directory(argv: list[str]) -> bool:
    for idx, arg in enumerate(argv):
        if arg in ("-o", "--option"):
            if idx + 1 < len(argv):
                opt = argv[idx + 1]
                if opt.startswith("base-directory=") or opt.startswith("extractor.base-directory="):
                    return True
        elif arg.startswith("-o") and "=" in arg:
            key = arg[2:]
            if key.startswith("base-directory=") or key.startswith("extractor.base-directory="):
                return True
    return False


def _build_gallery_dl_cmd(
    url: str,
    config_path: Path,
    dest: Path | None,
    passthrough_args: list[str],
) -> list[str]:
    cmd = ["gallery-dl", "--ignore-config", "--config", str(config_path)]

    # Inject per-site base-directory only if caller didn't pass their own override
    if (
        dest is not None
        and not _args_has_flag(passthrough_args, "--destination", "-d", "--dest")
        and not _passthrough_sets_base_directory(passthrough_args)
    ):
        cmd += ["-o", f"extractor.base-directory={dest}"]

    cmd += passthrough_args
    cmd.append(url)
    return cmd


def _run_gallery_dl_pipe(
    cmd: list[str],
    on_line: Callable[[str], None] | None = None,
    print_lines: bool = True,
) -> tuple[int, str]:
    """
    Stream output through a PIPE (no TTY).

    Note: Colors are typically disabled by gallery-dl in this mode.
    """
    tail: deque[str] = deque(maxlen=60)
    error_lines: deque[str] = deque(maxlen=20)

    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )

    assert proc.stdout is not None

    for line in proc.stdout:
        if on_line is not None:
            on_line(line.rstrip("\n"))
        if print_lines:
            sys.stdout.write(line)
            sys.stdout.flush()
        stripped = line.rstrip("\n")
        tail.append(stripped)
        if _ERROR_RE.search(stripped):
            error_lines.append(stripped)

    rc = proc.wait()
    if rc == 0:
        return 0, ""

    if error_lines:
        summary = "\n".join([ln for ln in error_lines if ln.strip()])
    else:
        summary = f"gallery-dl exited with code {rc}"
    return rc, summary[:2000]


def _run_gallery_dl_pty(cmd: list[str]) -> tuple[int, str]:
    """
    Stream output via a pseudo-TTY to preserve gallery-dl colors,
    while keeping a short tail for errors.json if it fails.
    """
    import pty

    tail: deque[str] = deque(maxlen=60)
    error_lines: deque[str] = deque(maxlen=20)
    master_fd, slave_fd = pty.openpty()

    proc = subprocess.Popen(
        cmd,
        stdout=slave_fd,
        stderr=slave_fd,
    )
    os.close(slave_fd)

    buf = b""
    try:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                chunk = b""

            if not chunk:
                break

            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                decoded = line.decode(errors="replace")
                tail.append(decoded)
                if _ERROR_RE.search(decoded):
                    error_lines.append(decoded)
    finally:
        if buf:
            decoded = buf.decode(errors="replace")
            tail.append(decoded)
            if _ERROR_RE.search(decoded):
                error_lines.append(decoded)
        os.close(master_fd)

    rc = proc.wait()
    if rc == 0:
        return 0, ""

    if error_lines:
        summary = "\n".join([ln for ln in error_lines if ln.strip()])
    else:
        summary = f"gallery-dl exited with code {rc}"
    return rc, summary[:2000]


def _run_gallery_dl(
    cmd: list[str],
    dry_run: bool,
    *,
    force_pipe: bool = False,
    on_line: Callable[[str], None] | None = None,
    print_lines: bool = True,
) -> tuple[int, str]:
    """
    Returns (returncode, message).

    - On dry_run: prints the command and returns success.
    - On real run: stream output to your terminal (so you see URLs/progress),
      while keeping a short tail for errors.json if it fails.
    """
    if dry_run:
        print(" ".join(cmd))
        return 0, ""

    if force_pipe:
        return _run_gallery_dl_pipe(cmd, on_line=on_line, print_lines=print_lines)

    # Prefer a PTY when we have a TTY to preserve gallery-dl color output.
    if sys.stdout.isatty() and os.name != "nt":
        return _run_gallery_dl_pty(cmd)

    return _run_gallery_dl_pipe(cmd, on_line=on_line, print_lines=print_lines)

def _errors_key(provider: str, name: str, username: str | None = None) -> str:
    if username:
        return f"{provider}:{name}:{username}"
    return f"{provider}:{name}"


def _load_errors(errors_path: Path) -> dict[str, Any]:
    if not errors_path.exists():
        return {"generated_at": _utc_now_iso(), "errors": {}}
    try:
        data = _load_json(errors_path)
        if not isinstance(data, dict):
            return {"generated_at": _utc_now_iso(), "errors": {}}
        if "errors" not in data or not isinstance(data["errors"], dict):
            data["errors"] = {}
        return data
    except Exception:
        return {"generated_at": _utc_now_iso(), "errors": {}}


def _upsert_error(
    errors_doc: dict[str, Any],
    provider: str,
    site: dict[str, Any],
    url: str,
    code: int,
    message: str,
) -> None:
    username = site.get("username")
    clean_username = username.lstrip("@") if isinstance(username, str) else None
    key = _errors_key(provider, site["name"], clean_username)
    entry: dict[str, Any] = {
        "provider": provider,
        "name": site["name"],
        "url": url,
        "code": code,
        "message": message,
        "last_error_at": _utc_now_iso(),
    }
    if clean_username:
        entry["username"] = clean_username

    errors_doc["errors"][key] = entry
    errors_doc["generated_at"] = _utc_now_iso()


def _clear_error(errors_doc: dict[str, Any], provider: str, name: str, username: str | None = None) -> bool:
    errors = errors_doc.get("errors", {})
    clean_username = username.lstrip("@") if isinstance(username, str) else None
    keys = [_errors_key(provider, name, clean_username)] if clean_username else []
    keys.append(_errors_key(provider, name))

    cleared = False
    for key in keys:
        if key in errors:
            del errors[key]
            cleared = True

    if cleared:
        errors_doc["generated_at"] = _utc_now_iso()
    return cleared


def _normalize_path_suffix(suffix: str) -> str:
    s = (suffix or "").strip()
    if not s:
        return ""
    if not s.startswith("/"):
        s = "/" + s
    return s


def _build_url(host: str, username: str, path_suffix: str) -> str:
    h = (host or "").strip()
    if h.startswith("http://") or h.startswith("https://"):
        h = h.split("://", 1)[1]
    h = h.strip("/")

    u = (username or "").strip().lstrip("@")
    suffix = _normalize_path_suffix(path_suffix)

    return f"https://{h}/{u}{suffix}"


def _normalize_username(username: str) -> str:
    return (username or "").strip().lstrip("@")


_DOMAIN_TO_PROVIDER: dict[str, str] = {
    "x.com": "twitter",
    "twitter.com": "twitter",
    "instagram.com": "instagram",
}


def _parse_import_line(line: str) -> tuple[str, str] | None:
    """Parse one line from an import file into (provider, username).

    Accepts bare domains or full URLs: instagram.com/user, https://x.com/user/media
    Returns None if the line is blank, a comment, or has an unrecognised domain.
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    # Strip scheme
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    # Split host from path
    parts = raw.lstrip("/").split("/", 1)
    domain = parts[0].lower()
    path = parts[1] if len(parts) > 1 else ""
    provider = _DOMAIN_TO_PROVIDER.get(domain)
    if not provider:
        return None
    username = path.split("/")[0].strip().lstrip("@")
    if not username:
        return None
    return provider, username


def _sync_sites_json(sites_path: Path, gdw_cfg: dict[str, object]) -> None:
    """Copy sites.json to every path listed in config gdw.sync_targets."""
    targets = gdw_cfg.get("sync_targets", [])
    if not isinstance(targets, list):
        return
    for raw in targets:
        dest = Path(str(raw))
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sites_path, dest)
            print(f"synced sites.json -> {dest}", file=sys.stdout)
        except Exception as exc:
            print(f"sync failed for {dest}: {exc}", file=sys.stderr)


def _normalize_url_for_match(url: str) -> str:
    u = (url or "").strip()
    while u.endswith("/"):
        u = u[:-1]
    return u


def _username_from_social_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("mobile."):
        host = host[7:]

    if host not in {"twitter.com", "x.com", "instagram.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None

    if host == "instagram.com" and parts[0] in {"p", "reel", "reels", "stories", "explore"}:
        return None
    if host in {"twitter.com", "x.com"} and parts[0] in {"i", "home", "hashtag", "search", "notifications"}:
        return None

    return parts[0]


def _load_sites_file(sites_path: Path) -> dict[str, Any]:
    data = _load_json(sites_path)
    if not isinstance(data, dict):
        raise ValueError("sites.json must contain a JSON object at the top level")
    return data


def _format_site_entry(site: dict[str, Any]) -> str:
    keys: list[str] = []
    for k in ("name", "username", "url", "disabled"):
        if k in site:
            keys.append(k)
    for k in sorted(site.keys()):
        if k not in keys:
            keys.append(k)
    parts = [f'{json.dumps(k)}: {json.dumps(site[k], ensure_ascii=False)}' for k in keys]
    return "{ " + ", ".join(parts) + " }"


def _write_sites_json(sites_path: Path, data: dict[str, Any]) -> None:
    sites_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["{"]
    providers = list(data.items())
    for idx, (provider, block) in enumerate(providers):
        lines.append(f'  {json.dumps(provider)}: {{')
        if not isinstance(block, dict):
            lines.append(f"    {json.dumps(block, ensure_ascii=False)}")
            lines.append("  }" + ("," if idx < len(providers) - 1 else ""))
            continue

        keys: list[str] = []
        for k in ("host", "path_suffix"):
            if k in block:
                keys.append(k)
        for k in sorted(block.keys()):
            if k not in keys and k != "sites":
                keys.append(k)
        if "sites" in block:
            keys.append("sites")

        for key_idx, key in enumerate(keys):
            is_last_key = key_idx == len(keys) - 1
            if key == "sites":
                sites_list = block.get("sites", [])
                lines.append('    "sites": [')
                if isinstance(sites_list, list):
                    for site_idx, site in enumerate(sites_list):
                        if isinstance(site, dict):
                            entry = _format_site_entry(site)
                        else:
                            entry = json.dumps(site, ensure_ascii=False)
                        comma = "," if site_idx < len(sites_list) - 1 else ""
                        lines.append(f"      {entry}{comma}")
                lines.append("    ]" + ("," if not is_last_key else ""))
            else:
                value = block.get(key)
                lines.append(
                    f"    {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
                    + ("," if not is_last_key else "")
                )

        lines.append("  }" + ("," if idx < len(providers) - 1 else ""))

    lines.append("}")
    sites_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _add_site(
    repo_root: Path,
    provider: str,
    name: str,
    username: str,
    host: str | None,
    path_suffix: str | None,
) -> None:
    sites_path = repo_root / "sites.json"
    if sites_path.exists():
        sites_doc = _load_sites_file(sites_path)
    else:
        sites_doc = {}

    if not isinstance(sites_doc, dict):
        print("sites.json must contain a JSON object at the top level", file=sys.stderr)
        raise SystemExit(2)

    provider_key = provider.strip()
    if not provider_key:
        print("Provider is required for --add", file=sys.stderr)
        raise SystemExit(2)

    block = sites_doc.get(provider_key)
    if block is None:
        if not host:
            print(f"Provider '{provider_key}' not found. Use --host to create it.", file=sys.stderr)
            raise SystemExit(2)
        block = {
            "host": host.strip(),
            "path_suffix": (path_suffix or "").strip().lstrip("/"),
            "sites": [],
        }
        sites_doc[provider_key] = block

    if not isinstance(block, dict):
        print(f"Provider '{provider_key}' must be a JSON object", file=sys.stderr)
        raise SystemExit(2)

    if host and not block.get("host"):
        block["host"] = host.strip()
    if path_suffix is not None and "path_suffix" not in block:
        block["path_suffix"] = path_suffix.strip().lstrip("/")

    sites_list = block.get("sites")
    if not isinstance(sites_list, list):
        sites_list = []
        block["sites"] = sites_list

    clean_name = (name or "").strip()
    clean_username = _normalize_username(username)
    if not clean_name or not clean_username:
        print("Name and username are required for --add", file=sys.stderr)
        raise SystemExit(2)

    for site in sites_list:
        if not isinstance(site, dict):
            continue
        if site.get("name") == clean_name and site.get("username") == clean_username:
            print(f"[{provider_key}] {clean_name} already exists", file=sys.stdout)
            return

    sites_list.append({"name": clean_name, "username": clean_username})
    _write_sites_json(sites_path, sites_doc)
    print(f"[{provider_key}] added {clean_name} (@{clean_username})", file=sys.stdout)


def _remove_site(
    repo_root: Path,
    provider: str,
    name: str,
    username: str | None,
) -> None:
    sites_path = repo_root / "sites.json"
    if not sites_path.exists():
        print("sites.json not found", file=sys.stderr)
        raise SystemExit(2)

    sites_doc = _load_sites_file(sites_path)
    provider_key = provider.strip()
    block = sites_doc.get(provider_key)
    if not isinstance(block, dict):
        print(f"Provider '{provider_key}' not found", file=sys.stderr)
        raise SystemExit(2)

    sites_list = block.get("sites")
    if not isinstance(sites_list, list):
        print(f"Provider '{provider_key}' has no sites list", file=sys.stderr)
        raise SystemExit(2)

    clean_name = (name or "").strip()
    clean_username = _normalize_username(username) if username else None
    if not clean_name:
        print("Name is required for --remove", file=sys.stderr)
        raise SystemExit(2)

    removed = 0
    new_sites: list[Any] = []
    for site in sites_list:
        if not isinstance(site, dict):
            new_sites.append(site)
            continue
        if site.get("name") != clean_name:
            new_sites.append(site)
            continue
        if clean_username and site.get("username") != clean_username:
            new_sites.append(site)
            continue
        removed += 1

    if removed == 0:
        print(f"[{provider_key}] no matching entries for {clean_name}", file=sys.stdout)
        return

    block["sites"] = new_sites
    _write_sites_json(sites_path, sites_doc)
    detail = f" (@{clean_username})" if clean_username else ""
    print(f"[{provider_key}] removed {removed} entr{'' if removed == 1 else 'ies'} for {clean_name}{detail}", file=sys.stdout)


def _sites_from_sites_json(sites_doc: dict[str, Any], provider_filter: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    providers = [provider_filter] if provider_filter else list(sites_doc.keys())
    for provider in providers:
        block = sites_doc.get(provider)
        if not isinstance(block, dict):
            continue

        host = block.get("host")
        if not isinstance(host, str) or not host.strip():
            continue

        path_suffix = block.get("path_suffix", "")
        if not isinstance(path_suffix, str):
            path_suffix = ""

        raw_sites = block.get("sites", [])
        if not isinstance(raw_sites, list):
            continue

        for site in raw_sites:
            if not isinstance(site, dict):
                continue
            if site.get("disabled") is True:
                continue

            name = site.get("name")
            username = site.get("username")

            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(username, str) or not username.strip():
                continue

            url = _build_url(host, username, path_suffix)
            out.append(
                {
                    "provider": provider,
                    "name": name.strip(),
                    "username": username.strip().lstrip("@"),
                    "url": url,
                }
            )

    return out


def _discover_sites(repo_root: Path, provider_filter: str | None) -> tuple[list[dict[str, Any]], str]:
    sites_path = repo_root / "sites.json"
    if not sites_path.exists():
        return [], "sites.json"
    sites_doc = _load_sites_file(sites_path)
    sites = _sites_from_sites_json(sites_doc, provider_filter)
    return sites, "sites.json"


_URL_RE = re.compile(r"https?://\S+")
_STATS_RE = re.compile(r"\b(found|downloaded|skipped)\b[^0-9]*([0-9]+)", re.IGNORECASE)
_ERROR_RE = re.compile(
    r"(\[error\]|error:|exception|traceback|http request failed|authorization|authentication|challenge)",
    re.IGNORECASE,
)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gdw", add_help=True)
    p.add_argument("url", nargs="?", help="URL to download (optional if sites.json has sites)")
    p.add_argument("--config", default="config.json", help="Path to config.json (repo-relative)")
    p.add_argument("--provider", help="Run only this provider's sites (e.g. twitter)")
    p.add_argument("--dry-run", action="store_true", help="Print commands without downloading")
    p.add_argument(
        "--add",
        nargs=3,
        metavar=("PROVIDER", "NAME", "USERNAME"),
        help="Add a site to sites.json and exit",
    )
    p.add_argument(
        "--remove",
        nargs="+",
        metavar="ITEM",
        help="Remove a site from sites.json and exit (USERNAME optional)",
    )
    p.add_argument("--host", help="Host for a new provider block (used with --add)")
    p.add_argument("--path-suffix", help="Path suffix for a new provider block (used with --add)")
    p.add_argument(
        "--import",
        dest="import_file",
        metavar="FILE",
        help="Import URLs from a file (one per line) into sites.json, then delete the file",
    )
    p.add_argument(
        "--sync",
        action="store_true",
        help="Copy sites.json to all paths in config gdw.sync_targets and exit",
    )
    p.add_argument(
        "--pinned-bar",
        action="store_true",
        help="Keep tqdm bar pinned by routing gallery-dl output through the bar",
    )
    p.add_argument(
        "--status-above-bar",
        action="store_true",
        help="Show the latest URL on a line above the progress bar (pinned mode)",
    )
    p.add_argument(
        "--no-url-postfix",
        dest="url_postfix",
        action="store_false",
        help="Disable showing the latest URL in pinned mode",
    )
    p.add_argument(
        "--errors-file",
        default="state/errors.json",
        help="Write ONLY errors here (repo-relative). Not created unless an error occurs.",
    )
    p.set_defaults(url_postfix=True)

    args, passthrough = p.parse_known_args(argv)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        raise SystemExit(2)

    repo_root = config_path.parent
    errors_path = (repo_root / Path(args.errors_file)).resolve()
    cfg = _load_config(config_path)
    gdw_cfg = cfg.get("gdw", {}) if isinstance(cfg, dict) else {}
    sites_path = repo_root / "sites.json"

    if args.sync:
        _sync_sites_json(sites_path, gdw_cfg)
        raise SystemExit(0)

    if args.import_file:
        import_path = Path(args.import_file).resolve()
        if not import_path.exists():
            print(f"Import file not found: {import_path}", file=sys.stderr)
            raise SystemExit(2)
        lines = import_path.read_text(encoding="utf-8").splitlines()
        added = 0
        skipped = 0
        for line in lines:
            parsed = _parse_import_line(line)
            if parsed is None:
                if line.strip() and not line.strip().startswith("#"):
                    print(f"skipped (unrecognised domain): {line.strip()}", file=sys.stderr)
                    skipped += 1
                continue
            provider, username = parsed
            _add_site(repo_root, provider=provider, name=username, username=username,
                      host=None, path_suffix=None)
            added += 1
        import_path.unlink()
        print(f"import done: {added} added, {skipped} skipped -- {import_path.name} deleted")
        _sync_sites_json(sites_path, gdw_cfg)
        raise SystemExit(0)

    if args.add:
        if args.remove:
            print("Use only one of --add or --remove", file=sys.stderr)
            raise SystemExit(2)
        provider, name, username = args.add
        _add_site(
            repo_root,
            provider=provider,
            name=name,
            username=username,
            host=args.host,
            path_suffix=args.path_suffix,
        )
        _sync_sites_json(sites_path, gdw_cfg)
        raise SystemExit(0)
    if args.remove:
        if len(args.remove) not in (2, 3):
            print("--remove expects PROVIDER NAME [USERNAME]", file=sys.stderr)
            raise SystemExit(2)
        provider = args.remove[0]
        name = args.remove[1]
        username = args.remove[2] if len(args.remove) == 3 else None
        _remove_site(repo_root, provider=provider, name=name, username=username)
        _sync_sites_json(sites_path, gdw_cfg)
        raise SystemExit(0)

    if args.url:
        base_dir = _resolve_base_directory(cfg, config_path)
        dest: Path | None = None
        if not _args_has_flag(passthrough, "--destination", "-d", "--dest") and not _passthrough_sets_base_directory(passthrough):
            sites, _source = _discover_sites(repo_root, None)
            target = _normalize_url_for_match(args.url)
            for site in sites:
                if _normalize_url_for_match(site.get("url", "")) == target:
                    dest = (base_dir / site["name"]).resolve()
                    break
            if dest is None:
                username = _username_from_social_url(target)
                if username:
                    dest = (base_dir / username).resolve()
        cmd = _build_gallery_dl_cmd(args.url, config_path, dest=dest, passthrough_args=passthrough)
        rc, msg = _run_gallery_dl(cmd, args.dry_run)
        if rc != 0 and msg:
            print(msg, file=sys.stderr)
        raise SystemExit(rc)

    base_dir = _resolve_base_directory(cfg, config_path)

    sites, source = _discover_sites(repo_root, args.provider)
    if not sites:
        if args.provider:
            print(f"No sites found for provider '{args.provider}' (source: {source})", file=sys.stderr)
        else:
            print(f"No sites found (source: {source})", file=sys.stderr)
        raise SystemExit(2)

    errors_doc: dict[str, Any] | None = None
    errors_dirty = False

    rc_any = 0
    if args.status_above_bar and not args.pinned_bar:
        print("Note: --status-above-bar requires --pinned-bar; enabling pinned-bar.", file=sys.stderr)
        args.pinned_bar = True
    status_bar = None
    bar_position = 0
    if args.pinned_bar and args.status_above_bar:
        status_bar = tqdm(total=0, desc="", bar_format="{desc}", dynamic_ncols=True, position=0, leave=False)
        bar_position = 1

    bar = tqdm(total=len(sites), desc="gdw", unit="site", dynamic_ncols=True, position=bar_position)
    term_columns = shutil.get_terminal_size((120, 20)).columns
    max_postfix_len = max(20, term_columns - 20)
    max_status_len = max(20, term_columns - 2)
    try:
        for item in sites:
            provider = item["provider"]
            name = item["name"]
            url = item["url"]
            label = f"{provider}/{name}"

            bar.set_postfix_str(label, refresh=True)
            bar.update(1)
            bar.write(f"[{provider}] {name}: {url}")
            if status_bar is not None:
                status_bar.set_description_str("", refresh=True)

            # gallery-dl already appends provider/username via its directory format,
            # so we only set a per-site base here to avoid double nesting.
            dest = (base_dir / name).resolve()
            cmd = _build_gallery_dl_cmd(url, config_path, dest=dest, passthrough_args=passthrough)

            if args.pinned_bar:
                found = 0
                downloaded = 0
                skipped = 0
                explicit: dict[str, int] = {}
                seen_items: set[str] = set()

                def on_line(line: str) -> None:
                    nonlocal found, downloaded, skipped
                    for match in _STATS_RE.finditer(line):
                        key = match.group(1).lower()
                        explicit[key] = int(match.group(2))

                    stripped = line.strip()
                    url_match = _URL_RE.search(line)

                    if args.url_postfix and url_match:
                        raw_url = url_match.group(0).rstrip(").,")
                        if status_bar is not None:
                            status_text = _truncate(raw_url, max_status_len)
                            status_bar.set_description_str(status_text, refresh=True)
                        else:
                            postfix = _truncate(f"{label} | {raw_url}", max_postfix_len)
                            bar.set_postfix_str(postfix, refresh=True)

                    if not stripped or stripped.startswith("["):
                        return

                    is_skipped = stripped.startswith("# ")
                    item = stripped[2:].strip() if is_skipped else stripped
                    if not item or item in seen_items:
                        return

                    seen_items.add(item)
                    found += 1
                    if is_skipped:
                        skipped += 1
                    else:
                        downloaded += 1

                    if args.url_postfix and not url_match:
                        if status_bar is not None:
                            status_text = _truncate(item, max_status_len)
                            status_bar.set_description_str(status_text, refresh=True)
                        else:
                            postfix = _truncate(f"{label} | {item}", max_postfix_len)
                            bar.set_postfix_str(postfix, refresh=True)

                rc, msg = _run_gallery_dl(
                    cmd,
                    args.dry_run,
                    force_pipe=True,
                    on_line=on_line,
                    print_lines=False,
                )
            else:
                rc, msg = _run_gallery_dl(cmd, args.dry_run)
            rc_any = rc_any or rc

            if args.pinned_bar:
                stat_parts: list[str] = []
                found_val = explicit.get("found", found)
                downloaded_val = explicit.get("downloaded", downloaded)
                skipped_val = explicit.get("skipped", skipped)
                if found_val:
                    stat_parts.append(f"{found_val} found")
                if downloaded_val:
                    stat_parts.append(f"{downloaded_val} downloaded")
                if skipped_val:
                    stat_parts.append(f"{skipped_val} skipped")
                if stat_parts:
                    bar.write(f"[{provider}] {name}: done ({', '.join(stat_parts)})")
                else:
                    bar.write(f"[{provider}] {name}: done (stats unavailable)")

            if rc != 0:
                if errors_doc is None:
                    errors_doc = _load_errors(errors_path)
                _upsert_error(errors_doc, provider, item, url, rc, msg or f"gallery-dl exited with code {rc}")
                errors_dirty = True
                if not args.dry_run:
                    _write_json(errors_path, errors_doc)
                bar.write(f"ERROR {provider}/{name} - see {Path(args.errors_file)}")
            else:
                if errors_path.exists():
                    if errors_doc is None:
                        errors_doc = _load_errors(errors_path)
                    username = item.get("username")
                    if _clear_error(errors_doc, provider, name, username if isinstance(username, str) else None):
                        errors_dirty = True
                        if not args.dry_run:
                            if errors_doc.get("errors"):
                                _write_json(errors_path, errors_doc)
                            else:
                                try:
                                    errors_path.unlink()
                                except FileNotFoundError:
                                    pass
    finally:
        bar.close()
        if status_bar is not None:
            status_bar.close()

    if errors_dirty and errors_doc is not None and not args.dry_run:
        if errors_doc.get("errors"):
            _write_json(errors_path, errors_doc)
        else:
            try:
                errors_path.unlink()
            except FileNotFoundError:
                pass

    raise SystemExit(rc_any)


if __name__ == "__main__":
    main()
