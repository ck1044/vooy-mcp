# Vooy 저장소와 멀티모달 RAG 연동 계약 v1

이 문서는 외부 RAG 워커가 저장된 자료를 읽고 추출 결과와 벡터를 전달할 때 쓰는 계약이다. 주소는 배포된 서버의 `https://<호스트>/api/v1/worker`다. 워커는 별도로 발급된 서비스 토큰을 `Authorization: Bearer <작업자 토큰>`에 넣는다. 모델 실행과 질문 임베딩은 외부 RAG가 담당한다.

## 공간과 작업

공간은 모델 이름·버전, 차원, 거리(`cosine`, `l2`, `inner_product`), 정규화(`unit`, `none`), 전처리 버전, 입력 모달리티, 단일/다중 벡터, 자료·질문 인코더를 함께 식별한다. 변경되면 다른 공간이다. 모달리티가 같은 벡터 공간에서 비교 가능한지는 모델 개발 측에서 정한다.

자료 등록 후 사용자가 등록된 공간과 파이프라인 버전으로 작업을 요청한다. 같은 요청 키는 같은 자료·공간·입력 버전에 대해서 같은 작업을 반환한다. 새 작업은 동일 자료·공간의 미완료 작업을 대체하며, 이미 활성화된 검색 결과는 새 결과가 완성될 때까지 유지된다.

## 작업 순서

1. `POST /jobs/claim`: `{"pipeline_revisions":["v1"],"space_ids":["공간 UUID"]}`. 작업을 받으면 `id`, `asset_id`, `asset_version_id`, `space_id`, `lease_epoch`, `lease_until`을 반환한다. 작업이 없으면 204다.
2. `POST /jobs/{job_id}/heartbeat`: `{"lease_epoch":1,"progress":45}`. 현재 임대를 5분 연장한다. `progress`는 선택적인 0~100 정수다. 60초마다 호출한다.
3. `POST /jobs/{job_id}/source-grant`: `{"lease_epoch":1}`. 텍스트는 `text_body`로 반환한다. 파일은 `source_url`을 반환하며 동일한 작업자 Bearer 헤더로 GET한다. 각 요청에서 현재 임대·삭제·원본 버전을 다시 검사한다. 파일은 Range 요청을 지원한다.
4. `POST /jobs/{job_id}/result-sets`: `{"lease_epoch":1}`. 결과 묶음 `id`를 받는다. 동일 작업·임대 세대에서 재호출하면 같은 ID를 반환한다.
5. `PUT /result-sets/{result_id}/batches/{batch_id}`로 추출 구간과 벡터를 보낸다. 한 배치는 JSON UTF-8 8 MiB, 벡터 256개 이하다.
6. 필요하면 `POST /result-sets/{result_id}/artifacts`로 파생물을 만든 뒤 분할 전송한다. 아래 항목을 참고한다.
7. `POST /result-sets/{result_id}/complete`에 `lease_epoch`와 manifest를 전달한다. 서버는 모든 배치·파생물 수를 검증하고 한 트랜잭션으로 활성화한다.
8. 실행 실패는 `POST /jobs/{job_id}/fail`에 `{"lease_epoch":1,"error_code":"model_error","retryable":true}`로 보고한다. 자동 재시도는 최초 실행 이후 최대 3회다.

## 벡터 배치

숫자는 계약 시험용 3차원이다. 실제 `values` 길이는 등록된 모델 공간의 차원과 같아야 한다. ID는 해당 작업에서 실제로 받은 값을 사용한다.

```json
{
  "lease_epoch": 1,
  "source_version_id": "7dbca6aa-1aca-4d14-8bc4-61aeaa6bfd75",
  "space_id": "a8c1b14c-1aca-4d14-8bc4-61aeaa6bfd75",
  "segments": [
    {
      "external_id": "scene-0001",
      "kind": "video",
      "text": "바닷가에서 걷는 장면",
      "locator": {"start_ms": 12400, "end_ms": 18900},
      "vectors": [{"ordinal": 0, "values": [0.6, 0.8, 0.0]}]
    }
  ]
}
```

텍스트 위치는 `{"char_start":0,"char_end":10}`, 문서는 `{"page":1}`, 이미지 영역은 `{"bbox":[0,0,1,1]}`, 영상·음성 위치는 `{"start_ms":0,"end_ms":1000}`이다. 시간·문자 구간 끝은 제외한다. 텍스트의 기준이 별도 추출물이라면 해당 `artifact_id`를 locator에 추가한다. 파생물 ID는 같은 결과 묶음의 manifest에 포함되어야 한다.

