const state = { page: 'overview', rooms: [], cameras: [], storage: [], events: [], people: [], summary: null, ai: null, audioStatus: null, audioActions: [] };
let activeTalkback = null;
let activeTalkbackStream = null;
const cameraAudioPlayers = new Map();
let liveLoadingTimer = null;
let liveLoadState = { total: 0, settled: new Set() };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char]));
const bytes = (value) => { if (!Number.isFinite(Number(value))) return '—'; const units=['B','KB','GB','TB']; let n=Number(value), i=0; while(n>=1024 && i<units.length-1){n/=1024;i++;} return `${n.toFixed(i===0?0:1)} ${units[i]}`; };
const dateText = (value) => { if(!value) return '—'; const date = new Date(value.includes('T') ? value : value.replace(' ', 'T') + 'Z'); return Number.isNaN(date.getTime()) ? value : date.toLocaleString(); };

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (response.status === 401) { showLogin(); throw new Error('Please sign in again.'); }
  if (!response.ok) throw new Error(payload.detail || payload.message || `Request failed (${response.status}).`);
  return payload;
}
function toast(message, type = 'success') { const el=document.createElement('div'); el.className=`toast ${type==='error'?'error':''}`; el.textContent=message; $('#toastRegion').appendChild(el); setTimeout(()=>el.remove(),4200); }
function showLogin() { $('#appShell').classList.add('hidden'); $('#loginScreen').classList.remove('hidden'); $('#loginPassword').focus(); }
function showApp() { $('#loginScreen').classList.add('hidden'); $('#appShell').classList.remove('hidden'); }
function openDialog(id) { const dialog=$(`#${id}`); if(dialog) dialog.showModal(); }
function closeDialog(id) { const dialog=$(`#${id}`); if(dialog) dialog.close(); }
function pageHeading(kicker, title, description, action = '') { return `<div class="page-heading"><div><p class="eyebrow">${kicker}</p><h1>${title}</h1><p>${description}</p></div>${action}</div>`; }
function stat(label, value, icon, foot) { return `<article class="stat-card"><div class="stat-top"><span>${label}</span><span class="stat-icon">${icon}</span></div><div class="stat-value">${value}</div><div class="stat-foot">${foot}</div></article>`; }
function emptyState(icon, title, description, buttonText = '', buttonAction = '') { return `<div class="empty-card"><div class="empty-icon">${icon}</div><strong>${title}</strong><p>${description}</p>${buttonText ? `<button class="btn secondary" data-action="${buttonAction}">${buttonText}</button>` : ''}</div>`; }
function roomOptions() { $('#cameraRoom').innerHTML = `<option value="">Unassigned</option>` + state.rooms.map(room => `<option value="${room.id}">${escapeHtml(room.name)}</option>`).join(''); }

async function loadAll() {
  const [summary, rooms, cameras, storage, events, ai, people, audioStatus, audioActions] = await Promise.all([
    api('/api/summary'), api('/api/rooms'), api('/api/cameras'), api('/api/storage'),
    api('/api/events?limit=100'), api('/api/ai/status'), api('/api/person-tracks?limit=100'),
    api('/api/audio/status'), api('/api/audio/actions?limit=40')
  ]);
  Object.assign(state, { summary, rooms, cameras, storage, events, ai, people, audioStatus, audioActions });
  $('#cameraNavCount').textContent = cameras.length;
  $('#peopleNavCount').textContent = people.length;
  $('#sidebarServerStatus').textContent = 'Connected · local';
  roomOptions();
  renderPage();
}

function renderPage() {
  const names = { overview:'Overview', live:'Live view', cameras:'Cameras', audio:'Audio & talkback', rooms:'Rooms', storage:'Storage', ai:'AI center', people:'People library', events:'Event history', settings:'Security settings' };
  $('#pageCrumb').textContent = names[state.page] || 'Overview';
  document.querySelectorAll('.nav-link[data-page]').forEach(button => button.classList.toggle('active', button.dataset.page === state.page));
  const renderers = { overview: renderOverview, live: renderLive, cameras: renderCameras, audio: renderAudio, rooms: renderRooms, storage: renderStorage, ai: renderAI, people: renderPeople, events: renderEvents, settings: renderSettings };
  $('#pageContent').innerHTML = (renderers[state.page] || renderOverview)();
  if (state.page === 'live') bindLiveImageStatus();
  else hideLiveLoadingOverlay();
}

function renderOverview() {
  const s=state.summary || {};
  const recent=state.events.slice(0,5);
  const diskPercent=s.disk_total_bytes ? Math.min(100,Math.round(s.disk_used_bytes/s.disk_total_bytes*100)) : 0;
  return `${pageHeading('SYSTEM OVERVIEW','Your security workspace','A local-first view of cameras, storage, AI services and recent activity.', '<button class="btn primary" data-action="add-camera">＋ Add camera</button>')}
    <div class="stats-grid overview-stats">${stat('Connected cameras',s.cameras??0,'▣',`${s.rooms??0} configured rooms`)}${stat('Registered storage',s.storage_targets??0,'▤','NAS and network shares')}${stat('Recorded events',s.events??0,'◷','Saved in the local event database')}${stat('Person events',s.person_tracks??0,'◎',`${s.active_person_tracks??0} currently active`)}${stat('Server disk used',bytes(s.disk_used_bytes),'▥',`${bytes(s.disk_free_bytes)} free · ${diskPercent}% used`)}</div>
    <div class="overview-grid"><section class="panel"><div class="panel-head"><div><h2>Camera devices</h2><p>Manage the cameras connected to your network</p></div><button class="text-link" data-page="cameras">View all →</button></div>${state.cameras.length?cameraTable(state.cameras.slice(0,5),false):emptyState('▣','No cameras added yet','Add your first PoE camera using its IP address and RTSP stream details.','Add your first camera','add-camera')}</section>
    <section class="panel"><div class="panel-head"><div><h2>Recent activity</h2><p>Latest events from your local system</p></div><button class="text-link" data-page="events">History →</button></div>${recent.length?`<div class="activity-list">${recent.map(event=>activityItem(event)).join('')}</div>`:`<div class="activity-empty">No events have been recorded yet.<br>Events will appear here when an AI or device service publishes them.</div>`}</section>
    <section class="panel"><div class="panel-head"><div><h2>Storage overview</h2><p>Server disk and registered network storage</p></div><button class="text-link" data-page="storage">Manage →</button></div><div class="stat-top"><span>Current server disk</span><strong>${bytes(s.disk_total_bytes)} total</strong></div><div class="progress-track"><span style="width:${diskPercent}%"></span></div><div class="stat-top"><span>${bytes(s.disk_used_bytes)} used</span><span>${bytes(s.disk_free_bytes)} available</span></div><div class="activity-empty">${state.storage.length?`${state.storage.length} NAS/storage target(s) registered. Network shares are not mounted yet.`:'Your 1 TB server drive is the initial storage location. Add your NAS when ready.'}</div></section>
    <section class="panel"><div class="panel-head"><div><h2>AI processing</h2><p>Local inference runtime status</p></div><button class="text-link" data-page="ai">Open AI center →</button></div><div class="activity-item"><div class="activity-icon">✳</div><div><strong>${state.ai?.runtime_status==='amd-provider-detected'?'AMD provider detected':state.ai?.runtime_version?'Inference runtime installed':'AI runtime not installed'}</strong><small>${escapeHtml(state.ai?.message || 'Checking local AI runtime.')}</small></div></div><div class="activity-item"><div class="activity-icon">▤</div><div><strong>${state.ai?.models_present??0} / ${state.ai?.models_total??3} model files</strong><small>Local model files available</small></div></div></section></div>`;
}

