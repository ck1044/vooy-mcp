# Vooy MCP 운영과 백업

## 서비스 확인

```bash
cd /home/pc/projects/vooy-mcp
docker compose ps
docker compose logs --tail=50 vooy-mcp
docker compose exec -T postgres psql -U vooy -d vooy -c 'SELECT extname, extversion FROM pg_extension WHERE extname = '\''vector'\'';'
```

원본은 `/mnt/d/VOOY/media`, PostgreSQL 데이터는 Compose의 `vooy-postgres` 볼륨에 있다. API는 3185 포트의 localhost에서 수신한다. `/healthz`는 프로세스 생존 확인이다.

## 일관된 백업

백업 스크립트는 실행 중인 Compose 서비스 중 PostgreSQL을 제외한 서비스를 정지하고 PostgreSQL custom-format dump와 `originals`, `derived`, `staging`, `imports`의 파일을 복사한다. 복사한 파일·DB의 SHA-256과 크기를 `manifest.json`에 기록하고, DB에 기록된 활성 자료의 각 원본 버전 해시와도 비교한 뒤 `.partial` 이름을 완성된 이름으로 변경한다. 성공·오류 시 정지했던 서비스를 다시 시작한다. 백업 중에는 업로드와 MCP 사용이 잠시 중단되며 그 시간은 저장된 파일 크기와 저장 장치 속도에 비례한다.

```bash
cd /home/pc/projects/vooy-mcp
python3 scripts/backup_storage.py --destination /별도/백업저장소/vooy
```

원본 저장 위치를 기본값과 다르게 설정한 경우 `--media-root /실제/미디어/경로`를 추가한다. 백업 대상은 미디어 루트 밖에 둔다. DB·파일 모두의 장애에 대비하려면 미디어와 DB가 놓인 장치와 별도의 저장장치를 대상 경로로 지정한다. 백업에는 원본과 추출 텍스트 등 자료 내용이 포함된다. `.secrets`의 MCP 토큰·DB 비밀번호는 포함되지 않는다.

일일 백업의 기본 경로는 원본 D 드라이브와 다른 C 드라이브의 `/mnt/c/Users/PC/VOOY/backups`이며, 실행 명령은 `python3 scripts/daily_backup.py`다. 실행 전 파일·DB 예상 용량의 110%와 2 GiB 여유 공간을 확인한다. 공간이 부족하면 기존 백업을 보존하고 오류를 기록한다. 백업 잠금으로 동시 실행을 막으며, 서비스 중지 기록을 남겨 중단 시 재시작한다.

매일 오전 3시의 Ubuntu 사용자 타이머를 적용하려면 다음을 실행한다.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/vooy-mcp-backup.service deploy/systemd/vooy-mcp-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vooy-mcp-backup.timer
systemctl --user list-timers vooy-mcp-backup.timer
```

`Persistent=true`로 지정되어 실행 시각에 사용자 서비스가 중지된 경우 다음 활성화 때 누락된 작업을 실행한다. 해당 시각에 Docker Desktop이 실행 중이어야 백업할 수 있다. `.secrets/backup-status.json`에 최근 성공·실패와 완료 시각이 남고 `journalctl --user -u vooy-mcp-backup.service`에서 실행 결과를 확인한다. 실패 시 기존 백업은 유지된다. `.partial`은 실패 또는 중단된 백업이다.

중단 후 서비스가 정지 상태라면 `python3 scripts/recover_backup.py`로 정지 기록에 남은 서비스를 시작한다. 타이머는 종료 후 이 복구 스크립트를 자동 실행한다.

## 별도 환경에서 복구 시험

복구 대상은 **원본 PostgreSQL과 다른 컨테이너**, 비어 있는 `vooy_restore_` 접두사의 데이터베이스와 아직 존재하지 않는 별도 미디어 경로다. `pgvector`가 포함된 PostgreSQL 16 컨테이너를 준비하고 빈 DB를 생성한 뒤 아래 명령을 실행한다.

```bash
cd /home/pc/projects/vooy-mcp
python3 scripts/restore_storage.py \
  --backup /별도/백업저장소/vooy/vooy-backup-날짜 \
  --target-db-container 복구용-postgres-컨테이너 \
  --target-db-name vooy_restore_check \
  --target-dir /별도/복구시험/media
```

스크립트는 먼저 DB dump와 모든 파일의 크기·해시를 검증하고, 대상 DB가 비어 있는지 검사한다. 그다음 DB와 파일을 복원하고 파일 해시를 다시 확인한다. 중간에 실패하면 실패한 대상 DB를 새 빈 DB로 다시 준비하고 새 복구 경로를 사용한다. 복구된 DB에서 자료 수·구간·벡터를 조회하고 미디어 SHA-256을 원본과 비교한다.

## 고정 HTTPS 주소

현재 `trycloudflare.com` 주소는 임시 테스트 주소다. 고정 주소는 Windows에서 실행되는 Cloudflare Tunnel의 공개 호스트로 `https://원하는-호스트`를 등록하고, 해당 호스트를 `http://127.0.0.1:3185`로 연결해 설정한다. `/home/pc/projects/vooy-mcp/.env`의 `VOOY_PUBLIC_BASE_URL`을 해당 HTTPS 주소로 바꾸고 앱 컨테이너를 재생성한다. 공개 호스트와 브라우저 Origin은 서버의 `VOOY_MCP_EXTRA_HOSTS`와 `VOOY_MCP_EXTRA_ORIGINS` 설정 및 터널의 Host 헤더 전달 설정에 맞춰 확인한다.

고정 호스트 설정 후 Vooy에서 `/mcp` 주소와 기존 `Authorization: Bearer …`를 사용해 다시 연결하고 업로드 링크, 8 MiB 조각, 재개, 다운로드의 Range 응답을 확인한다. 실제 호스트 생성과 터널 경로 변경은 해당 Cloudflare 계정의 터널 관리 권한이 필요하다.
