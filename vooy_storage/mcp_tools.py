"""MCP tools for the media repository."""

import json
import os
import re
from pathlib import Path
from mcp_types import CallToolResult


def _is_bts_gallery_browse(query):
    if not isinstance(query, str):
        return False
    compact = re.sub(r'\s+', '', query.casefold()).strip('?!.,。！？')
    return bool(re.fullmatch(r'(?:bts|방탄소년단)(?:의)?(?:사진|이미지)(?:들|을|들을)?(?:좀)?(?:보여줘|보여줘요|보여주세요|찾아줘|찾아주세요|줘|목록)?', compact))

def public_asset(asset):
    if asset is None:
        raise ValueError("자료를 찾을 수 없습니다.")
    return {key: value for key, value in asset.items() if key not in ("object_key", "storage_backend")}


def register_storage_tools(server, repository, uploads, downloads, imports=None, rag=None, encoder=None, artifact_downloads=None,
                           default_pipeline_revision='v1', captioner=None):
    @server.tool(title="텍스트 저장")
    def save_text(title: str, text: str, tags: list[str] | None = None,
                  source: str | None = None, request_key: str | None = None) -> dict:
        """메모, 이야기 또는 읽을 수 있는 단톡방 대화를 제목·태그·출처와 함께 저장하고 자료 ID를 반환합니다."""
        return public_asset(repository.save_text(title, text, tags, source, request_key))

    @server.tool(title="사진·영상·파일 업로드")
    def create_upload(title: str, kind: str, asset_id: str | None = None,
                      expected_version_id: str | None = None,
                      tags: list[str] | None = None, source: str | None = None) -> dict:
        """사진·영상·음성·문서를 보관할 업로드 링크를 만듭니다. kind는 image, video, audio, document 중 하나입니다. 새 자료에는 태그와 출처를 지정할 수 있습니다. 파일이나 접근 가능한 주소를 받지 못했으면 링크를 안내해 사용자가 직접 선택하도록 하고 get_upload_status로 완료를 확인하세요. 원본 교체에는 자료 ID와 현재 버전 ID를 함께 입력합니다."""
        if tags is not None or source is not None:
            return uploads.create_upload(title, kind, asset_id, expected_version_id,
                                         tags=tags, source=source)
        if asset_id or expected_version_id:
            return uploads.create_upload(title, kind, asset_id, expected_version_id)
        return uploads.create_upload(title, kind)

    @server.tool(title="업로드 상태 확인")
    def get_upload_status(upload_id: str) -> dict:
        """업로드 진행 상태와 저장이 완료된 자료의 ID를 확인합니다."""
        return uploads.get_status(upload_id)

    @server.tool(title="업로드 링크 다시 받기")
    def get_upload_link(upload_id: str) -> dict:
        """진행 중인 업로드를 다시 열 수 있는 새 일회성 링크를 발급합니다."""
        return uploads.issue_link(upload_id)

    @server.tool(title="업로드 저장 완료")
    def finalize_upload(upload_id: str) -> dict:
        """전송을 마친 파일의 크기와 형식을 확인하고 저장 확정을 요청합니다."""
        return uploads.request_complete(upload_id)

    @server.tool(title="자료 상세 조회")
    def get_asset(asset_id: str, version_id: str | None = None) -> dict:
        """자료 ID로 제목·유형·태그·파일 정보와 텍스트 내용을 조회합니다. 버전 ID를 지정하면 해당 원본 버전을 조회합니다."""
        asset = public_asset(repository.get_asset(asset_id, version_id) if version_id else repository.get_asset(asset_id))
        if rag is not None and asset.get('kind') == 'image':
            asset['image_metadata'] = rag.image_metadata(asset_id, asset['version_id'])
        return asset

    @server.tool(title="저장된 자료 찾기")
    def list_assets(query: str | None = None, kind: str | None = None,
                    tags: list[str] | None = None, limit: int = 20,
                    cursor: str | None = None, collection_id: str | None = None) -> dict:
        """저장된 자료의 제목·태그·텍스트 본문에서 문자를 검색하고 유형이나 컬렉션으로 범위를 좁힙니다. 사진 속 내용의 의미 검색은 별도 RAG 연결이 필요합니다."""
        params = dict(query=query, kind=kind, tags=tags, limit=limit, cursor=cursor)
        if collection_id:
            params['collection_id'] = collection_id
        result = repository.list_assets(**params)
        result["assets"] = [public_asset(asset) for asset in result["assets"]]
        return result

    @server.tool(title="원본 다운로드")
    def get_download_link(asset_id: str, version_id: str | None = None) -> dict:
        """사진·영상·문서·텍스트 원본의 다운로드 링크를 발급합니다. 링크는 15분간 유효하며 만료 후 다시 받을 수 있습니다."""
        link = downloads.link(asset_id, version_id) if version_id else downloads.link(asset_id)
        if link is None:
            raise ValueError("자료를 찾을 수 없습니다.")
        return link

    @server.tool(title="자료 정보 수정")
    def update_asset_metadata(asset_id: str, title: str | None = None,
                              tags: list[str] | None = None, source: str | None = None) -> dict:
        """저장된 자료의 제목·태그·출처를 수정합니다."""
        return public_asset(repository.update_asset(asset_id, title, tags, source))

    @server.tool(title="자료 삭제")
    def delete_asset(asset_id: str) -> dict:
        """자료를 목록과 검색에서 제외하고 원본과 관련 검색 데이터를 삭제 처리합니다."""
        if not repository.delete_asset(asset_id):
            raise ValueError("자료를 찾을 수 없습니다.")
        return {"id": asset_id, "deleted": True}

    @server.tool(title="원본 버전 이력 보기")
    def list_asset_versions(asset_id: str) -> dict:
        """교체된 원본을 포함해 자료의 버전 이력을 조회합니다."""
        return {'versions': [public_asset(item) for item in repository.list_versions(asset_id)]}

    @server.tool(title="컬렉션 만들기")
    def create_collection(name: str, description: str | None = None) -> dict:
        """사진·영상·메모를 함께 정리할 컬렉션을 만듭니다."""
        return repository.create_collection(name, description)

    @server.tool(title="컬렉션 목록 보기")
    def list_collections() -> dict:
        """만들어 둔 컬렉션의 이름과 정보를 조회합니다."""
        return {'collections': repository.list_collections()}

    @server.tool(title="컬렉션에 자료 추가")
    def add_to_collection(collection_id: str, asset_id: str) -> dict:
        """기존 사진·영상·텍스트를 선택한 컬렉션에 추가합니다."""
        if not repository.add_to_collection(collection_id, asset_id):
            raise ValueError('자료 또는 컬렉션을 찾을 수 없습니다.')
        return {'collection_id': collection_id, 'asset_id': asset_id, 'added': True}

    @server.tool(title="컬렉션에서 자료 빼기")
    def remove_from_collection(collection_id: str, asset_id: str) -> dict:
        """컬렉션과 자료의 연결을 제거합니다. 자료 원본은 저장소에 남습니다."""
        return {'collection_id': collection_id, 'asset_id': asset_id,
                'removed': repository.remove_from_collection(collection_id, asset_id)}

    if imports is not None:
        @server.tool(title="공개 링크에서 파일 가져오기")
        def import_from_url(title: str, url: str) -> dict:
            """공개된 웹 주소에서 사진·영상·음성·문서를 가져오는 작업을 시작합니다. 진행 상태는 get_import_status로 확인합니다."""
            return imports.create(title, url)

        @server.tool(title="링크 가져오기 상태 확인")
        def get_import_status(job_id: str) -> dict:
            """공개 링크에서 파일을 가져오는 작업의 상태와 저장된 자료 ID를 확인합니다."""
            return imports.status(job_id)

    @server.tool(title="백업 상태 확인")
    def get_backup_status() -> dict:
        """가장 최근 자동 백업의 실행 상태와 성공·실패 여부를 확인합니다."""
        path = Path(os.environ.get('VOOY_BACKUP_STATUS_FILE', '/run/vooy-mcp/secrets/backup-status.json'))
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'state': 'not_run'}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError('백업 상태를 읽을 수 없습니다.') from exc
        if not isinstance(value, dict):
            raise ValueError('백업 상태 형식이 올바르지 않습니다.')
        return value

    if rag is not None:
        def image_preview(asset_id, version_id):
            if artifact_downloads is None:
                return None
            for artifact in rag.list_artifacts(asset_id):
                if artifact['kind'] == 'thumbnail':
                    link = artifact_downloads.preview_link(artifact['id'], version_id)
                    if link:
                        return link
            return None

        @server.tool(title="사진 미리보기")
        def get_image_preview(asset_id: str) -> dict:
            """저장된 사진의 미리보기 주소와 원본 다운로드 주소를 발급합니다. 미리보기 처리 전에는 원본 링크와 처리 상태를 반환합니다."""
            asset = repository.get_asset(asset_id)
            if asset is None or asset.get('kind') != 'image':
                raise ValueError('사진을 찾을 수 없습니다.')
            preview = image_preview(asset_id, asset['version_id'])
            return {'asset_id': asset_id, 'version_id': asset['version_id'], 'preview': preview,
                    'preview_status': 'ready' if preview else 'not_ready',
                    'download': downloads.link(asset_id, asset['version_id'])}

        @server.tool(title="분석 결과와 미리보기 조회")
        def list_artifacts(asset_id: str) -> dict:
            """연결된 RAG가 생성한 OCR·자막·썸네일 등 파생 자료가 있으면 목록과 다운로드 링크를 조회합니다."""
            items = rag.list_artifacts(asset_id)
            if artifact_downloads:
                for item in items:
                    item['download'] = artifact_downloads.link(item['id'])
            return {'artifacts': items}

        @server.tool(title="검색용 임베딩 공간 등록")
        def register_embedding_space(model_id: str, model_revision: str, dimensions: int, metric: str,
                                     normalization: str, preprocessing_revision: str,
                                     modalities: list[str], document_encoder: str,
                                     query_encoder: str, representation: str = 'single_vector') -> dict:
            """외부 RAG 모델의 이름·차원·처리 방식을 등록해 검색용 임베딩 공간을 만듭니다. 모델 실행은 별도 연결이 필요합니다."""
            return rag.register_space(model_id, model_revision, dimensions, metric, normalization,
                                      preprocessing_revision, modalities, representation,
                                      document_encoder, query_encoder)

        @server.tool(title="임베딩 공간 목록 보기")
        def list_embedding_spaces() -> dict:
            """등록된 임베딩 모델과 검색 공간을 조회합니다."""
            return {'spaces': rag.list_spaces()}

        @server.tool(title="자료 분석·임베딩 요청")
        def request_ingestion(asset_id: str, space_id: str, pipeline_revision: str | None = None,
                              request_key: str | None = None) -> dict:
            """자료의 멀티모달 분석과 임베딩 작업을 예약합니다. 파이프라인을 생략하면 연결된 작업자 설정을 사용합니다."""
            revision = default_pipeline_revision if pipeline_revision is None else pipeline_revision
            return rag.enqueue(asset_id, space_id, revision, request_key)

        @server.tool(title="자료 분석 상태 확인")
        def get_ingestion_status(job_id: str | None = None, asset_id: str | None = None) -> dict:
            """분석 작업 ID 또는 자료 ID로 처리 상태를 확인합니다."""
            if bool(job_id) == bool(asset_id):
                raise ValueError('작업 ID 또는 자료 ID 중 하나를 입력해 주세요.')
            result = rag.status(job_id) if job_id else {'jobs': rag.status_for_asset(asset_id)}
            if result is None:
                raise ValueError('작업을 찾을 수 없습니다.')
            return result

        @server.tool(title="실패한 분석 다시 요청")
        def retry_ingestion(job_id: str) -> dict:
            """실패한 자료 분석 작업을 다시 처리하도록 예약합니다."""
            return rag.retry(job_id)

        def _search_assets(query, space_id=None, kinds=None, limit=5, collection_id=None,
                           captured_from=None, captured_to=None, auto_collection_policy=False):
            spaces = rag.list_spaces()
            if space_id:
                selected = next((space for space in spaces if space['id'] == space_id), None)
            else:
                selected = spaces[0] if len(spaces) == 1 else None
            if selected is None:
                raise ValueError('검색에 사용할 임베딩 공간을 지정해 주세요.')
            if encoder is None:
                raise ValueError('질문 임베딩 인코더가 연결되지 않았습니다.')
            vector = encoder.encode(query, selected)
            params = {'limit': limit, 'kinds': kinds}
            if collection_id:
                params['collection_id'] = collection_id
            if captured_from is not None:
                params['captured_from'] = captured_from
            if captured_to is not None:
                params['captured_to'] = captured_to
            if auto_collection_policy:
                params['auto_collection_policy'] = True
            media_search = bool(kinds) and all(kind in ('video', 'audio', 'document') for kind in kinds)
            if media_search:
                params['use_collection_policy'] = False
            result = rag.search_vector(selected['id'], vector, **params)
            if media_search:
                result['relevance_unverified'] = True
            for row in result['results']:
                row['download'] = downloads.link(row['asset_id'], row['version_id'])
                if row['kind'] == 'image':
                    row['preview'] = image_preview(row['asset_id'], row['version_id'])
                elif row['kind'] in ('video', 'audio', 'document') and hasattr(downloads, 'media_link'):
                    row['media'] = downloads.media_link(row['asset_id'], row['version_id'])
                    start = (row.get('locator') or {}).get('start_ms')
                    if row['media'] and row['kind'] in ('video', 'audio') and isinstance(start, int):
                        row['playback_url'] = row['media']['url'] + '#t=' + str(start / 1000)
            if result.get('relevance_unverified') and result.get('missing_capture_date_count'):
                result['status'] = 'date_metadata_missing'
                result['message'] = ('촬영 날짜가 없는 사진이 있어 날짜 조건에 맞는지 확인할 수 없습니다. '
                                     '표시된 사진도 일치가 검증되지 않은 후보입니다.')
            elif result['results'] and result.get('relevance_unverified'):
                result['status'] = 'candidates_unverified'
                result['message'] = ('검색된 사진은 유사도 기준이 검증되지 않은 후보입니다. '
                                     '요청과 일치하는지 직접 확인해 주세요.')
            elif result['results']:
                result['status'] = 'ok'
            elif result.get('missing_capture_date_count'):
                result['status'] = 'date_metadata_missing'
                result['message'] = ('촬영 날짜가 없는 사진이 있어 날짜 조건에 맞는지 확인할 수 없습니다. '
                                     '날짜 조건 없이 검색하거나 사진의 촬영 정보를 확인해 주세요.')
            else:
                result['status'] = 'no_match'
            return result

        @server.tool(title="의미로 자료 검색")
        def search_assets(query: str, space_id: str | None = None,
                          kinds: list[str] | None = None, limit: int = 5,
                          collection_id: str | None = None, captured_from: str | None = None,
                          captured_to: str | None = None) -> dict:
            """질문과 의미가 가까운 사진·영상·오디오·문서·텍스트를 검색합니다. kinds에 image, video, audio, document를 지정할 수 있으며 영상·오디오는 시간 구간, 문서는 페이지를 반환합니다. 컬렉션과 촬영 날짜 범위(YYYY-MM-DD)를 지정할 수 있습니다. 등록된 임베딩 공간과 연결된 질문 인코더가 필요합니다. 원본은 media 또는 download 주소로 받습니다."""
            return _search_assets(query, space_id, kinds, limit, collection_id, captured_from, captured_to)

        @server.tool(title='사진 검색 및 이미지 표시')
        def search_photos(query: str, collection_id: str | None = None, limit: int = 3,
                          captured_from: str | None = None, captured_to: str | None = None,
                          gallery_id: str | None = None) -> CallToolResult:
            """자연어로 사진을 검색하고 미리보기와 photo_id를 반환합니다. candidates_unverified는 일치가 검증되지 않은 후보이므로 후보임을 사용자에게 안내합니다. 촬영일 미상은 날짜 확인이 필요합니다. 갤러리 이름을 알면 gallery_id에 지정합니다(예: bts-daily). get_photo로 원본을 표시합니다."""
            from vooy_storage.image_response import search_result
            if gallery_id:
                matches = [item for item in repository.list_collections() if item['name'] == gallery_id]
                if len(matches) != 1 or (collection_id and collection_id != matches[0]['id']):
                    raise ValueError('갤러리 이름이 없거나 모호합니다.')
                collection_id = matches[0]['id']
            if not captured_from and not captured_to and _is_bts_gallery_browse(query):
                collections = [item for item in repository.list_collections() if item['name'] == 'bts-daily']
                spaces = rag.list_spaces()
                if len(collections) == 1 and len(spaces) == 1:
                    target = collections[0]['id']
                    if collection_id is None or collection_id == target:
                        payload = rag.browse_collection_images(spaces[0]['id'], target, limit,
                                                               require_unique_policy=collection_id is None)
                        if payload is not None:
                            for row in payload['results']:
                                row['photo_id'] = row['asset_id']
                                row['download'] = downloads.link(row['asset_id'], row['version_id'])
                                row['preview'] = image_preview(row['asset_id'], row['version_id'])
                            return search_result(payload, rag, captioner=captioner)
            payload = _search_assets(query, kinds=['image'], limit=limit, collection_id=collection_id,
                                     captured_from=captured_from, captured_to=captured_to,
                                     auto_collection_policy=not collection_id)
            for row in payload['results']:
                row['photo_id'] = row['asset_id']
            return search_result(payload, rag, captioner=captioner)

        @server.tool(title='사진 원본 표시')
        def get_photo(photo_id: str, version_id: str | None = None) -> CallToolResult:
            """search_photos에서 받은 photo_id의 원본 이미지 콘텐츠와 임시 다운로드 링크를 반환합니다."""
            from vooy_storage.image_response import photo_result
            return photo_result(photo_id, repository, downloads, version_id, captioner=captioner)

        @server.tool(title='영상 장면 검색')
        def search_videos(query: str, limit: int = 5, collection_id: str | None = None) -> CallToolResult:
            """영상의 대표 프레임을 의미로 검색하고 해당 프레임과 시간 구간, 재생·다운로드 링크를 제공합니다."""
            from vooy_storage.video_response import video_search_result
            return video_search_result(_search_assets(query, kinds=['video'], limit=limit,
                                                       collection_id=collection_id), rag)

        @server.tool(title='오디오 구간 검색')
        def search_audio(query: str, limit: int = 5, collection_id: str | None = None) -> dict:
            """오디오 구간을 의미로 검색하고 시간 구간과 재생·다운로드 링크를 제공합니다."""
            return _search_assets(query, kinds=['audio'], limit=limit, collection_id=collection_id)

        @server.tool(title='문서 내용 검색')
        def search_documents(query: str, limit: int = 5, collection_id: str | None = None) -> dict:
            """문서의 페이지 또는 텍스트 구간을 의미로 검색하고 원본 열기·다운로드 링크를 제공합니다."""
            return _search_assets(query, kinds=['document'], limit=limit, collection_id=collection_id)

        @server.tool(title='영상·오디오·문서 열기')
        def get_media(asset_id: str, version_id: str | None = None) -> CallToolResult:
            """자료 ID로 영상·오디오·문서의 임시 열기 링크와 다운로드 링크를 반환합니다."""
            from mcp_types import ResourceLink, TextContent
            asset = repository.get_asset(asset_id, version_id) if version_id else repository.get_asset(asset_id)
            if not asset or asset.get('kind') not in ('video', 'audio', 'document'):
                raise ValueError('영상·오디오·문서를 찾을 수 없습니다.')
            version = asset['version_id']
            original = downloads.link(asset_id, version)
            inline = downloads.media_link(asset_id, version)
            payload = {'asset_id': asset_id, 'version_id': version, 'kind': asset['kind'],
                       'title': asset.get('title'), 'mime_type': asset.get('mime_type'),
                       'media': inline, 'download': original}
            link = inline or original
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload, ensure_ascii=False)),
                ResourceLink(type='resource_link', name=asset.get('filename') or 'media', uri=link['url'],
                             mime_type=asset.get('mime_type') or 'application/octet-stream')], structured_content=payload)