function cameraTable(cameras, showActions=true) {
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Camera</th><th>Network address</th><th>Room</th><th>Connection</th><th>Local AI</th>${showActions?'<th></th>':''}</tr></thead><tbody>${cameras.map(camera=>`<tr><td><strong>${escapeHtml(camera.name)}</strong><span class="subcell">RTSP · port ${camera.port}</span></td><td>${escapeHtml(camera.host)}<span class="subcell">${escapeHtml(camera.stream_path)}</span></td><td>${escapeHtml(camera.room_name||'Unassigned')}</td><td><span class="pill ${camera.connection_status==='online'?'green':'gray'}">${camera.connection_status==='online'?'Online':'Registered'}</span></td><td><span class="pill ${camera.ai_enabled?'green':'gray'}">${camera.ai_enabled?'Enabled':'Off'}</span></td>${showActions?`<td><button class="table-action" data-action="test-camera-ai" data-id="${camera.id}">Test AI</button> <button class="table-action" data-action="toggle-camera-ai" data-enabled="${camera.ai_enabled?0:1}" data-id="${camera.id}">${camera.ai_enabled?'Turn AI off':'Turn AI on'}</button> <button class="table-action" data-action="auto-configure-camera" data-id="${camera.id}">Detect stream</button> <button class="table-action" data-action="delete-camera" data-id="${camera.id}">Remove</button></td>`:''}</tr>`).join('')}</tbody></table></div>`;
}
function renderCameras() { return `${pageHeading('DEVICE MANAGEMENT','Cameras','Add and organize RTSP/ONVIF cameras on your local network.','<div class="heading-actions"><button class="btn secondary" data-page="live">▣ View all cameras</button><button class="btn primary" data-action="add-camera">＋ Add camera</button></div>')}<div class="panel">${state.cameras.length?cameraTable(state.cameras):emptyState('▣','No cameras registered','Add a camera by IP address, test its RTSP stream, and assign it to a room.','Add camera','add-camera')}<div class="panel-head" style="margin:18px 0 0"><div><h2>Network discovery</h2><p>Search the local network for ONVIF-advertising cameras.</p></div><button class="btn secondary" data-action="discover">Discover ONVIF</button></div><div id="discoveryResults"></div></div>`; }
function cameraLiveCard(camera) {
  const reolinkCamera = camera.vendor === 'reolink';
  const controlReady = reolinkCamera && Boolean(state.audioStatus?.reolink_cli);
  return `<article class="live-camera-card" data-camera-card="${camera.id}">
    <div class="live-camera-heading"><div><strong>${escapeHtml(camera.name)}</strong><small>${escapeHtml(camera.room_name || 'Unassigned')} · ${escapeHtml(camera.host)}</small></div><span class="pill gray" data-stream-state="${camera.id}">Queued</span></div>
    <div class="live-video-frame">
      <img class="live-feed" data-live-feed="${camera.id}" data-stream-src="/api/cameras/${camera.id}/stream?quality=sub" alt="Live feed: ${escapeHtml(camera.name)}" decoding="async" referrerpolicy="no-referrer">
      <div class="live-feed-placeholder" data-live-placeholder="${camera.id}">
        <span class="live-card-spinner" aria-hidden="true"></span>
        <span data-placeholder-message>Starting low-latency stream…</span>
        <button class="btn secondary small live-retry-button" data-action="retry-camera-stream" data-id="${camera.id}" type="button">Retry stream</button>
      </div>
    </div>
    <div class="live-camera-actions">
      <button class="btn secondary small" data-action="fullscreen-camera" data-id="${camera.id}">⛶ Fullscreen</button>
      <button class="btn secondary small" data-action="listen-camera" data-id="${camera.id}">♫ Listen</button>
      <button class="btn secondary small" data-action="diagnose-camera-audio" data-id="${camera.id}">Check audio</button>
      <button class="btn secondary small" data-action="talk-camera" data-id="${camera.id}" ${controlReady ? '' : 'disabled title="Talkback requires the Reolink control helper"'}>🎙 Record & send</button>
      <button class="btn secondary small" data-action="siren-camera" data-id="${camera.id}" ${controlReady ? '' : 'disabled title="Siren control requires the Reolink control helper"'}>◉ Siren</button>
      <button class="btn secondary small" data-action="siren-stop-camera" data-id="${camera.id}" ${controlReady ? '' : 'disabled title="Siren control requires the Reolink control helper"'}>Stop siren</button>
    </div>
    <div class="live-camera-footer"><span>${controlReady ? 'Reolink control helper detected' : (reolinkCamera ? 'Install the Reolink LAN helper for speaker controls' : 'RTSP video · select Reolink for speaker controls')}</span><button class="text-link" data-action="connect-audio" data-id="${camera.id}">${controlReady ? 'Connect / refresh controls' : 'Enable Reolink controls'}</button></div>
    <div class="audio-player-slot" data-audio-slot="${camera.id}"></div>
  </article>`;
}

function updateLiveLoadingProgress() {
  const total = liveLoadState.total;
  const ready = liveLoadState.settled.size;
  const progress = $('#liveLoadingProgress');
  const bar = $('#liveLoadingBarFill');
  if (progress) progress.textContent = `${ready} of ${total} camera${total === 1 ? '' : 's'} ready or checked`;
  if (bar) bar.style.width = `${total ? Math.round((ready / total) * 100) : 100}%`;
}

function hideLiveLoadingOverlay() {
  if (liveLoadingTimer) {
    clearTimeout(liveLoadingTimer);
    liveLoadingTimer = null;
  }
  const overlay = $('#liveLoadingOverlay');
  if (overlay) {
    overlay.classList.remove('visible');
    overlay.setAttribute('aria-hidden', 'true');
  }
}

function settleLiveCamera(cameraId) {
  liveLoadState.settled.add(String(cameraId));
  updateLiveLoadingProgress();
  if (liveLoadState.total > 0 && liveLoadState.settled.size >= liveLoadState.total) {
    hideLiveLoadingOverlay();
  }
}

function showCameraPlaceholder(cameraId, message, showRetry = false) {
  const placeholder = document.querySelector(`[data-live-placeholder="${cameraId}"]`);
  if (!placeholder) return;
  const messageNode = placeholder.querySelector('[data-placeholder-message]');
  if (messageNode) messageNode.textContent = message;
  const spinner = placeholder.querySelector('.live-card-spinner');
  if (spinner) spinner.classList.toggle('hidden', showRetry);
  const retry = placeholder.querySelector('.live-retry-button');
  if (retry) retry.classList.toggle('visible', showRetry);
  placeholder.classList.remove('hidden');
}

function startLiveCameraFeed(image) {
  const cameraId = image.dataset.liveFeed;
  const statePill = document.querySelector(`[data-stream-state="${cameraId}"]`);
  const placeholder = document.querySelector(`[data-live-placeholder="${cameraId}"]`);
  if (statePill) { statePill.textContent = 'Connecting'; statePill.className = 'pill gray'; }
  showCameraPlaceholder(cameraId, 'Trying the camera substream first…', false);

  image.addEventListener('load', () => {
    image.dataset.diagnosing = '0';
    if (statePill) { statePill.textContent = 'Live'; statePill.className = 'pill green'; }
    if (placeholder) placeholder.classList.add('hidden');
    settleLiveCamera(cameraId);
  });

  image.addEventListener('error', async () => {
    if (image.dataset.diagnosing === '1') return;
    image.dataset.diagnosing = '1';
    if (statePill) { statePill.textContent = 'Checking'; statePill.className = 'pill amber'; }
    showCameraPlaceholder(cameraId, 'Stream did not start. Checking camera and decoder…', false);
    settleLiveCamera(cameraId);
    try {
      const result = await api(`/api/cameras/${cameraId}/stream/diagnostics`);
      const ok = result.video_decode_ok === true;
      if (statePill) {
        statePill.textContent = ok ? 'Retrying' : (result.connected ? 'Decode failed' : 'Offline');
        statePill.className = `pill ${ok ? 'green' : 'amber'}`;
      }
      const retries = Number(image.dataset.diagnosticRetries || 0);
      if (ok && retries < 1) {
        image.dataset.diagnosticRetries = String(retries + 1);
        showCameraPlaceholder(cameraId, 'Video decoded. Re-opening the feed…', false);
        const retry = new URL(image.dataset.streamSrc, window.location.href);
        retry.searchParams.set('t', Date.now());
        image.src = retry.toString();
      } else {
        showCameraPlaceholder(cameraId, result.message || 'No decoded video frame was received.', true);
      }
    } catch (error) {
      showCameraPlaceholder(cameraId, error.message || 'Unable to diagnose this camera.', true);
      if (statePill) { statePill.textContent = 'Offline'; statePill.className = 'pill amber'; }
    } finally {
      image.dataset.diagnosing = '0';
    }
  });

  // Assign only after load/error listeners exist, avoiding a race on fast first frames.
  image.dataset.diagnosing = '0';
  image.src = image.dataset.streamSrc + (image.dataset.streamSrc.includes('?') ? '&' : '?') + 't=' + Date.now();
}

function bindLiveImageStatus() {
  const images = [...document.querySelectorAll('[data-live-feed]')];
  liveLoadState = { total: images.length, settled: new Set() };
  const overlay = $('#liveLoadingOverlay');
  const message = $('#liveLoadingMessage');
  if (!images.length) {
    hideLiveLoadingOverlay();
    return;
  }
  if (overlay) {
    overlay.classList.add('visible');
    overlay.setAttribute('aria-hidden', 'false');
  }
  if (message) message.textContent = 'Opening low-bandwidth streams first. Larger streams are used only when necessary.';
  updateLiveLoadingProgress();
  images.forEach(startLiveCameraFeed);
  // Do not trap the operator behind an overlay if a disconnected camera never answers.
  if (liveLoadState.settled.size < liveLoadState.total) {
    liveLoadingTimer = window.setTimeout(() => {
      hideLiveLoadingOverlay();
      if (message) message.textContent = 'Some cameras are still connecting. Their individual tiles show the current status.';
    }, 12000);
  } else {
    hideLiveLoadingOverlay();
  }
}

function retryLiveCamera(cameraId) {
  const image = document.querySelector(`[data-live-feed="${cameraId}"]`);
  const statePill = document.querySelector(`[data-stream-state="${cameraId}"]`);
  if (!image) return;
  image.dataset.diagnosticRetries = '0';
  if (statePill) { statePill.textContent = 'Reconnecting'; statePill.className = 'pill gray'; }
  showCameraPlaceholder(cameraId, 'Reconnecting to the camera…', false);
  const next = new URL(image.dataset.streamSrc, window.location.href);
  next.searchParams.set('t', Date.now());
  image.src = next.toString();
}

function getLiveGridColumns() {
  try {
    const saved = Number(localStorage.getItem('nexgard.liveGridColumns') || 2);
    return [1, 2, 3, 4].includes(saved) ? saved : 2;
  } catch {
    return 2;
  }
}

function applyLiveGridLayout(columns) {
  const grid = document.querySelector('.live-camera-grid');
  const selected = [1, 2, 3, 4].includes(Number(columns)) ? Number(columns) : 2;
  if (grid) grid.dataset.columns = String(selected);
  const picker = $('#liveGridColumns');
  if (picker) picker.value = String(selected);
  try { localStorage.setItem('nexgard.liveGridColumns', String(selected)); } catch { /* Private browsing may block storage. */ }
}

function syncFullscreenControls() {
  const current = document.fullscreenElement;
  document.querySelectorAll('[data-action="fullscreen-camera"]').forEach(button => {
    const card = button.closest('[data-camera-card]');
    const active = Boolean(card && current === card);
    button.textContent = active ? '⤢ Exit fullscreen' : '⛶ Fullscreen';
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const wall = document.querySelector('.live-camera-grid');
  const wallButton = document.querySelector('[data-action="fullscreen-wall"]');
  if (wallButton) {
    const active = Boolean(wall && current === wall);
    wallButton.textContent = active ? '⤢ Exit fullscreen grid' : '⛶ Fullscreen grid';
    wallButton.setAttribute('aria-pressed', active ? 'true' : 'false');
  }
}

async function enterCameraFullscreen(cameraId) {
  const card = document.querySelector(`[data-camera-card="${cameraId}"]`);
  if (!card) return;
  try {
    if (document.fullscreenElement === card) {
      await document.exitFullscreen();
      return;
    }
    if (document.fullscreenElement) await document.exitFullscreen();
    await card.requestFullscreen();
  } catch (error) {
    toast(`Fullscreen could not start: ${error.message}`, 'error');
  }
}

async function enterWallFullscreen() {
  const wall = document.querySelector('.live-camera-grid');
  if (!wall) { toast('There are no camera tiles to display.', 'error'); return; }
  try {
    if (document.fullscreenElement === wall) {
      await document.exitFullscreen();
      return;
    }
    if (document.fullscreenElement) await document.exitFullscreen();
    await wall.requestFullscreen();
  } catch (error) {
    toast(`Fullscreen grid could not start: ${error.message}`, 'error');
  }
}

function renderLive() {
  const columns = getLiveGridColumns();
  const content = state.cameras.length
    ? `<div class="live-camera-grid" data-columns="${columns}">${state.cameras.map(cameraLiveCard).join('')}</div>`
    : emptyState('▣', 'No cameras registered', 'Add your first camera, then every configured camera will appear in this live grid.', 'Add camera', 'add-camera');
  return `${pageHeading('CAMERA WALL', 'Live view', 'Watch every configured camera together, or expand any camera to fullscreen.', '<button class="btn primary" data-action="add-camera">＋ Add camera</button>')}
    <div class="live-toolbar"><div><strong>${state.cameras.length} camera${state.cameras.length===1?'':'s'} configured</strong><small>All cameras appear in the grid. Choose the number of columns, enter fullscreen for the whole wall, or expand one camera.</small></div><div class="live-view-actions"><label class="live-layout-control" for="liveGridColumns"><span>Grid columns</span><select id="liveGridColumns" aria-label="Number of live-view grid columns"><option value="1" ${columns===1?'selected':''}>1 column</option><option value="2" ${columns===2?'selected':''}>2 columns</option><option value="3" ${columns===3?'selected':''}>3 columns</option><option value="4" ${columns===4?'selected':''}>4 columns</option></select></label><div class="heading-actions"><button class="btn secondary" data-action="fullscreen-wall" aria-pressed="false">⛶ Fullscreen grid</button><button class="btn secondary" data-action="refresh-live">↻ Refresh feeds</button></div></div></div>
    ${content}
    <p class="live-keyboard-hint">Tip: use a camera's <strong>Fullscreen</strong> button to focus on one feed. Press <kbd>Esc</kbd> to return to the grid.</p>
    <section class="panel live-audio-note"><div class="panel-head"><div><h2>Camera audio</h2><p>Listen, speak, and trigger a one-shot camera siren from each tile. Controls appear only when the camera supports them.</p></div><button class="text-link" data-page="audio">Open audio center →</button></div>
      <div class="audio-capability-row"><span>FFmpeg ${state.audioStatus?.ffmpeg?'ready':'missing'}</span><span>Reolink helper ${state.audioStatus?.reolink_cli?'installed':'not installed'}</span><span>Text-to-speech ${state.audioStatus?.tts_engine?'ready':'needs espeak-ng'}</span></div>
    </section>`;
}

function renderAudio() {
  const options = state.cameras.map(camera => `<option value="${camera.id}">${escapeHtml(camera.name)} · ${escapeHtml(camera.host)}</option>`).join('');
  const actions = state.audioActions.length ? `<div class="audio-action-list">${state.audioActions.slice(0,12).map(item => `<div class="activity-item"><div class="activity-icon">♫</div><div class="audio-action-detail"><strong>${escapeHtml(item.action)} · ${escapeHtml(item.camera_name || 'Camera removed')}</strong><small>${escapeHtml(item.content_preview || item.details || item.status)} · ${escapeHtml(dateText(item.created_at))}</small></div><span class="pill ${item.status==='success'?'green':'amber'}">${escapeHtml(item.status)}</span></div>`).join('')}</div>` : `<div class="activity-empty">Audio actions will appear here when you use camera audio controls.</div>`;
  return `${pageHeading('AUDIO CONTROL', 'Audio & talkback', 'Send speech through a compatible camera, use live talkback, or trigger a one-shot siren.')}
    <div class="audio-status-grid">
      <div class="audio-status-card"><strong>FFmpeg</strong><span class="pill ${state.audioStatus?.ffmpeg?'green':'amber'}">${state.audioStatus?.ffmpeg?'Ready':'Not installed'}</span></div>
      <div class="audio-status-card"><strong>Reolink control helper</strong><span class="pill ${state.audioStatus?.reolink_cli?'green':'amber'}">${state.audioStatus?.reolink_cli?'Installed':'Not installed'}</span><small>${escapeHtml(state.audioStatus?.reolink_cli_version || state.audioStatus?.reolink_cli_path || 'No helper detected')}</small></div>
      <div class="audio-status-card"><strong>Local TTS engine</strong><span class="pill ${state.audioStatus?.tts_engine?'green':'amber'}">${state.audioStatus?.tts_engine?'Ready':'Not installed'}</span></div>
      <div class="audio-status-card"><strong>Local AI text correction</strong><span class="pill ${state.audioStatus?.local_ai_text_correction?'green':'amber'}">${state.audioStatus?.local_ai_text_correction?'Configured':'Not configured'}</span></div>
    </div>
    ${!state.audioStatus?.reolink_cli ? `<div class="storage-note"><strong>Camera controls need the Reolink LAN helper.</strong> NexGard remains your own dashboard and backend. The optional official Reolink command helper supplies the model-specific speaker, talkback and siren protocol. Live RTSP video and RTSP audio listening do not need it.</div>` : ''}
    <section class="panel audio-console">
      <div class="panel-head"><div><h2>Speaker and talkback console</h2><p>Choose a camera, prepare your announcement, then send it to that camera's speaker.</p></div></div>
      <div class="field"><label for="audioCameraSelect">Target camera</label><select id="audioCameraSelect" ${state.cameras.length?'':'disabled'}>${options || '<option value="">Add a camera first</option>'}</select></div>
      <div class="field"><label for="audioText">Announcement text</label><textarea id="audioText" rows="4" maxlength="1000" placeholder="Type the announcement you want the camera to speak.">This area is monitored.</textarea></div>
      <div class="audio-console-actions">
        <button class="btn secondary" data-action="correct-audio-text" ${state.audioStatus?.local_ai_text_correction?'':'title="Configure a local Ollama model to enable AI correction"'}>✦ Correct text with AI</button>
        <button class="btn primary" data-action="speak-selected" ${state.cameras.length?'':'disabled'}>♫ Speak through camera</button>
        <button class="btn secondary" data-action="listen-selected" ${state.cameras.length?'':'disabled'}>🎧 Listen to camera</button>
        <button class="btn secondary" data-action="diagnose-selected-audio" ${state.cameras.length?'':'disabled'}>Check audio path</button>
        <button class="btn secondary" data-action="test-speaker-selected" ${state.cameras.length && state.audioStatus?.tts_engine && state.audioStatus?.reolink_cli?'':'disabled'}>Test speaker</button>
      </div>
      <p id="audioConsoleMessage" class="muted">${escapeHtml(state.audioStatus?.ollama_message || (state.audioStatus?.local_ai_text_correction?'Local AI text correction is ready.':'AI spelling/grammar correction needs a local Ollama model. Speech synthesis runs locally using espeak-ng.'))}</p>
      <div id="cameraAudioDiagnostic" class="storage-note">Use Check audio path to verify whether the RTSP stream contains a microphone audio track. If it does not, enable Record Audio in the Reolink app under Device Settings → Sounds.</div>
      <div class="audio-console-actions">
        <button class="btn secondary" data-action="talk-selected" ${state.cameras.length?'':'disabled'}>🎙 Record & send talkback</button>
        <button class="btn danger" data-action="siren-selected" ${state.cameras.length?'':'disabled'}>Trigger one-shot siren</button>
        <button class="btn secondary" data-action="siren-stop-selected" ${state.cameras.length?'':'disabled'}>Stop siren</button>
        <button class="btn secondary" data-action="connect-selected" ${state.cameras.length?'':'disabled'}>Enable camera audio controls</button>
      </div>
      <label class="audio-auto-correct"><input id="audioAutoCorrect" type="checkbox" ${state.audioStatus?.local_ai_text_correction?'checked':'disabled'}> Automatically correct spelling and grammar with local AI before speaking</label><p class="audio-safety-note">Microphone talkback requires HTTPS and browser microphone permission. Recording is limited to 10 seconds, then sent to the selected camera. Siren plays once per manual trigger. Device capabilities are checked before camera controls are used.</p>
    </section>
    <section class="panel"><div class="panel-head"><div><h2>Recent audio actions</h2><p>Local history of commands sent from NexGard.</p></div><button class="btn secondary" data-action="refresh-audio">Refresh</button></div>${actions}</section>`;
}

function renderRooms() { return `${pageHeading('ORGANIZATION','Rooms','Group cameras by the physical areas they monitor.','<button class="btn primary" data-action="add-room">＋ Add room</button>')}${state.rooms.length?`<div class="room-grid">${state.rooms.map(room=>`<article class="room-card"><div class="room-card-top"><div class="room-symbol">⌂</div><button class="table-action" data-action="delete-room" data-id="${room.id}">Remove</button></div><h3>${escapeHtml(room.name)}</h3><p>${escapeHtml(room.description||'No description added.')}</p><div class="room-card-bottom"><span>Assigned cameras</span><strong>${room.camera_count??0}</strong></div></article>`).join('')}</div>`:emptyState('⌂','No rooms created','Create rooms such as Driveway, Front Door, Garage or Backyard to organize cameras.','Create first room','add-room')}`; }
function renderStorage() { const s=state.summary||{}; return `${pageHeading('STORAGE MANAGEMENT','Storage','Register local disks and network storage destinations.','<button class="btn primary" data-action="add-storage">＋ Add NAS</button>')}<div class="storage-note"><strong>Safe setup:</strong> adding a NAS here only saves its connection details. It will not mount a share, modify /etc/fstab, or change server permissions. We will implement and test mounting as a separate step.</div><div class="stats-grid">${stat('Server disk',bytes(s.disk_total_bytes),'▥','Current application host')}${stat('Available space',bytes(s.disk_free_bytes),'⇩','Reported by the server OS')}${stat('Registered NAS',state.storage.length,'▤','Network storage records')}${stat('Initial target','1 TB','⌂','Local server storage')}</div><section class="panel"><div class="panel-head"><div><h2>Network storage devices</h2><p>Registered SMB and NFS destinations</p></div></div>${state.storage.length?`<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Name</th><th>Host</th><th>Share</th><th>Protocol</th><th>Status</th><th></th></tr></thead><tbody>${state.storage.map(item=>`<tr><td><strong>${escapeHtml(item.name)}</strong><span class="subcell">Mount: ${escapeHtml(item.mount_path)}</span></td><td>${escapeHtml(item.host)}</td><td>${escapeHtml(item.share_path)}</td><td>${item.protocol.toUpperCase()}</td><td><span class="pill amber">Not mounted</span></td><td><button class="table-action" data-action="delete-storage" data-id="${item.id}">Remove</button></td></tr>`).join('')}</tbody></table></div>`:emptyState('▤','No NAS registered','Your server disk will be used first. Register the 8 TB NAS when it is connected to the network.','Add NAS','add-storage')}</section>`; }
function renderAI() {
  const ai = state.ai || {};
  const objectReady = ai.object_detection_ready === true;
  const recentAiEvents = state.events.filter(event => /person detected|vehicle detected|animal detected/i.test(event.event_type)).slice(0, 8);
  const status = ai.status || 'not-ready';
  const statusMeta = {
    running: ['RUNNING', 'green'],
    'ready-no-cameras': ['READY · NO CAMERAS', 'green'],
    ready: ['READY', 'green'],
    starting: ['STARTING', 'amber'],
    degraded: ['DEGRADED', 'amber'],
    error: ['ERROR', 'red'],
    'not-ready': ['NOT READY', 'amber'],
    'not-started': ['NOT STARTED', 'gray']
  }[status] || [status.toUpperCase(), 'amber'];
  const cameraErrors = Object.entries(ai.camera_errors || {});
  const cameraProfiles = Object.entries(ai.camera_profiles || {});
  return `${pageHeading('LOCAL INFERENCE','AI center','Automatic local object detection from enabled cameras. Inference runs on this server; no cloud AI account is required.', '<button class="btn secondary" data-action="reload-ai">↻ Refresh AI</button>')}
    <div class="ai-status-banner"><strong>Object detection: ${statusMeta[0]}</strong><span class="pill ${statusMeta[1]}">${escapeHtml(status)}</span><p>${escapeHtml(ai.message || 'AI status unavailable.')}</p></div>
    <div class="stats-grid">${stat('AI model', objectReady ? 'YOLOv8n' : 'Missing','✳', objectReady ? 'Local ONNX model loaded' : 'Run setup-ai.sh')}${stat('Active camera workers', ai.worker_camera_count ?? 0,'▣','Enabled cameras being sampled')}${stat('Frames analyzed', ai.frames_processed ?? 0,'▧','Since the last service start')}${stat('Last inference', ai.last_inference_ms == null ? '—' : `${ai.last_inference_ms} ms`,'⚡','CPU inference time')}</div>
    <div class="ai-grid">
      <article class="ai-card"><div class="ai-icon">◎</div><h3>Person detection</h3><p>Detects people in camera frames and creates temporary camera-session event IDs with snapshots.</p><span class="pill ${objectReady?'green':'amber'}">${objectReady?'Model ready':'Not ready'}</span></article>
      <article class="ai-card"><div class="ai-icon">🚗</div><h3>Vehicle detection</h3><p>Detects cars, trucks, buses, motorcycles and bicycles and records event history.</p><span class="pill ${objectReady?'green':'amber'}">${objectReady?'Model ready':'Not ready'}</span></article>
      <article class="ai-card"><div class="ai-icon">◈</div><h3>Animal detection</h3><p>Detects common COCO classes such as dogs, cats, birds, horses, sheep and cattle.</p><span class="pill ${objectReady?'green':'amber'}">${objectReady?'Model ready':'Not ready'}</span></article>
      <article class="ai-card"><div class="ai-icon">▤</div><h3>License plate recognition</h3><p>Plate localization and OCR are separate models and are not installed in this build.</p><span class="pill amber">Not installed</span></article>
    </div>
    ${ai.last_detection_summary ? `<div class="storage-note"><strong>Latest frame summary:</strong> ${escapeHtml(ai.last_detection_summary)} · ${escapeHtml(ai.last_frame_at || '')}</div>` : ''}
    ${cameraProfiles.length ? `<section class="panel" style="margin-top:17px"><div class="panel-head"><div><h2>Camera AI workers</h2><p>Actual stream profile last used by each worker</p></div></div><div class="activity-list">${cameraProfiles.map(([id, profile])=>`<div class="activity-item"><div class="activity-icon">▣</div><div><strong>Camera ${escapeHtml(id)} · ${escapeHtml(profile.quality || 'unknown').toUpperCase()}</strong><small>${escapeHtml(profile.stream_path || '')} · ${escapeHtml(profile.last_frame_at || '')}</small></div></div>`).join('')}</div></section>` : ''}
    ${cameraErrors.length ? `<section class="panel" style="margin-top:17px"><div class="panel-head"><div><h2>Camera AI issues</h2><p>Per-camera diagnostics from the latest sampling attempt</p></div></div><div class="activity-list">${cameraErrors.map(([id, message])=>`<div class="activity-item"><div class="activity-icon">!</div><div><strong>Camera ${escapeHtml(id)}</strong><small>${escapeHtml(message)}</small></div></div>`).join('')}</div></section>` : ''}
    ${ai.last_error && !cameraErrors.length ? `<div class="storage-note"><strong>Latest AI issue:</strong> ${escapeHtml(ai.last_error)}</div>` : ''}
    <section class="panel" style="margin-top:17px"><div class="panel-head"><div><h2>Recent AI detections</h2><p>${recentAiEvents.length} recent events · local camera processing</p></div><button class="btn secondary" data-action="refresh-ai-events">Refresh</button></div>${recentAiEvents.length ? `<div class="activity-list">${recentAiEvents.map(activityItem).join('')}</div>` : '<div class="activity-empty">No detections yet. Once the model is installed and at least one camera AI worker has analyzed a frame, detections will appear here.</div>'}</section>
    <section class="panel" style="margin-top:17px"><h2>Runtime details</h2><p class="muted">ONNX Runtime ${escapeHtml(ai.runtime_version || 'not installed')} · Providers: ${escapeHtml(ai.available_providers?.join(', ') || 'none')} · Model file: ${ai.models?.object_detector ? 'present' : 'missing'}</p><p class="muted">This server uses its CPU for AI inference. Unsupported legacy GPUs are not used. Person event IDs are temporary object-tracking session IDs only; NexGard does not identify people by face or match people across separate visits.</p></section>`;
}


function renderSettings() {
  return `${pageHeading('ACCOUNT & SECURITY','Security settings','Change the NexGard administrator password without editing server files.')}
    <section class="panel settings-panel"><div class="panel-head"><div><h2>Administrator password</h2><p>Use your current password to set a new one. The new password must be at least 12 characters.</p></div></div>
      <form id="changePasswordForm" class="settings-form">
        <div class="field"><label for="currentAdminPassword">Current password</label><input id="currentAdminPassword" name="current_password" type="password" autocomplete="current-password" required maxlength="200"></div>
        <div class="field"><label for="newAdminPassword">New password</label><input id="newAdminPassword" name="new_password" type="password" autocomplete="new-password" minlength="12" required maxlength="200"></div>
        <div class="field"><label for="confirmAdminPassword">Confirm new password</label><input id="confirmAdminPassword" name="confirm_password" type="password" autocomplete="new-password" minlength="12" required maxlength="200"></div>
        <p id="passwordChangeMessage" class="form-message" role="status"></p>
        <div class="modal-actions"><button class="btn primary" type="submit">Save new password</button></div>
      </form>
    </section>
    <div class="storage-note"><strong>Security note:</strong> the password is saved locally in the server's protected .env file. It is never returned to the browser or displayed after saving. Your current session remains signed in; use the new password the next time you sign in.</div>`;
}

