# Vooy MCP 개발·서빙 기반

Ubuntu에서 MCP 도구를 개발하고 Docker Desktop으로 실행하는 프로젝트다. 텍스트를 PostgreSQL에, 이미지·영상 등 원본을 D 드라이브에 보관한다. 서버는 Streamable HTTP를 사용하며 도구 호출에 Bearer 토큰이 필요하다.

## 저장소·RAG 개발 기획

[미디어 저장소·임베딩 연동 개발 기획서](docs/superpowers/plans/2026-11-07-vooy-media-storage.md)에 이미지·영상·텍스트 저장, PostgreSQL/pgvector 데이터 구조, 업로드 API, 외부 RAG 계약과 단계별 완료 기준을 정리했다. 최초 구현 범위는 전용 DB·파일 저장소·재개 가능한 업로드·Vooy 자료 관리다.

사진 검색의 후속 개발 기준은 [OpenRouter 직접 이미지 임베딩·ZIP 이식 기획서 v2](docs/superpowers/plans/2026-11-07-vooy-image-memory-search.md)다. ZIP의 실제 이미지를 OpenRouter 멀티모달 모델로 벡터화하고, 같은 모델의 텍스트 질문으로 검색해 Vooy에 사진을 전달하는 구조를 정의한다.

`oba_new.zip`과 기존 서버의 차이와 이식 범위는 [OBA 이식 비교](docs/oba-migration-comparison.md)에 기록한다.

## 처음 설정

```bash
cd /home/pc/projects/vooy-mcp
uv sync --locked
uv run python scripts/create_token.py
python3 scripts/create_postgres_password.py
python3 scripts/create_rag_worker_token.py
mkdir -p /mnt/d/VOOY/media
# .env에 실제 공개 주소를 VOOY_PUBLIC_BASE_URL로 설정한다.
docker compose up -d --build
uv run python scripts/check_server.py
```

기본 MCP 주소는 `http://127.0.0.1:3185/mcp`이며 상태 확인 주소는 `http://127.0.0.1:3185/healthz`다. 토큰은 `.secrets/mcp-token`에 저장한다. 토큰 생성 스크립트는 기존 토큰을 덮어쓰지 않는다. 상태 확인 주소는 프로세스 생존 여부만 표시한다.

## 자료 저장과 사용

Vooy에서 `save_text`로 짧은 텍스트를 저장하고, `create_upload`로 받은 링크에서 이미지·영상 파일을 선택한다. 업로드 화면에서 자료 ID를 확인한 뒤 `get_asset` 또는 `list_assets`로 조회한다. `get_download_link`는 15분 동안 유효한 원본 다운로드 링크를 발급한다. `update_asset_metadata`로 제목·태그·출처를 수정하고 `delete_asset`으로 목록에서 삭제한다.

단톡방 대화 저장과 사진 업로드 방법, 실제 확인 결과는 [단톡방 기록 저장 안내](docs/vooy-group-archive.md)를 참고한다. 새 파일 업로드에 출처와 태그를 함께 지정할 수 있다.

파일은 최대 10 GiB, 전송 조각은 8 MiB다. 전송을 중단한 뒤 같은 파일을 다시 선택하면 기존 조각의 해시를 확인하고 이어서 전송한다. 링크가 만료되면 `get_upload_link`로 새 링크를 발급한다. 브라우저 전송 완료 후 서버가 파일의 형식과 SHA-256을 확인하므로 저장 완료 상태까지 기다린다. 파일 원본은 `/mnt/d/VOOY/media`, PostgreSQL 데이터는 `vooy-postgres` Docker 볼륨에 보관한다.

현재 자료 관리는 단일 소유자 기준이다. 파일 삭제 시 목록과 다운로드에서 즉시 제외하고 정리 작업으로 원본·파생물·벡터를 지운다. 업로드 임시 상태는 미디어 저장소의 `staging`에 보관한다.

## 컬렉션·버전·외부 RAG

`create_collection`, `add_to_collection`, `list_collections`로 자료를 묶는다. 기존 자료의 파일을 교체할 때 `create_upload`에 `asset_id`와 현재 `expected_version_id`를 전달하면 이전 버전이 보존된다. `list_asset_versions`로 이력을 조회하고 `get_download_link`에 `version_id`를 지정해 이전 원본을 받을 수 있다. `import_from_url`은 공개 웹의 파일을 작업으로 가져오며 `get_import_status`로 확인한다.

