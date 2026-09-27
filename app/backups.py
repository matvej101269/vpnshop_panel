"""Encrypted, verified backups for the SQLite database and its config-encryption key."""
import argparse
import io
import os
import sqlite3
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from app.db import engine
from app.config import settings
from app.runtime_config import _key_path


def backup_directory() -> Path:
    path = Path(engine.url.database).resolve().parent / "backups"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def create_backup() -> Path:
    key = settings.backup_encryption_key
    if not key:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is not configured; refusing to write an unencrypted backup")
    encryptor = Fernet(key.encode("ascii"))
    database = Path(engine.url.database).resolve()
    with sqlite3.connect(database) as source:
        snapshot = sqlite3.connect(":memory:")
        source.backup(snapshot)
        integrity = snapshot.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("SQLite integrity verification failed")
        snapshot_path = backup_directory() / f".snapshot-{uuid.uuid4().hex}.tmp"
        with sqlite3.connect(snapshot_path) as target:
            snapshot.backup(target)
        snapshot.close()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        archive.add(snapshot_path, arcname="vpnshop.db")
        archive.add(_key_path(), arcname=".vpnshop-secret.key")
    snapshot_path.unlink(missing_ok=True)
    encrypted = encryptor.encrypt(payload.getvalue())
    filename = datetime.now(timezone.utc).strftime("vpnshop-%Y%m%d-%H%M%S-%f.vpbak")
    destination = backup_directory() / filename
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(encrypted)
    # Verify the encrypted artifact can be decrypted and the embedded database is sound.
    verified = encryptor.decrypt(destination.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(verified), mode="r:gz") as archive:
        db_member = archive.extractfile("vpnshop.db")
        if db_member is None:
            destination.unlink(missing_ok=True)
            raise RuntimeError("Backup verification failed: database missing")
        temp = sqlite3.connect(":memory:")
        temp.deserialize(db_member.read())
        if temp.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            destination.unlink(missing_ok=True)
            raise RuntimeError("Backup verification failed: invalid database")
        temp.close()
    backups = sorted(backup_directory().glob("vpnshop-*.vpbak"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in backups[14:]:
        old.unlink(missing_ok=True)
    return destination


def restore_backup(path: str) -> None:
    """Offline restore. Stop the app first; restore both DB and key from the same archive."""
    key = settings.backup_encryption_key
    if not key:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is not configured")
    decrypted = Fernet(key.encode("ascii")).decrypt(Path(path).read_bytes())
    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        if set(members) != {"vpnshop.db", ".vpnshop-secret.key"}:
            raise RuntimeError("Unexpected backup contents")
        database_data = archive.extractfile(members["vpnshop.db"]).read()
        check_db = sqlite3.connect(":memory:")
        check_db.deserialize(database_data)
        integrity = check_db.execute("PRAGMA integrity_check").fetchone()[0]
        check_db.close()
        if integrity != "ok":
            raise RuntimeError("Backup database integrity check failed")
        database = Path(engine.url.database).resolve()
        key_path = _key_path()
        database_tmp = database.with_suffix(database.suffix + ".restore.tmp")
        key_tmp = key_path.with_suffix(key_path.suffix + ".restore.tmp")
        database_tmp.write_bytes(database_data)
        key_tmp.write_bytes(archive.extractfile(members[".vpnshop-secret.key"]).read())
        os.replace(database_tmp, database)
        os.replace(key_tmp, key_path)
        os.chmod(key_path, 0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", metavar="FILE")
    args = parser.parse_args()
    if args.restore:
        restore_backup(args.restore)
        print("Restored. Restart VPN Shop.")
    else:
        print(create_backup())