function renderPeople() {
  const s=state.summary||{};
  const active=state.people.filter(item=>item.status==='active').length;
  const snapshots=state.people.reduce((total,item)=>total+Number(item.snapshot_count||0),0);
  const table=state.people.length?`<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Event ID</th><th>Camera</th><th>First seen</th><th>Last seen</th><th>Snapshots</th><th>Status</th><th></th></tr></thead><tbody>${state.people.map(item=>`<tr><td><strong>${escapeHtml(item.public_id)}</strong><span class="subcell">Temporary camera tracking ID</span></td><td>${escapeHtml(item.camera_name||'Unknown camera')}</td><td>${escapeHtml(dateText(item.first_seen))}</td><td>${escapeHtml(dateText(item.last_seen))}</td><td>${Number(item.snapshot_count||0)}</td><td><span class="pill ${item.status==='active'?'amber':'green'}">${item.status==='active'?'In view':'Completed'}</span></td><td><button class="text-link" data-action="view-person" data-id="${escapeHtml(item.public_id)}">Open</button></td></tr>`).join('')}</tbody></table></div>`:emptyState('◎','No person events yet','Person detections will appear here when the camera tracking pipeline publishes observations. The dashboard does not invent detections.');
  return `${pageHeading('AI EVENT INTELLIGENCE','People library','Review person-detection events and their locally stored snapshots.')}<div class="stats-grid">${stat('Person events',s.person_tracks??state.people.length,'◎','Temporary tracking sessions')}${stat('Active tracks',s.active_person_tracks??active,'◉','Currently marked in view')}${stat('Saved snapshots',s.person_snapshots??snapshots,'▧','JPEG and PNG event images')}${stat('Tracking mode','Per-camera','⌁','No face matching across visits')}</div><div class="storage-note"><strong>Privacy by design:</strong> random IDs represent one continuous camera tracking event only. When the track ends, a later visit receives a new ID. The system stores event snapshots for review; it does not create facial templates or identify people by face.</div><section class="panel"><div class="panel-head"><div><h2>Detection history</h2><p>Newest tracking events first · ${state.people.length} loaded</p></div><button class="btn secondary" data-action="refresh-people">Refresh</button></div>${table}</section>`;
}

