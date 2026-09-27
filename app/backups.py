"""Encrypted, verified backups for the application database and config key."""
import argparse
import io
import os
import sqlite3
import subprocess
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from app.db import engine
from app.config import settings
from app.runtime_config import _key_path


def backup_directory() -> Path:
    path = Path("data/backups").resolve()
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def _pgrestore_env() -> dict[str, str]:
    env = os.environ.copy()
    if engine.url.password:
        env["PGPASSWORD"] = engine.url.password
    return env


def _pg_connection_args() -> list[str]:
    url = engine.url.set(drivername="postgresql", password=None)
    return ["--dbname", url.render_as_string(hide_password=False)]


def _make_database_dump(destination: Path) -> str:
    if _is_postgres():
        subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--file", str(destination),
                        *_pg_connection_args()], check=True, capture_output=True, text=True,
                       env=_pgrestore_env(), timeout=3600)
        subprocess.run(["pg_restore", "--list", str(destination)], check=True, capture_output=True,
                       text=True, env=_pgrestore_env(), timeout=120)
        return "vpnshop.pgdump"

    database = Path(engine.url.database).resolve()
    with sqlite3.connect(database) as source:
        snapshot = sqlite3.connect(":memory:")
        source.backup(snapshot)
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            snapshot.close()
            raise RuntimeError("SQLite integrity verification failed")
        with sqlite3.connect(destination) as target:
            snapshot.backup(target)
        snapshot.close()
    return "vpnshop.db"


def create_backup() -> Path:
    key = settings.backup_encryption_key
    if not key:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is not configured; refusing to write an unencrypted backup")
    encryptor = Fernet(key.encode("ascii"))
    directory = backup_directory()
    snapshot_path = directory / f".snapshot-{uuid.uuid4().hex}.tmp"
    try:
        database_arcname = _make_database_dump(snapshot_path)
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            archive.add(snapshot_path, arcname=database_arcname)
            archive.add(_key_path(), arcname=".vpnshop-secret.key")
    finally:
        snapshot_path.unlink(missing_ok=True)

    encrypted = encryptor.encrypt(payload.getvalue())
    filename = datetime.now(timezone.utc).strftime("vpnshop-%Y%m%d-%H%M%S-%f.vpbak")
    destination = directory / filename
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(encrypted)
    verified = encryptor.decrypt(destination.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(verified), mode="r:gz") as archive:
        database_member = "vpnshop.pgdump" if _is_postgres() else "vpnshop.db"
        dump = archive.extractfile(database_member)
        if dump is None:
            destination.unlink(missing_ok=True)
            raise RuntimeError("Backup verification failed: database dump missing")
        verify_path = directory / f".verify-{uuid.uuid4().hex}.tmp"
        try:
            verify_path.write_bytes(dump.read())
            if _is_postgres():
                subprocess.run(["pg_restore", "--list", str(verify_path)], check=True,
                               capture_output=True, text=True, env=_pgrestore_env(), timeout=120)
            else:
                with sqlite3.connect(":memory:") as check_db:
                    check_db.deserialize(verify_path.read_bytes())
                    if check_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Backup verification failed: invalid SQLite database")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            verify_path.unlink(missing_ok=True)
    backups = sorted(directory.glob("vpnshop-*.vpbak"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in backups[14:]:
        old.unlink(missing_ok=True)
    return destination


def restore_backup(path: str) -> None:
    """Offline restore. Stop app, bot, workers and scheduler before restoring."""
    key = settings.backup_encryption_key
    if not key:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is not configured")
    decrypted = Fernet(key.encode("ascii")).decrypt(Path(path).read_bytes())
    expected_db = "vpnshop.pgdump" if _is_postgres() else "vpnshop.db"
    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        if set(members) != {expected_db, ".vpnshop-secret.key"}:
            raise RuntimeError("Backup contents do not match the configured database type")
        db_data = archive.extractfile(members[expected_db]).read()
        key_data = archive.extractfile(members[".vpnshop-secret.key"]).read()

    directory = backup_directory()
    dump_path = directory / f".restore-{uuid.uuid4().hex}.tmp"
    key_path = _key_path()
    key_tmp = key_path.with_suffix(key_path.suffix + ".restore.tmp")
    try:
        dump_path.write_bytes(db_data)
        key_tmp.write_bytes(key_data)
        if _is_postgres():
            subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner",
                            *_pg_connection_args(), str(dump_path)], check=True,
                           capture_output=True, text=True, env=_pgrestore_env(), timeout=3600)
        else:
            with sqlite3.connect(":memory:") as check_db:
                check_db.deserialize(db_data)
                if check_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup database integrity check failed")
            database = Path(engine.url.database).resolve()
            database_tmp = database.with_suffix(database.suffix + ".restore.tmp")
            database_tmp.write_bytes(db_data)
            os.replace(database_tmp, database)
        os.replace(key_tmp, key_path)
        os.chmod(key_path, 0o600)
    finally:
        dump_path.unlink(missing_ok=True)
        key_tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", metavar="FILE")
    args = parser.parse_args()
    if args.restore:
        restore_backup(args.restore)
        print("Restored. Restart all VPN Shop services.")
    else:
        print(create_backup())
