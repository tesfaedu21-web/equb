import gzip
import os
import smtplib
import subprocess
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db, NotificationSettings, Settings
from routers.deps import _require_superadmin

logger = logging.getLogger("equb.backup")
router = APIRouter()


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_db_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "host":     p.hostname or "localhost",
        "port":     str(p.port or 5432),
        "user":     p.username or "",
        "password": p.password or "",
        "dbname":   p.path.lstrip("/"),
    }


def _do_pg_dump(label: str = "") -> str | None:
    """
    Run pg_dump and return the output file path on success, None on failure.
    label is appended to the filename (e.g. 'pre_reset', 'daily').
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        logger.warning("backup: DATABASE_URL not set")
        return None
    params = _parse_db_url(db_url)
    if not params["dbname"]:
        logger.warning("backup: could not parse DB name from DATABASE_URL")
        return None

    backup_dir = "/tmp/equb_backups"
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    out_file = os.path.join(backup_dir, f"equb{suffix}_{timestamp}.sql")

    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]
    cmd = [
        "pg_dump",
        "-h", params["host"],
        "-p", params["port"],
        "-U", params["user"],
        "-d", params["dbname"],
        "--no-password", "--format=plain", "--encoding=UTF8",
        "-f", out_file,
    ]
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            logger.error("backup: pg_dump failed: %s", r.stderr[:300])
            return None
        logger.info("backup: saved %s (%d bytes)", out_file, os.path.getsize(out_file))
        return out_file
    except FileNotFoundError:
        logger.warning("backup: pg_dump not found on this host")
        return None
    except Exception as e:
        logger.error("backup: error: %s", e)
        return None


def send_backup_email(path: str, filename: str, db: Session) -> bool:
    """
    Compress the backup file and send it as a Gmail/SMTP attachment.
    Uses the SMTP settings already configured in NotificationSettings.
    Returns True on success, False on failure. Never raises.
    """
    import io
    try:
        cfg = db.query(NotificationSettings).first()
        gs  = db.query(Settings).first()
        if not cfg or not cfg.email_enabled or not cfg.smtp_host or not cfg.email_from:
            logger.info("backup email: SMTP not configured — skipping email delivery")
            return False

        # Compress into memory — avoids writing then re-reading a temp file
        buf = io.BytesIO()
        with open(path, "rb") as f_in, gzip.open(buf, "wb") as f_out:
            f_out.write(f_in.read())
        gz_data = buf.getvalue()
        gz_size = len(gz_data)

        # Gmail has a 25 MB attachment limit — skip if over 20 MB compressed
        if gz_size > 20 * 1024 * 1024:
            logger.warning("backup email: compressed file too large (%d bytes)", gz_size)
            return False

        group_name  = (gs.group_name if gs else None) or "Equb"
        gz_filename = filename + ".gz"

        msg = EmailMessage()
        msg["Subject"] = f"[{group_name}] Database Backup — {filename}"
        msg["From"]    = cfg.email_from
        msg["To"]      = cfg.email_from   # send backup to the admin's own mailbox
        msg.set_content(
            f"Automated backup of the {group_name} database.\n\n"
            f"File   : {gz_filename}\n"
            f"Size   : {gz_size:,} bytes (compressed)\n"
            f"Time   : {_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
            "Keep this email for your records. You can restore with:\n"
            "  gunzip -c backup.sql.gz | psql <connection_string>"
        )
        msg.add_attachment(gz_data, maintype="application", subtype="gzip",
                           filename=gz_filename)

        port = cfg.smtp_port or 587
        if cfg.smtp_use_tls:
            smtp = smtplib.SMTP(cfg.smtp_host, port, timeout=30)
        else:
            smtp = smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=30)
        try:
            if cfg.smtp_use_tls:
                smtp.starttls()
            if cfg.smtp_user and cfg.smtp_password:
                smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        finally:
            smtp.quit()

        logger.info("backup email: sent %s (%d bytes) to %s", gz_filename, gz_size, cfg.email_from)
        return True
    except Exception as e:
        logger.error("backup email: failed — %s", e)
        return False


def _prune_old_backups(backup_dir: str, keep: int = 7, label: str = ""):
    """Keep only the most recent `keep` backups with the given label prefix."""
    try:
        prefix = f"equb_{label}_" if label else "equb_"
        files = sorted(
            [f for f in os.listdir(backup_dir)
             if f.startswith(prefix) and f.endswith(".sql")],
            reverse=True,
        )
        for old in files[keep:]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except OSError:
                pass
    except Exception:
        pass


@router.get("/download")
def download_backup(request: Request, db: Session = Depends(get_db)):
    """Stream a pg_dump SQL backup. Superadmin only."""
    _require_superadmin(request)
    path = _do_pg_dump("manual")
    if not path:
        raise HTTPException(500, "Backup failed — check server logs")
    timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"equb_backup_{timestamp}.sql"
    logger.info("Backup download: %s (%d bytes)", filename, os.path.getsize(path))
    return FileResponse(
        path=path,
        media_type="application/octet-stream",
        filename=filename,
    )


@router.get("/status")
def backup_status(request: Request, db: Session = Depends(get_db)):
    """Returns pg_dump availability and SMTP backup status."""
    _require_superadmin(request)
    db_url = os.environ.get("DATABASE_URL", "")
    pg_dump_available = False
    pg_version = "not found"
    try:
        r = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, timeout=5)
        pg_dump_available = r.returncode == 0
        pg_version = r.stdout.strip()
    except Exception:
        pass

    cfg = db.query(NotificationSettings).first()
    email_backup_configured = bool(
        cfg and cfg.email_enabled and cfg.smtp_host and cfg.email_from
    )
    params = _parse_db_url(db_url) if db_url else {}
    return {
        "has_database_url":        bool(db_url),
        "pg_dump_available":       pg_dump_available,
        "pg_dump_version":         pg_version,
        "host":                    params.get("host", ""),
        "dbname":                  params.get("dbname", ""),
        "email_backup_configured": email_backup_configured,
    }