async function openPersonDetails(publicId) {
  const dialog=$('#personDetailsDialog');
  const content=$('#personDetailsContent');
  content.innerHTML='<p class="muted">Loading person event…</p>';
  dialog.showModal();
  try {
    const item=await api(`/api/person-tracks/${encodeURIComponent(publicId)}`);
    const snapshots=item.snapshots||[];
    content.innerHTML=`<div class="modal-heading"><div><p class="eyebrow">PERSON EVENT</p><h2>${escapeHtml(item.public_id)}</h2></div><button type="button" class="icon-button" data-action="close-person-details">×</button></div><div class="person-meta-grid"><div><small>Camera</small><strong>${escapeHtml(item.camera_name||'Unknown')}</strong></div><div><small>Status</small><strong>${item.status==='active'?'Currently in view':'Completed'}</strong></div><div><small>First seen</small><strong>${escapeHtml(dateText(item.first_seen))}</strong></div><div><small>Last seen</small><strong>${escapeHtml(dateText(item.last_seen))}</strong></div><div><small>Detection confidence</small><strong>${item.confidence==null?'Not provided':`${Math.round(Number(item.confidence)*100)}%`}</strong></div><div><small>Snapshots saved</small><strong>${snapshots.length}</strong></div></div><div class="person-description"><small>Observable appearance notes</small><p>${escapeHtml(item.appearance_description||'No appearance description supplied by the detector.')}</p></div><div class="panel-head" style="margin-top:22px"><div><h2>Event snapshots</h2><p>Images captured during this single tracking session.</p></div></div>${snapshots.length?`<div class="snapshot-grid">${snapshots.map(snapshot=>`<a class="snapshot-card" href="${escapeHtml(snapshot.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(snapshot.url)}" alt="Person event snapshot"><span>${escapeHtml(dateText(snapshot.captured_at))}</span></a>`).join('')}</div>`:'<div class="activity-empty">No snapshots were submitted for this event.</div>'}<div class="modal-actions"><button type="button" class="btn secondary" data-action="close-person-details">Close</button>${item.status==='active'?`<button type="button" class="btn secondary" data-action="end-person" data-id="${escapeHtml(item.public_id)}">Mark view ended</button>`:''}<button type="button" class="btn danger" data-action="delete-person" data-id="${escapeHtml(item.public_id)}">Delete event and images</button></div>`;
  } catch(error) {
    content.innerHTML=`<div class="modal-heading"><h2>Unable to load event</h2><button type="button" class="icon-button" data-action="close-person-details">×</button></div><p class="form-error">${escapeHtml(error.message)}</p>`;
  }
}

