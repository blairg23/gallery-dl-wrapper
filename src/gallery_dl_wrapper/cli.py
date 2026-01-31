import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _cmd(url: str, config_path: Path) -> list[str]:
    return ["gallery-dl", "--ignore-config", "--config", str(config_path), url]


def run_gallery_dl(url: str, config_path: Path, dry_run: bool) -> int:
    cmd = _cmd(url, config_path)
    if dry_run:
        print(" ".join(cmd))
        return 0
    return subprocess.call(cmd)


def _load_config(config_path: Path) -> dict[str, Any]:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read config: {e}", file=sys.stderr)
        raise SystemExit(2)


def _sites_for_provider(cfg: dict[str, Any], provider: str) -> list[str]:
    ex = cfg.get("extractor", {})
    if not isinstance(ex, dict):
        return []
    prov = ex.get(provider, {})
    if not isinstance(prov, dict):
        return []
    gdw = prov.get("_gdw", {})
    if not isinstance(gdw, dict):
        return []
    sites = gdw.get("sites", [])
    if isinstance(sites, list) and all(isinstance(x, str) for x in sites):
        return sites
    return []


def _all_provider_sites(cfg: dict[str, Any]) -> list[str]:
    ex = cfg.get("extractor", {})
    if not isinstance(ex, dict):
        return []
    out: list[str] = []
    for v in ex.values():
        if not isinstance(v, dict):
            continue
        gdw = v.get("_gdw", {})
        if not isinstance(gdw, dict):
            continue
        sites = gdw.get("sites", [])
        if isinstance(sites, list) and all(isinstance(x, str) for x in sites):
            out.extend(sites)
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gdw")
    p.add_argument("url", nargs="?", help="URL to download (optional if config has provider site lists)")
    p.add_argument("--config", default="config.json", help="Path to config.json (repo-relative)")
    p.add_argument("--provider", help="Run only this extractor provider's _gdw.sites (e.g. twitter, pixiv)")
    p.add_argument("--dry-run", action="store_true", help="Print commands without downloading")
    args = p.parse_args(argv)

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        raise SystemExit(2)

    if args.url:
        raise SystemExit(run_gallery_dl(args.url, config_path, args.dry_run))

    cfg = _load_config(config_path)

    if args.provider:
        sites = _sites_for_provider(cfg, args.provider)
        if not sites:
            print(f'No sites found at extractor.{args.provider}._gdw.sites', file=sys.stderr)
            raise SystemExit(2)
    else:
        sites = _all_provider_sites(cfg)
        if not sites:
            print('No sites found under extractor.<provider>._gdw.sites', file=sys.stderr)
            raise SystemExit(2)

    rc = 0
    for u in sites:
        rc = rc or run_gallery_dl(u, config_path, args.dry_run)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