임베딩 공간을 `register_embedding_space`로 등록하고 `request_ingestion`을 호출하면 외부 RAG 워커가 작업을 받을 수 있다. 워커는 `.secrets/rag-worker-token`의 별도 토큰을 사용한다. 초기 토큰은 `python3 scripts/create_rag_worker_token.py`로 한 번 생성한다. 구간·벡터·파생물을 수신하고 전체 검증이 끝나면 활성화한다. 질문 검색 `search_assets`는 실제 RAG 질문 인코더를 `.env`의 `VOOY_RAG_QUERY_URL`에 연결한 뒤 사용한다. 요청은 `{"query":"질문","space_id":"공간 UUID"}`, 응답은 `{"embedding":[...]}`다. 모델 실행과 인코더는 외부 RAG에서 제공한다.

작업자 API와 예시는 [RAG 연동 계약](docs/rag-contract-v1.md), 백업·복구와 운영 방법은 [운영 문서](docs/operations.md)를 참고한다. `get_backup_status`로 일일 백업 상태를 확인할 수 있다.

### OpenRouter 사진 검색

OpenRouter API 키는 `.secrets/openrouter-api-key`에 보관한다. `.env`에 `VOOY_OPENROUTER_API_KEY_FILE=/run/vooy-mcp/secrets/openrouter-api-key`를 지정하고 서버를 빌드한 뒤 `docker compose exec vooy-mcp python -m vooy_storage.setup_openrouter`로 임베딩 공간을 등록한다. 출력된 공간 ID를 `.env`의 `VOOY_EMBEDDING_SPACE_ID`에 지정하고 `docker compose --profile rag up -d`로 서버와 워커를 실행한다. 사진·텍스트가 저장되면 자동으로 색인 작업이 예약된다. `get_ingestion_status`에서 작업 상태를 확인한다.

Vooy에서 `search_photos`는 사진 결과와 MCP 이미지 미리보기를 반환한다. 선택한 `photo_id`를 `get_photo`에 전달하면 원본 이미지 콘텐츠와 임시 표시·다운로드 주소를 받는다. OBA에서 이식한 출처는 `source`, 원본 식별자는 `source_photo_id`로 표시한다. OBA 예제의 날짜·장소는 `metadata.origin=synthetic`인 가상 값이다.

검색 결과의 현재 사진을 OpenRouter 비전 모델로 분석한 `visual_observation`은 해당 이미지와 버전에 묶인 AI 관찰 설명이며, 신원·촬영 장소·사건에 대한 확정 정보가 아니다. 설명 요청이 실패하면 사진은 계속 제공한다.

영상·오디오·문서도 업로드 확정 후 자동 색인한다. `search_videos`는 영상에서 최대 48개 대표 프레임을 추출해 검색하고 장면 이미지와 해당 시각을 반환한다. 긴 영상의 간격은 늘어나며 반환 구간은 대표 프레임의 주변 범위다. `search_audio`는 최대 120초 단위의 음성을 직접 임베딩하고 검색된 시간 구간을 제공한다. `search_documents`는 PDF 페이지와 UTF-8 TXT·Markdown·JSON·DOCX의 텍스트 구간을 검색한다. `get_media`는 원본의 임시 열기 링크와 다운로드 링크를 제공한다. 재생·표시 방식은 Vooy 클라이언트의 지원 여부에 따른다.

이미지 폴더를 등록할 때는 폴더를 읽기 전용으로 연결한다. 아래 `/path/to/photos`와 데이터셋 이름은 실제 값으로 바꾼다.

```bash
docker compose run --rm --no-deps --volume /path/to/photos:/import:ro --entrypoint python vooy-mcp /app/scripts/import_image_directory.py /import --dataset photos
```

폴더 원본은 보존되며 데이터셋 이름·상대경로·파일 해시가 같은 자료는 재등록하지 않는다. HEIC 사진은 원본을 보존하고 JPEG로 변환해 임베딩·미리보기를 만든다. 미지원 파일의 작업 상태에는 실패 사유가 표시된다. 실제 OpenRouter 응답과 Vooy 화면 표시는 사용 환경에서 확인한다.