function activityItem(event) { const icon=event.event_type.toLowerCase().includes('plate')?'▤':event.event_type.toLowerCase().includes('vehicle')?'▣':'✳'; return `<div class="activity-item"><div class="activity-icon">${icon}</div><div style="min-width:0;flex:1"><strong>${escapeHtml(event.event_type)} · ${escapeHtml(event.camera_name)}</strong><small>${escapeHtml(event.summary)} · ${escapeHtml(dateText(event.occurred_at))}</small></div></div>`; }
function renderEvents() { return `${pageHeading('SYSTEM ACTIVITY','Event history','Searchable local record of camera and AI events.')}<section class="panel">${state.events.length?`<div class="activity-list">${state.events.map(activityItem).join('')}</div>`:emptyState('◷','No events recorded','When the camera and AI pipeline is connected, detections will be recorded here.')}</section>`; }

async function submitForm(event, url, form, errorSelector, successMessage, after) {
  event.preventDefault(); const error=$(errorSelector); error.textContent=''; const data=Object.fromEntries(new FormData(form).entries());
  if(data.port) data.port=Number(data.port); if(data.control_port) data.control_port=Number(data.control_port); if(data.room_id==='') data.room_id=null; else if(data.room_id) data.room_id=Number(data.room_id);
  try { await api(url,{method:'POST',body:JSON.stringify(data)}); form.reset(); toast(successMessage); if(after) after(); await loadAll(); }
  catch(err){ error.textContent=err.message; }
}

