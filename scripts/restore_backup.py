"""Restore a backup into an EMPTY, migrated database:  python -m scripts.restore_backup backup-XXXX.json.gz"""
import gzip
import json
import sys

from app import db as dbm
from app.services import backup

if __name__ == "__main__":
    data = json.loads(gzip.decompress(open(sys.argv[1], "rb").read()))
    with dbm.session_scope() as s:
        print(backup.restore(s, data))
