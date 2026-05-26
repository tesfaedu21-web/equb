import os
import subprocess
import tempfile
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from database import get_db
from routers.deps import _require_superadmin

logger = logging.getLogger("equb.backup")
router = APIRouter()


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_db_url(url: str) -> dict:
    """Parse a postgresql://user:pass@host:port/dbname URL into pg_dump args."""
    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": str(p.port or 5432),
        "user": p.username or "",
        "password": p.password or "",
        "dbname": p.path.lstrip("/"),
    }


@router.get("/download")
def download_backup(request: Request, db: Session = Depends(get_db)):
    """
    Stream a pg_dump SQL backup of the database.
    Requires superadmin role.
    """
    _require_superadmin(request, db)

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise HTTPException(500, "DATABASE_URL is not set — cannot run pg_dump")

    params = _parse_db_url(db_url)
    if not params["dbname"]:
        raise HTTPException(500, "Could not parse database name from DATABASE_URL")

    timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"equb_backup_{timestamp}.sql"

    tmp = tempfile.NamedTemporaryFile(suffix=".sql", delete=False)
    tmp.close()

    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    cmd = [
        "pg_dump",
        "-h", params["host"],
        "-p", params["port"],
        "-U", params["user"],
        "-d", params["dbname"],
        "--no-password",
        "--format=plain",
        "--encoding=UTF8",
        "-f", tmp.name,
    ]

    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            logger.error("pg_dump failed: %s", result.stderr)
            raise HTTPException(500, f"pg_dump failed: {result.stderr[:500]}")
    except FileNotFoundError:
        raise HTTPException(500, "pg_dump not found — ensure PostgreSQL client tools are installed")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Backup timed out after 120 seconds")

    logger.info("Backup created: %s (%d bytes)", filename, os.path.getsize(tmp.name))
    return FileResponse(
        path=tmp.name,
        media_type="application/octet-stream",
        filename=filename,
        background=None,
    )


@router.get("/status")
def backup_status(request: Request, db: Session = Depends(get_db)):
    """Returns whether pg_dump is available and basic DB info."""
    _require_superadmin(request, db)
    db_url = os.environ.get("DATABASE_URL", "")
    has_url = bool(db_url)
    pg_dump_available = False
    try:
        r = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, timeout=5)
        pg_dump_available = r.returncode == 0
        pg_version = r.stdout.strip()
    except Exception:
        pg_version = "not found"

    params = _parse_db_url(db_url) if db_url else {}
    return {
        "has_database_url": has_url,
        "pg_dump_available": pg_dump_available,
        "pg_dump_version": pg_version,
        "host": params.get("host", ""),
        "dbname": params.get("dbname", ""),
    }