async function diagnoseCameraAudio(cameraId, { quiet = false } = {}) {
  const panel = $('#cameraAudioDiagnostic');
  const camera = state.cameras.find(item => Number(item.id) === Number(cameraId));
  if (panel) panel.textContent = `Checking microphone audio track for ${camera?.name || `camera ${cameraId}`}…`;
  try {
    const result = await api(`/api/cameras/${cameraId}/audio/diagnostics`);
    const lines = [result.message || 'Audio check complete.'];
    if (result.audio_streams?.length) {
      lines.push('Audio codec: ' + result.audio_streams.map(item => `${item.codec_name || 'unknown'}${item.channels ? ` · ${item.channels} ch` : ''}${item.sample_rate ? ` · ${item.sample_rate} Hz` : ''}`).join(', '));
    }
    if (result.available && result.audio_decode_message) lines.push('Decode test: ' + result.audio_decode_message);
    lines.push(result.talkback_message || '');
    if (panel) panel.textContent = lines.filter(Boolean).join(' ');
    if (!quiet) toast(result.message || 'Audio check complete.', result.listen_ready ? 'success' : 'error');
    return result;
  } catch (error) {
    if (panel) panel.textContent = error.message;
    if (!quiet) toast(error.message, 'error');
    throw error;
  }
}

async function toggleCameraAudio(cameraId) {
  const existing = cameraAudioPlayers.get(cameraId);
  if (existing) {
    existing.pause();
    existing.src = '';
    existing.remove();
    cameraAudioPlayers.delete(cameraId);
    toast('Camera audio stopped.');
    return;
  }
  let diagnostic;
  try { diagnostic = await diagnoseCameraAudio(cameraId, { quiet: true }); }
  catch { return; }
  if (!diagnostic.listen_ready) {
    toast(diagnostic.message || 'The camera microphone stream is not ready to play.', 'error');
    return;
  }
  const audio = new Audio(`/api/cameras/${cameraId}/audio/listen`);
  audio.preload = 'none';
  audio.controls = true;
  audio.autoplay = true;
  const slot = document.querySelector(`[data-audio-slot="${cameraId}"]`) || $('#toastRegion');
  if (slot) slot.appendChild(audio);
  cameraAudioPlayers.set(cameraId, audio);
  audio.addEventListener('error', async () => {
    cameraAudioPlayers.delete(cameraId);
    try { await diagnoseCameraAudio(cameraId, { quiet: true }); } catch {}
    toast('The RTSP microphone track was detected, but the browser audio stream failed. Check server FFmpeg logs and camera audio settings.', 'error');
  });
  try { await audio.play(); toast('Listening to camera microphone.'); }
  catch (error) { cameraAudioPlayers.delete(cameraId); audio.remove(); toast(`Could not play camera audio: ${error.message}`, 'error'); }
}

async function testCameraSpeaker(cameraId) {
  const camera = state.cameras.find(item => Number(item.id) === Number(cameraId));
  const message = `NexGard speaker test for ${camera?.name || 'this camera'}.`;
  if (!confirm(`Play this short test announcement through ${camera?.name || 'the selected camera'}?\n\n${message}`)) return;
  try {
    await api(`/api/cameras/${cameraId}/audio/speak`, { method: 'POST', body: JSON.stringify({ text: message }) });
    toast('Speaker test command was accepted. Check that sound is audible at the camera.');
    await loadAll();
  } catch (error) { toast(error.message, 'error'); }
}

async function connectReolinkAudio(cameraId) {
  try {
    toast('Registering camera control…');
    const result = await api(`/api/cameras/${cameraId}/audio/connect`, { method: 'POST' });
    const capabilities = [result.supports_talkback ? 'two-way audio / TTS' : '', result.supports_siren ? 'camera siren' : ''].filter(Boolean).join(' and ');
    toast(result.message || (capabilities ? `Camera supports ${capabilities}.` : 'Camera control check complete.'));
    await loadAll();
  } catch (error) { toast(error.message, 'error'); }
}

