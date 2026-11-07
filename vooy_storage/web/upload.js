'use strict';
const uid = location.pathname.split('/').pop();
const api = '/api/v1/uploads/' + uid;
const fileInput = document.getElementById('file');
const start = document.getElementById('upload');
const pause = document.getElementById('pause');
const status = document.getElementById('status');
const progress = document.getElementById('progress');
const chunkSize = 8 * 1024 * 1024;
let paused = false;
let completed = false;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const hex = bytes => Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
async function call(url, options = {}) {
  const response = await fetch(url, {...options, credentials:'same-origin', cache:'no-store'});
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).error || ''; } catch (_) {}
    throw new Error(detail || `요청 실패 (${response.status}). 업로드 링크를 다시 열어 주세요.`);
  }
  return response.status === 204 ? null : (response.headers.get('content-type') || '').includes('json') ? response.json() : null;
}
async function poll() {
  while (true) {
    const state = await call(api);
    if (state.state === 'completed') { completed = true; progress.value = 100; status.textContent = '저장 완료. 자료 ID: ' + state.asset.id; start.disabled = true; pause.disabled = true; return; }
    if (state.state === 'failed') throw new Error(state.error || '파일 저장에 실패했습니다.');
    if (state.state !== 'finalizing') return state;
    status.textContent = '파일을 검증하고 저장하는 중입니다.';
    await sleep(1500);
  }
}
async function initialize() {
  const token = new URLSearchParams(location.hash.slice(1)).get('token');
  if (token) {
    history.replaceState(null, '', location.pathname);
    await call(api + '/exchange', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token})});
  }
  const state = await poll();
  if (state) { status.textContent = state.title + ' — 파일을 선택해 주세요.'; start.disabled = !fileInput.files.length; }
}
fileInput.addEventListener('change', () => { start.disabled = !fileInput.files.length; });
pause.addEventListener('click', () => { paused = true; pause.disabled = true; status.textContent = '현재 조각을 전송한 뒤 일시 중단합니다.'; });
start.addEventListener('click', async () => {
  const file = fileInput.files[0]; if (!file) return;
  paused = false; start.disabled = true; pause.disabled = false; fileInput.disabled = true;
  try {
    let state = await call(api + '/file', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({filename:file.name,size:file.size})});
    // Validate existing chunks against the reselected file before resuming.
    for (const old of state.parts) {
      const bytes = await file.slice(old.part_no * chunkSize, old.part_no * chunkSize + old.size).arrayBuffer();
      if (hex(await crypto.subtle.digest('SHA-256', bytes)) !== old.sha256) throw new Error('이전에 선택한 파일과 내용이 다릅니다.');
    }
    for (let i = state.parts.length; i * chunkSize < file.size; i++) {
      if (paused) { status.textContent = '일시 중단했습니다. 같은 파일을 선택해 이어 올릴 수 있습니다.'; return; }
      const bytes = await file.slice(i * chunkSize, Math.min((i + 1) * chunkSize, file.size)).arrayBuffer();
      const hash = hex(await crypto.subtle.digest('SHA-256', bytes));
      await call(api + '/parts/' + i, {method:'PUT', headers:{'Content-Type':'application/octet-stream','X-Part-SHA256':hash}, body:bytes});
      progress.value = Math.round(Math.min((i + 1) * chunkSize, file.size) / file.size * 100);
      status.textContent = `전송 중 ${progress.value}%`;
    }
    await call(api + '/complete', {method:'POST'}); await poll();
  } catch (error) { status.textContent = error.message; }
  finally { start.disabled = completed; pause.disabled = true; fileInput.disabled = completed; }
});
initialize().catch(error => {status.textContent = error.message;});
