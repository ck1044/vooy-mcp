"""A small authenticated Streamable HTTP MCP server for local development."""

import hmac
import logging
import os
import json
import re
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
import uvicorn


ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("uvicorn.error")


class BearerTokenMiddleware:
    """Protect the MCP endpoint while leaving the local liveness check available."""

    def __init__(self, app, tokens: tuple[str, ...]):
        self.app = app
        self.tokens = tuple(token.encode("utf-8") for token in tokens)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
            scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")
        ):
            authorization = Headers(scope=scope).getlist("authorization")
            supplied = authorization[0] if len(authorization) == 1 else ""
            bearer_supplied = supplied.startswith("Bearer ")
            supplied_token = supplied[7:].encode("utf-8") if bearer_supplied else b""
            matches = [hmac.compare_digest(supplied_token, token) for token in self.tokens]
            valid = bearer_supplied and any(matches)
            if not valid:
                if len(authorization) > 1:
                    credential_format = "duplicate"
                elif not supplied:
                    credential_format = "missing"
                elif supplied.startswith("Bearer Bearer "):
                    credential_format = "duplicate-bearer"
                elif supplied.startswith("Bearer "):
                    credential_format = "bearer"
                else:
                    credential_format = "other"
                logger.warning(
                    "MCP authentication rejected: method=%s format=%s",
                    scope.get("method"),
                    credential_format,
                )
                response = JSONResponse(
                    {"error": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            logger.info("MCP authentication accepted: method=%s", scope.get("method"))
            received = bytearray()
            async def traced_receive():
                event = await receive()
                if event['type'] == 'http.request' and len(received) <= 262144:
                    received.extend(event.get('body', b''))
                    if not event.get('more_body', False) and len(received) <= 262144:
                        try:
                            message = json.loads(received)
                            method = message.get('method') if isinstance(message, dict) else None
                            params = message.get('params') if isinstance(message, dict) else None
                            name = params.get('name') if isinstance(params, dict) else None
                            if isinstance(method, str) and re.fullmatch(r'[a-zA-Z0-9_./-]{1,80}', method):
                                if isinstance(name, str) and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', name):
                                    logger.info('MCP request method=%s tool=%s', method, name)
                                else:
                                    logger.info('MCP request method=%s', method)
                        except (ValueError, UnicodeError):
                            pass
                        received.clear()
                return event
            await self.app(scope, traced_receive, send)
            return
        await self.app(scope, receive, send)


def read_token_file(token_path: Path) -> str:
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("MCP token file could not be read") from exc
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise RuntimeError("MCP token must contain at least 32 non-whitespace ASCII characters")
    return token


def read_tokens() -> tuple[str, ...]:
    token_path = Path(
        os.environ.get("VOOY_MCP_TOKEN_FILE", str(ROOT / ".secrets" / "mcp-token"))
    )
    extra_dir = Path(
        os.environ.get("VOOY_MCP_EXTRA_TOKEN_DIR", str(ROOT / ".secrets"))
    )
    paths = [token_path]
    if extra_dir.is_dir():
        paths.extend(path for path in sorted(extra_dir.glob("mcp-token-*")) if path.is_file())
    return tuple(read_token_file(path) for path in paths)


def extra_values(name: str) -> list[str]:
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def create_app(tokens: tuple[str, ...]):
    database_url = os.environ.get("VOOY_DATABASE_URL")
    if database_url:
        description = "사진·영상·음성·문서·텍스트를 저장하고 조회·다운로드하며 컬렉션으로 관리합니다."
        instructions = (
            "이 서버는 개인 미디어 저장소입니다. 사진·영상·음성·문서는 create_upload로 "
            "업로드 링크를 만들고, 텍스트는 save_text로 저장합니다. list_assets는 제목·태그·"
            "텍스트 본문을 검색하며, get_download_link는 원본 다운로드 링크를 발급합니다. "
            "단톡방 대화는 실제로 읽을 수 있는 내용만 save_text로 저장합니다. 사진 원본이나 "
            "접근 가능한 주소가 전달되지 않았다면 create_upload 링크를 안내하고 사용자가 "
            "파일을 선택해 업로드한 후 완료 상태를 확인합니다. "
            "컬렉션으로 자료를 묶을 수 있습니다. 사진 의미 검색은 search_photos를 호출해 "
            "결과의 이미지 콘텐츠와 preview_content_index로 미리보기를 보여주고, 선택한 photo_id는 "
            "get_photo로 원본 이미지와 임시 image_url을 받습니다. BTS 갤러리 검색에는 "
            "gallery_id=bts-daily를 사용하고 다른 사진은 해당 컬렉션을 확인합니다. 출처는 표시하고 메타데이터의 "
            "origin이 synthetic이면 가상 날짜·장소임을 명시합니다. 사진 설명은 현재 결과의 "
            "visual_observation에만 근거하고 과거 대화의 다른 사진 설명을 이어 붙이지 않습니다. "
            "시각 설명이 없으면 사진에 보이는 것을 추정하지 않습니다. 이미지 첨부나 메시지 전송 성공은 "
            "사용 중인 서비스의 실제 결과로 확인합니다. 영상은 search_videos, 음성은 search_audio, "
            "문서는 search_documents로 관련 구간을 찾고 get_media로 원본을 엽니다. "
            "새 도구가 보이지 않으면 search_assets에 kinds로 video, audio 또는 document를 지정할 수 있습니다."
        )
    else:
        description = "MCP 서버의 연결 상태를 확인합니다."
        instructions = "ping 도구로 MCP 연결 상태를 확인합니다. 저장소는 아직 연결되지 않았습니다."
    server = MCPServer(
        "vooy-mcp",
        title="Vooy 미디어 저장소",
        description=description,
        instructions=instructions,
    )

    @server.tool(title="MCP 연결 확인")
    def ping(message: str = "안녕하세요") -> str:
        """MCP 연결 상태를 확인하고 입력한 메시지를 돌려줍니다."""
        return f"MCP 연결 확인: {message}"

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return JSONResponse({"status": "ok"})

    extra_routes = []
    if database_url:
        from vooy_storage.repository import Repository
        from vooy_storage.uploads import UploadManager
        from vooy_storage.downloads import DownloadManager
        from vooy_storage.mcp_tools import register_storage_tools
        from vooy_storage.imports import ImportManager
        from vooy_storage.rag import RAGService, worker_routes
        from vooy_storage.query_encoder import QueryEncoder
        from vooy_storage.artifact_downloads import ArtifactDownloads
        from vooy_storage.cleanup import CleanupManager

        password_file = os.environ.get("VOOY_DATABASE_PASSWORD_FILE")
        password = Path(password_file).read_text(encoding="utf-8").strip() if password_file else None
        media_root = os.environ.get("VOOY_MEDIA_ROOT", "/data/media")
        public_url = os.environ.get("VOOY_PUBLIC_BASE_URL", "http://127.0.0.1:3185")
        repository = Repository(database_url, media_root, password)
        repository.init_schema()
        rag = RAGService(repository)
        rag.init_schema()
        automatic_space = os.environ.get('VOOY_EMBEDDING_SPACE_ID')
        if automatic_space:
            from vooy_storage.auto_ingestion import AutoIngestion
            ingestion = AutoIngestion(repository, automatic_space,
                                      os.environ.get('VOOY_RAG_PIPELINE_REVISION', 'openrouter-image-v1'))
            ingestion.start()
        cleanup = CleanupManager(repository)
        cleanup.start()
        uploads = UploadManager(repository, media_root, public_url)
        downloads = DownloadManager(repository, public_url, tokens[0])
        artifact_downloads = ArtifactDownloads(rag, downloads)
        imports = ImportManager(repository, media_root)
        encoder_token_file = os.environ.get('VOOY_RAG_QUERY_TOKEN_FILE')
        encoder_token = Path(encoder_token_file).read_text(encoding='utf-8').strip() if encoder_token_file else None
        openrouter_key_file = os.environ.get('VOOY_OPENROUTER_API_KEY_FILE')
        captioner = None
        if openrouter_key_file:
            from vooy_storage.openrouter import OpenRouterEmbeddings, OpenRouterQueryEncoder
            from vooy_storage.photo_caption import PhotoCaptioner
            key = Path(openrouter_key_file).read_text(encoding='utf-8').strip()
            captioner = PhotoCaptioner(key)
            encoder = OpenRouterQueryEncoder(OpenRouterEmbeddings(
                key,
                model=os.environ.get('VOOY_EMBEDDING_MODEL', 'google/gemini-embedding-2'),
                dimensions=int(os.environ.get('VOOY_EMBEDDING_DIMENSIONS', '1536')),
                provider=os.environ.get('VOOY_EMBEDDING_PROVIDER') or None,
            ))
        else:
            encoder = QueryEncoder(os.environ.get('VOOY_RAG_QUERY_URL'), encoder_token)
        default_pipeline = os.environ.get('VOOY_RAG_PIPELINE_REVISION') or (
            'openrouter-image-v1' if openrouter_key_file else 'v1')
        register_storage_tools(server, repository, uploads, downloads, imports, rag, encoder, artifact_downloads,
                               default_pipeline_revision=default_pipeline, captioner=captioner)
        extra_routes = uploads.routes() + downloads.routes() + artifact_downloads.routes()
        worker_token_file = os.environ.get('VOOY_RAG_WORKER_TOKEN_FILE')
        if worker_token_file:
            worker_token = read_token_file(Path(worker_token_file))
            extra_routes += worker_routes(rag, worker_token, public_url)

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*"] + extra_values("VOOY_MCP_EXTRA_HOSTS"),
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"]
        + extra_values("VOOY_MCP_EXTRA_ORIGINS"),
    )
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=262144,
        transport_security=security,
    )
    app.add_middleware(BearerTokenMiddleware, tokens=tokens)
    app.router.routes.extend(extra_routes)
    return app


if __name__ == "__main__":
    uvicorn.run(
        create_app(read_tokens()),
        host=os.environ.get("VOOY_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("VOOY_MCP_PORT", "3185")),
        access_log=False,
    )