async function triggerSiren(cameraId) {
  if (!confirm('Send one short siren playback to this camera?')) return;
  try { await api(`/api/cameras/${cameraId}/audio/siren`, { method: 'POST' }); toast('One-shot siren command sent.'); await loadAll(); }
  catch (error) { toast(error.message, 'error'); }
}

async function stopSiren(cameraId) {
  try { await api(`/api/cameras/${cameraId}/audio/siren/stop`, { method: 'POST' }); toast('Siren stop command sent.'); }
  catch (error) { toast(error.message, 'error'); }
}

async function toggleTalkback(cameraId, button) {
  if (activeTalkback) {
    if (activeTalkbackCameraId !== cameraId) { toast('Finish the current talkback recording first.', 'error'); return; }
    activeTalkback.stop();
    if (button) button.textContent = '🎙 Record & send';
    return;
  }
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    toast('Browser microphone access requires a trusted HTTPS origin. The current HTTP page cannot request the microphone.', 'error');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
    activeTalkbackStream = stream;
    activeTalkbackCameraId = cameraId;
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
    const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    activeTalkback = recorder;
    recorder.addEventListener('dataavailable', event => { if (event.data?.size) chunks.push(event.data); });
    recorder.addEventListener('stop', async () => {
      stream.getTracks().forEach(track => track.stop());
      activeTalkback = null;
      activeTalkbackStream = null;
      activeTalkbackCameraId = null;
      if (button) button.textContent = '🎙 Record & send';
      const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
      if (!blob.size) { toast('No microphone audio was recorded.', 'error'); return; }
      try {
        await api(`/api/cameras/${cameraId}/audio/talk`, { method: 'POST', body: blob, headers: { 'Content-Type': blob.type || 'audio/webm' } });
        toast('Talkback audio sent to camera.');
        await loadAll();
      } catch (error) { toast(error.message, 'error'); }
    });
    recorder.start(250);
    if (button) button.textContent = '■ Stop & send now';
    toast('Recording microphone for up to 10 seconds. Press again to send.');
    window.setTimeout(() => { if (activeTalkback === recorder && recorder.state === 'recording') recorder.stop(); }, 10000);
  } catch (error) { toast(`Microphone could not start: ${error.message}`, 'error'); }
}

let activeTalkbackCameraId = null;

async function correctAudioText() {
  const field = $('#audioText');
  const message = $('#audioConsoleMessage');
  if (!field) return;
  const original = field.value.trim();
  if (!original) { toast('Type an announcement first.', 'error'); return; }
  if (message) message.textContent = 'Checking text with the local AI model…';
  try {
    const result = await api('/api/audio/correct-text', { method: 'POST', body: JSON.stringify({ text: original }) });
    if (result.available) field.value = result.corrected;
    if (message) message.textContent = result.message + (result.changed ? ' Suggested correction applied to the text box.' : ' No changes were needed.');
    toast(result.available ? 'Text correction complete.' : 'Local AI correction is not configured.', result.available ? 'success' : 'error');
  } catch (error) { if (message) message.textContent = error.message; toast(error.message, 'error'); }
}

async function speakSelectedCamera() {
  const cameraId = Number($('#audioCameraSelect')?.value);
  let text = $('#audioText')?.value?.trim();
  if (!cameraId) { toast('Choose a camera first.', 'error'); return; }
  if (!text) { toast('Type an announcement first.', 'error'); return; }
  try {
    if ($('#audioAutoCorrect')?.checked) {
      const correction = await api('/api/audio/correct-text', { method: 'POST', body: JSON.stringify({ text }) });
      if (!correction.available) {
        throw new Error('Automatic correction is enabled but no local AI model is configured. Set up Ollama, or turn off automatic correction to speak the original text.');
      }
      text = correction.corrected;
      $('#audioText').value = text;
    }
    if (!confirm(`Play this announcement through the selected camera?\n\n${text}`)) return;
    await api(`/api/cameras/${cameraId}/audio/speak`, { method: 'POST', body: JSON.stringify({ text }) });
    toast('Announcement sent to the camera speaker.');
    await loadAll();
  } catch (error) { toast(error.message, 'error'); }
}

