import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def open_db(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_dir / "gdw.sqlite3")
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS download_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            site_name TEXT NOT NULL,
            username TEXT,
            url TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            exit_code INTEGER,
            error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS downloaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER REFERENCES download_runs(id) ON DELETE SET NULL,
            provider TEXT NOT NULL,
            site_name TEXT NOT NULL,
            username TEXT,
            source_url TEXT NOT NULL,
            archive_key TEXT,
            path TEXT NOT NULL,
            size_bytes INTEGER,
            mtime_ns INTEGER,
            sha256 TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            missing_at TEXT,
            UNIQUE(path)
        );
        CREATE INDEX IF NOT EXISTS idx_downloaded_files_site
            ON downloaded_files(provider, site_name, username);
        CREATE INDEX IF NOT EXISTS idx_downloaded_files_archive_key
            ON downloaded_files(archive_key);
    """)
    conn.commit()


def create_run(
    conn: sqlite3.Connection,
    provider: str,
    site_name: str,
    username: str | None,
    url: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO download_runs (provider, site_name, username, url, started_at) VALUES (?, ?, ?, ?, ?)",
        (provider, site_name, username, url, _utc_now_iso()),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    exit_code: int,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "UPDATE download_runs SET finished_at = ?, exit_code = ?, error_message = ? WHERE id = ?",
        (_utc_now_iso(), exit_code, error_message or None, run_id),
    )
    conn.commit()


def sync_site_files(
    conn: sqlite3.Connection,
    run_id: int,
    provider: str,
    site_name: str,
    username: str | None,
    dest_dir: Path,
    source_url: str = "",
) -> None:
    """Scan dest_dir after a successful run; upsert file records and mark missing files."""
    now = _utc_now_iso()
    present: set[str] = set()

    if dest_dir.exists():
        for fpath in dest_dir.rglob("*"):
            if not fpath.is_file():
                continue
            path_str = str(fpath)
            stat = fpath.stat()
            present.add(path_str)
            conn.execute(
                """
                INSERT INTO downloaded_files
                    (run_id, provider, site_name, username, source_url, path,
                     size_bytes, mtime_ns, first_seen_at, last_seen_at, missing_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(path) DO UPDATE SET
                    run_id = excluded.run_id,
                    size_bytes = excluded.size_bytes,
                    mtime_ns = excluded.mtime_ns,
                    last_seen_at = excluded.last_seen_at,
                    missing_at = NULL
                """,
                (run_id, provider, site_name, username, source_url, path_str,
                 stat.st_size, stat.st_mtime_ns, now, now),
            )

    # Mark previously known files for this site as missing if they are no longer present
    rows = conn.execute(
        """
        SELECT id, path FROM downloaded_files
        WHERE provider = ? AND site_name = ?
          AND (username IS ? OR username = ?)
          AND missing_at IS NULL
        """,
        (provider, site_name, username, username),
    ).fetchall()

    for row in rows:
        if row["path"] not in present:
            conn.execute(
                "UPDATE downloaded_files SET missing_at = ? WHERE id = ?",
                (now, row["id"]),
            )

    conn.commit()


def audit_site(
    conn: sqlite3.Connection,
    provider: str,
    site_name: str,
    username: str | None,
    dest_dir: Path,
) -> dict[str, Any]:
    """Return audit data for one site: missing known files and orphan local files."""
    rows = conn.execute(
        """
        SELECT path, missing_at FROM downloaded_files
        WHERE provider = ? AND site_name = ?
          AND (username IS ? OR username = ?)
        """,
        (provider, site_name, username, username),
    ).fetchall()

    manifest_paths: set[str] = set()
    missing: list[str] = []
    for row in rows:
        manifest_paths.add(row["path"])
        if row["missing_at"] is not None:
            missing.append(row["path"])

    orphans: list[str] = []
    dest_exists = dest_dir.exists()
    if dest_exists:
        for fpath in dest_dir.rglob("*"):
            if fpath.is_file() and str(fpath) not in manifest_paths:
                orphans.append(str(fpath))

    return {
        "provider": provider,
        "site_name": site_name,
        "username": username,
        "dest": str(dest_dir),
        "dest_exists": dest_exists,
        "manifest_files": len(manifest_paths),
        "present_files": len(manifest_paths) - len(missing),
        "missing_files": len(missing),
        "orphan_files": len(orphans),
        "missing": sorted(missing),
        "orphans": sorted(orphans),
    }