같은 배치 ID와 같은 본문을 재전송하면 같은 결과가 나온다. 같은 ID에 다른 본문, 중복 구간 ID, 차원 불일치, 유한하지 않은 값, 코사인 공간의 영벡터, 단위 벡터 오차가 0.001을 넘는 입력은 거부된다. 다중 벡터는 한 구간 안에서 순번 0부터 연속으로 보낸다.

## 파생물 업로드

썸네일·프레임·자막 등은 결과 묶음에 소속된다. 먼저 `POST /result-sets/{result_id}/artifacts`에 다음 정보를 보낸다. `artifact_id`는 재시도 시 같은 요청으로 만들기 위한 선택적 UUID다.

```json
{
  "lease_epoch": 1,
  "kind": "frame",
  "filename": "frame-001.jpg",
  "mime_type": "image/jpeg",
  "size_bytes": 12345,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "extractor_revision": "video-frame-v1"
}
```

응답의 `id`로 `PUT /artifacts/{id}/parts/{part_no}`에 raw bytes를 보낸다. 헤더 `X-Lease-Epoch`과 `X-Part-SHA256`을 함께 보낸다. 조각은 0부터 순서대로, 8 MiB 단위로 보낸다. 마지막 조각은 남은 크기다. 같은 번호·같은 해시 재전송은 성공하고 내용이 바뀌면 409다. `POST /artifacts/{id}/status`에 `{"lease_epoch":1}`을 보내 저장된 조각을 확인할 수 있다.

모든 조각 전송 후 `POST /artifacts/{id}/complete`에 `{"lease_epoch":1}`을 보낸다. 서버는 각 조각과 전체 SHA-256을 검사하고 원본 버전의 파생물 위치에 보관한다. 자료당 파생물 하나의 최대 크기는 10 GiB다. 파일 확정 상태와 결과 활성화 상태는 별개다.

## 활성화 manifest

```json
{
  "lease_epoch": 1,
  "manifest": {
    "batches": ["batch-1"],
    "segment_count": 1,
    "vector_count": 1,
    "artifact_ids": ["5c6da6b8-1aca-4d14-8bc4-61aeaa6bfd75"],
    "artifact_count": 1
  }
}
```

파생물이 없다면 `artifact_ids: []`, `artifact_count: 0`을 보낸다. 모든 배치와 파생물을 정확히 나열해야 활성화된다. 완료된 같은 manifest의 재제출은 같은 결과를 반환한다. 원본이 삭제·교체되거나 임대가 만료되면 결과 제출은 409다. 새로운 임대는 더 높은 `lease_epoch`를 가지며 이전 세대의 결과를 활성화할 수 없다.

## 검색과 반환

단일 벡터 공간에서는 모델이 만든 질문 벡터를 같은 `space_id`로 조회한다. 검색은 현재 원본 버전과 활성 결과만 대상으로 하며 `asset_id`, `version_id`, `segment_id`, 제목, 관련 텍스트, 위치, 공간 ID와 거리를 반환한다. 현재 구현의 정확 검색은 인덱스 선택 이전의 기준선이다. 다중 벡터는 저장할 수 있고, 검색 시 점수 집계와 재정렬은 실제 RAG 모델 계약이 정해진 뒤 연결한다.

Vooy의 `search_assets`는 별도 질문 인코더에 POST `{"query":"검색 질문","space_id":"공간 UUID"}`를 보내고 `{"embedding":[0.6,0.8,0.0]}` 형태의 응답을 받는다. `.env`의 `VOOY_RAG_QUERY_URL`로 인코더 주소를 지정한다. 인증이 필요한 경우 `.secrets` 아래 토큰 파일을 만들고 컨테이너 경로를 `VOOY_RAG_QUERY_TOKEN_FILE`에 지정하면 `Authorization: Bearer` 헤더로 전달한다. 인코더는 등록된 공간의 차원·정규화와 호환되는 질문 벡터를 반환해야 한다.

작업자 오류는 401(토큰), 409(임대·버전·상태 충돌), 413(배치 크기), 422(형식·차원 등 입력 검증)으로 구분한다. 운영 코드는 토큰과 원본 내용을 로그에 기록하지 않는다.