$('#loginForm').addEventListener('submit', async (event)=>{ event.preventDefault(); $('#loginError').textContent=''; try { await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#loginPassword').value})}); $('#loginPassword').value=''; showApp(); await loadAll(); } catch(err){ $('#loginError').textContent=err.message; } });
$('#logoutBtn').addEventListener('click', async ()=>{ try{await api('/api/logout',{method:'POST'});}catch{} cameraAudioPlayers.forEach(player=>{player.pause();player.src='';});cameraAudioPlayers.clear();if(activeTalkback&&activeTalkback.state==='recording')activeTalkback.stop();showLogin(); });
$('#mainNav').addEventListener('click',(event)=>{const button=event.target.closest('[data-page]');if(button){state.page=button.dataset.page;renderPage();}});
$('#pageContent').addEventListener('submit', async (event) => {
  if (event.target.id !== 'changePasswordForm') return;
  event.preventDefault();
  const form = event.target;
  const message = $('#passwordChangeMessage');
  const data = Object.fromEntries(new FormData(form).entries());
  if (data.new_password !== data.confirm_password) {
    if (message) message.textContent = 'The new passwords do not match.';
    return;
  }
  if (data.new_password.length < 12) {
    if (message) message.textContent = 'Use at least 12 characters for the new password.';
    return;
  }
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  if (message) message.textContent = 'Saving the new administrator password…';
  try {
    await api('/api/settings/password', { method: 'POST', body: JSON.stringify(data) });
    form.reset();
    if (message) message.textContent = 'Administrator password changed successfully.';
    toast('Administrator password updated.');
  } catch (error) {
    if (message) message.textContent = error.message;
    toast(error.message, 'error');
  } finally {
    submit.disabled = false;
  }
});

$('#pageContent').addEventListener('change', (event) => {
  if (event.target?.id === 'liveGridColumns') applyLiveGridLayout(event.target.value);
});

document.addEventListener('fullscreenchange', syncFullscreenControls);

$('#pageContent').addEventListener('click', async (event)=>{
  const pageButton=event.target.closest('[data-page]'); if(pageButton){state.page=pageButton.dataset.page;renderPage();return;}
  const button=event.target.closest('[data-action]'); if(!button)return; const action=button.dataset.action; const id=Number(button.dataset.id);
  if(action==='refresh-live'){document.querySelectorAll('img[data-live-feed]').forEach(img=>retryLiveCamera(img.dataset.liveFeed));return;}
  if(action==='fullscreen-camera'){await enterCameraFullscreen(id);return;}
  if(action==='fullscreen-wall'){await enterWallFullscreen();return;}
  if(action==='reload-ai' || action==='refresh-ai-events'){try{await api('/api/ai/reload',{method:'POST'});await loadAll();toast('AI status refreshed.');}catch(err){toast(err.message,'error');}return;}
  if(action==='retry-camera-stream'){retryLiveCamera(id);return;}
  if(action==='auto-configure-camera'){button.disabled=true;const oldText=button.textContent;button.textContent='Detecting…';try{const result=await api(`/api/cameras/${id}/auto-configure`,{method:'POST'});toast(result.message||'Camera diagnosis finished.',result.connected?'success':'error');await loadAll();}catch(err){toast(err.message,'error');}finally{button.disabled=false;button.textContent=oldText;}return;}
  if(action==='toggle-camera-ai'){const enabled=button.dataset.enabled==='1';button.disabled=true;try{const result=await api(`/api/cameras/${id}/ai`,{method:'PATCH',body:JSON.stringify({enabled})});toast(result.message);await loadAll();}catch(err){toast(err.message,'error');button.disabled=false;}return;}
  if(action==='test-camera-ai'){button.disabled=true;const oldText=button.textContent;button.textContent='Testing…';try{const result=await api(`/api/cameras/${id}/ai/test`,{method:'POST'});const found=result.detections?.length?result.detections.map(item=>`${item.label} ${Math.round(item.confidence*100)}%`).join(', '):'No supported objects visible in this test frame';toast(`AI test passed · ${result.quality} stream · ${result.inference_ms} ms · ${found}`);}catch(err){toast(err.message,'error');}finally{button.disabled=false;button.textContent=oldText;}return;}
  if(action==='refresh-audio'){try{await loadAll();toast('Audio history refreshed.');}catch(err){toast(err.message,'error');}return;}
  if(action==='listen-camera'){await toggleCameraAudio(id);return;}
  if(action==='diagnose-camera-audio'){await diagnoseCameraAudio(id);return;}
  if(action==='talk-camera'){await toggleTalkback(id,button);return;}
  if(action==='siren-camera'){await triggerSiren(id);return;}
  if(action==='siren-stop-camera'){await stopSiren(id);return;}
  if(action==='connect-audio'){await connectReolinkAudio(id);return;}
  if(action==='correct-audio-text'){await correctAudioText();return;}
  if(action==='speak-selected'){await speakSelectedCamera();return;}
  if(action==='listen-selected'){const selected=Number($('#audioCameraSelect')?.value);if(selected)await toggleCameraAudio(selected);return;}
  if(action==='diagnose-selected-audio'){const selected=Number($('#audioCameraSelect')?.value);if(selected)await diagnoseCameraAudio(selected);return;}
  if(action==='test-speaker-selected'){const selected=Number($('#audioCameraSelect')?.value);if(selected)await testCameraSpeaker(selected);return;}
  if(action==='talk-selected'){const selected=Number($('#audioCameraSelect')?.value);if(selected)await toggleTalkback(selected,button);return;}
  if(action==='siren-selected'){const selected=Number($('#audioCameraSelect')?.value);if(selected)await triggerSiren(selected);return;}
  if(action==='siren-stop-selected'){const selected=Number($('#audioCameraSelect')?.value);if(selected)await stopSiren(selected);return;}
  if(action==='connect-selected'){const selected=Number($('#audioCameraSelect')?.value);if(selected)await connectReolinkAudio(selected);return;}
  if(action==='add-camera'){roomOptions();openDialog('cameraDialog');}
  if(action==='view-person'){await openPersonDetails(button.dataset.id);return;}
  if(action==='close-person-details'){closeDialog('personDetailsDialog');return;}
  if(action==='refresh-people'){try{await loadAll();toast('People library refreshed.');}catch(err){toast(err.message,'error');}return;}
  if(action==='end-person'){if(confirm('Mark this camera tracking event as completed?')){try{await api(`/api/person-tracks/${encodeURIComponent(button.dataset.id)}/end`,{method:'POST'});toast('Person event completed.');closeDialog('personDetailsDialog');await loadAll();}catch(err){toast(err.message,'error');}}return;}
  if(action==='delete-person'){if(confirm('Delete this person event and its stored snapshots? This cannot be undone.')){try{await api(`/api/person-tracks/${encodeURIComponent(button.dataset.id)}`,{method:'DELETE'});toast('Person event and snapshots deleted.');closeDialog('personDetailsDialog');await loadAll();}catch(err){toast(err.message,'error');}}return;}
  if(action==='add-room')openDialog('roomDialog');
  if(action==='add-storage')openDialog('storageDialog');
  if(action==='delete-camera' && confirm('Remove this camera from NexGard Security?')){try{await api(`/api/cameras/${id}`,{method:'DELETE'});toast('Camera removed.');await loadAll();}catch(err){toast(err.message,'error');}}
  if(action==='delete-room' && confirm('Remove this room? Cameras will become unassigned.')){try{await api(`/api/rooms/${id}`,{method:'DELETE'});toast('Room removed.');await loadAll();}catch(err){toast(err.message,'error');}}
  if(action==='delete-storage' && confirm('Remove this registered NAS entry? This does not delete files on the NAS.')){try{await api(`/api/storage/${id}`,{method:'DELETE'});toast('Storage entry removed.');await loadAll();}catch(err){toast(err.message,'error');}}
  if(action==='discover'){
    const target=$('#discoveryResults');button.disabled=true;button.textContent='Searching…';
    try{const result=await api('/api/discovery/onvif',{method:'POST'});target.innerHTML=result.devices.length?`<div class="activity-list">${result.devices.map(device=>`<div class="activity-item"><div class="activity-icon">▣</div><div><strong>${escapeHtml(device.host)}</strong><small>${escapeHtml(device.xaddr)}</small></div><button class="text-link" data-action="use-discovered" data-host="${escapeHtml(device.host)}">Use IP</button></div>`).join('')}</div>`:'<p class="muted">No ONVIF devices found. Confirm multicast discovery is enabled and both devices are on the same network.</p>';}
    catch(err){target.innerHTML=`<p class="muted">${escapeHtml(err.message)}</p>`;}finally{button.disabled=false;button.textContent='Discover ONVIF';}
  }
  if(action==='use-discovered'){ $('#cameraHost').value=button.dataset.host||'';openDialog('cameraDialog'); }
});
$('#personDetailsContent').addEventListener('click', async (event)=>{
  const button=event.target.closest('[data-action]');
  if(!button)return;
  const action=button.dataset.action;
  const publicId=button.dataset.id;
  if(action==='close-person-details'){closeDialog('personDetailsDialog');return;}
  if(action==='end-person'){
    if(confirm('Mark this camera tracking event as completed?')){
      try{await api(`/api/person-tracks/${encodeURIComponent(publicId)}/end`,{method:'POST'});toast('Person event completed.');closeDialog('personDetailsDialog');await loadAll();}
      catch(err){toast(err.message,'error');}
    }
    return;
  }
  if(action==='delete-person'){
    if(confirm('Delete this person event and its stored snapshots? This cannot be undone.')){
      try{await api(`/api/person-tracks/${encodeURIComponent(publicId)}`,{method:'DELETE'});toast('Person event and snapshots deleted.');closeDialog('personDetailsDialog');await loadAll();}
      catch(err){toast(err.message,'error');}
    }
  }
});

document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>closeDialog(button.dataset.close)));
$('#cameraForm').addEventListener('submit',(event)=>submitForm(event,'/api/cameras',$('#cameraForm'),'#cameraFormError','Camera saved.',()=>closeDialog('cameraDialog')));
$('#roomForm').addEventListener('submit',(event)=>submitForm(event,'/api/rooms',$('#roomForm'),'#roomFormError','Room created.',()=>closeDialog('roomDialog')));
$('#storageForm').addEventListener('submit',(event)=>submitForm(event,'/api/storage',$('#storageForm'),'#storageFormError','NAS registered.',()=>closeDialog('storageDialog')));
$('#testCameraBtn').addEventListener('click',async()=>{
  const form=$('#cameraForm'); const data=Object.fromEntries(new FormData(form).entries()); data.port=Number(data.port||554); $('#cameraTestMessage').textContent='Checking RTSP service and detecting a supported video path on the server…'; $('#testCameraBtn').disabled=true;
  try{
    const result=await api('/api/cameras/test',{method:'POST',body:JSON.stringify(data)});
    if(result.connected && result.stream_path) $('#cameraPath').value=result.stream_path;
    const media=result.streams?.map(s=>`${s.codec_name||s.codec_type}${s.width?` ${s.width}×${s.height}`:''}`).join(', ') || '';
    $('#cameraTestMessage').textContent=result.connected?`VIDEO FOUND at ${result.stream_path} · ${media}`:result.message;
    $('#cameraTestMessage').style.color=result.connected?'#77deb2':'#edc27d';
  }
  catch(err){$('#cameraTestMessage').textContent=err.message;$('#cameraTestMessage').style.color='#ff9299';}
  finally{$('#testCameraBtn').disabled=false;}
});
$('#refreshBtn').addEventListener('click',async()=>{try{await loadAll();toast('Dashboard refreshed.');}catch(err){toast(err.message,'error');}});

(async function init(){
  try{const session=await api('/api/session');if(session.authenticated){showApp();await loadAll();}else showLogin();}
  catch{showLogin();}
})();