## 개발과 확인

`server.py`에서 `@server.tool()`로 함수를 추가한다. 도구마다 무엇을 하는지 설명과 입력 타입을 작성한다. 서버를 수정한 뒤 컨테이너를 다시 빌드하고 확인한다.

```bash
uv run python -m unittest discover -s tests -v
docker compose up -d --build
uv run python scripts/check_server.py
docker compose ps
docker compose logs --tail=50
```

Docker 없이 실행할 때는 `uv run python server.py`를 사용한다. Docker 서버와 같은 포트를 사용하므로 먼저 `docker compose stop`으로 Docker 서버를 중지한다. 서버는 기본적으로 `127.0.0.1:3185`에서 수신한다.

## 원격 연결

Vooy는 원격 HTTP MCP 주소와 수동 토큰을 지원한다. 안정적인 외부 HTTPS 주소는 기존 Windows Cloudflare 터널에 새 공개 경로를 추가해 연결할 수 있다. Cloudflare 경로를 추가할 때 `.env.example`을 `.env`로 복사해 공개 호스트와 필요한 Origin을 `VOOY_MCP_EXTRA_HOSTS`, `VOOY_MCP_EXTRA_ORIGINS`에 각각 쉼표로 구분해 설정한 뒤 컨테이너를 재생성한다. 예: 호스트 `mcp.example.com`, Origin `https://mcp.example.com`.

Vooy에서 수동 토큰을 사용하고 **인증 헤더 이름**에 `Authorization`을 입력한다. **인증 토큰**에는 `Bearer `와 공백을 앞에 붙여 `Bearer <token>` 형식으로 입력한다. 토큰만 입력하면 인증되지 않는다. 2026-11-07에 임시 HTTPS 주소로 Vooy 연결을 확인했다. 토큰 파일은 Git과 Docker 이미지에 포함하지 않는다.

계정마다 다른 토큰을 사용하려면 추가 토큰을 만들고 서버를 재시작한다. 추가 토큰 파일은 `.secrets/mcp-token-`으로 시작해야 한다. 예를 들어 두 번째 계정의 토큰은 다음과 같이 만든다.

```bash
uv run python scripts/create_token.py --name mcp-token-account2
docker compose restart
```

기존 토큰은 유지된다. 토큰마다 별도로 폐기할 수 있지만 현재는 모든 토큰에 같은 도구와 권한이 적용된다. 토큰 생성 스크립트는 같은 이름의 기존 파일을 덮어쓰지 않는다.

### 임시 HTTPS 테스트

Windows의 `cloudflared` Quick Tunnel을 별도 프로세스로 실행해 임시 `https://…trycloudflare.com/mcp` 주소를 만들 수 있다. 실제 주소는 Windows의 `%LOCALAPPDATA%\VooyMcp\quick-*.err.log`에서 확인한다. 이 테스트 터널은 기존 Cloudflare 서비스와 별개이며 종료하거나 다시 만들면 주소가 바뀐다. Quick Tunnel은 SSE를 지원하지 않으므로 현재 서버는 단순 호출의 응답을 JSON으로 제공한다. 고정 주소와 장기 운영에는 위의 기존 터널 공개 경로를 사용한다.

Vooy의 인증 토큰 입력란에 필요한 전체 값(`Bearer ` 접두사 포함)을 Windows 클립보드에 복사하려면 Ubuntu에서 다음 명령을 실행한 뒤 붙여넣는다.

```bash
{ printf 'Bearer '; tr -d '\r\n' < /home/pc/projects/vooy-mcp/.secrets/mcp-token; } | /mnt/c/Windows/System32/clip.exe
```

두 번째 계정의 값을 복사하려면 위 명령의 파일 이름을 `mcp-token-account2`로 바꾼다. 이 환경에는 `Bearer` 접두사가 포함된 복사용 값이 `.secrets/account2-authorization`에도 준비되어 있다.

## 중지 및 재시작

```bash
docker compose stop
docker compose start
```

`restart: unless-stopped` 설정으로 Docker 엔진이 시작될 때 컨테이너를 재시작한다. 서비스가 외부에서 계속 접근 가능하려면 Windows, Docker Desktop, Ubuntu 및 연결된 터널이 실행 중이어야 한다.
