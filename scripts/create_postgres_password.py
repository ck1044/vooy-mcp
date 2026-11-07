"""Create the database password once without printing or replacing it."""

import os
from pathlib import Path
import secrets


directory = Path(__file__).resolve().parents[1] / ".secrets"
directory.mkdir(mode=0o700, exist_ok=True)
directory.chmod(0o700)
password_file = directory / "postgres-password"
try:
    descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
except FileExistsError:
    print(f"기존 데이터베이스 암호 유지: {password_file}")
else:
    os.fchmod(descriptor, 0o640)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(secrets.token_urlsafe(32) + "\n")
    print(f"데이터베이스 암호 생성: {password_file}")
