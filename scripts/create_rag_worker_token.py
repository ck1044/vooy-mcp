"""Create a separate RAG worker credential without displaying it."""
import os
from pathlib import Path
import secrets

folder = Path(__file__).resolve().parents[1] / '.secrets'
folder.mkdir(mode=0o700, exist_ok=True)
path = folder / 'rag-worker-token'
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print('기존 RAG 작업자 토큰 유지')
else:
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(secrets.token_urlsafe(40) + '\n')
    print('RAG 작업자 토큰 생성 완료')
