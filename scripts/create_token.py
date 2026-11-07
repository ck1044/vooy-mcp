"""Create the local MCP bearer token once without printing it."""

import argparse
import os
from pathlib import Path
import re
import secrets


parser = argparse.ArgumentParser(description="Create an MCP token without overwriting existing tokens.")
parser.add_argument("--name", default="mcp-token")
name = parser.parse_args().name
if not re.fullmatch(r"mcp-token(?:-[a-z0-9][a-z0-9_-]{0,40})?", name):
    parser.error("name must be mcp-token or mcp-token-<lowercase-name>")

directory = Path(__file__).resolve().parents[1] / ".secrets"
directory.mkdir(mode=0o700, exist_ok=True)
directory.chmod(0o700)
token_file = directory / name
try:
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print(f"기존 토큰 유지: {token_file}")
else:
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(secrets.token_urlsafe(32) + "\n")
    print(f"토큰 생성: {token_file}")
