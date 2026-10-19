// cameras.js — стабилизированный модуль камер
// - защита от гонок в превью через sequence
// - mainBuffer sequence для избежания перекрёстных загрузок
'use strict';

function setupCameraHover() {
    const container = els.cameraContainer;
    if (!container) return;
    container.addEventListener('mouseenter', () => {
        state.cameraHovered = true;
        if (els.cameraOverlay && els.cameraOverlay.dataset.peekable === '1') {
            els.cameraOverlay.classList.add('overlay-peek');
        }
    });
    container.addEventListener('mouseleave', () => {
        state.cameraHovered = false;
        if (els.cameraOverlay) els.cameraOverlay.classList.remove('overlay-peek');
    });
}

function updateMode(mode) {
    const normalized = mode === 'RAW' ? 'RAW' : 'RULES';
    const changed = state.mode !== normalized;
    state.mode = normalized;
    applyModeUI(normalized);
    if (changed) applyMainCameraSource();
}

function viewModeContextVisible() {
    const production = ['RUNNING', 'STOPPING'].includes(state.lineState);
    const selectedFrame = JOG_ALLOWED_STATES.includes(state.lineState) && state.selectedAnalysisActive;
    return production || selectedFrame;
}

function viewModeAllowed() {
    return (
        viewModeContextVisible()
        && state.statusReceived
        && Object.keys(state.backendControls || {}).length > 0
        && !state.controlPending
        && !state.offline
        && !state.modePending
        && !state.jogBusy
        && !state.distributorDiagnosticPending
        && !state.distributorDiagnosticBackendBusy
        && !state.selectedAnalysisPending
    );
}

function updateViewModeControls() {
    const visible = viewModeContextVisible() && !state.offline;
    const allowed = viewModeAllowed();
    if (els.viewModeToggle) {
        els.viewModeToggle.classList.toggle('is-faded', !visible);
        els.viewModeToggle.disabled = !allowed;
        els.viewModeToggle.textContent = state.mode === 'RULES'
            ? 'ПРАВИЛА' : 'МОДЕЛИ';
        els.viewModeToggle.setAttribute('aria-pressed', state.mode === 'RULES' ? 'true' : 'false');
    }
}

function applyModeUI() {
    updateViewModeControls();
    if (typeof applyLiveBadge === 'function') {
        applyLiveBadge(state.jogActive);
        return;
    }
    if (state.jogActive || state.selectedAnalysisActive) return;
    if (els.modeBadge) els.modeBadge.classList.add('is-faded');
}

async function setViewMode(newMode) {
    if (!viewModeAllowed()) return;
    if (newMode !== 'RAW' && newMode !== 'RULES') return;
    if (state.mode === newMode) return;
    const oldMode = state.mode;
    state.modePending = true;
    updateViewModeControls();
    clearControlError();
    try {
        const result = await apiPost(`/api/mode/${newMode}`, true);
        if (!result) { applyModeUI(oldMode); return; }
        state.mode = newMode;
        if (typeof result.frame_version === 'number') state.currentVersion = result.frame_version;
        state.mainCamStreamView = null;
        applyModeUI(newMode);
        applyMainCameraSource();
        refreshPreviewStrip();
        requestImmediateStatus();
    } finally {
        state.modePending = false;
        updateViewModeControls();
    }
}

async function toggleMode() {
    const newMode = state.mode === 'RULES' ? 'RAW' : 'RULES';
    await setViewMode(newMode);
}

function setupViewModeControls() {
    if (!els.viewModeToggle) return;
    els.viewModeToggle.addEventListener('click', toggleMode);
    updateViewModeControls();
}

// ——— камеры с защитой от гонок ———

function cameraRolesMissingFrames() {
    return (state.cameras || []).filter(
        role => Number(state.frameVersions[role] || 0) <= 0,
    );
}

function cameraFramesReady() {
    return state.cameras.length > 0 && cameraRolesMissingFrames().length === 0;
}

function cameraRolesMissingPreviews() {
    if (!els.previewStrip || !state.cameras.length) return [...state.cameras];
    return state.cameras.filter(role => {
        const card = [...els.previewStrip.querySelectorAll('.preview-cam')]
            .find(item => item.dataset.role === role);
        const img = card && card.querySelector('img');
        const [loadedVersion, loadedMode] = String(img?.dataset.frameKey || '').split('|');
        return !img
            || Number(loadedVersion || 0) <= 0
            || loadedMode !== state.mode
            || !img.complete
            || img.naturalWidth <= 0;
    });
}

function cameraPreviewsReady() {
    return state.cameras.length > 0 && cameraRolesMissingPreviews().length === 0;
}

function setPreviewLoadState(img, loadState) {
    const card = img && img.closest('.preview-cam');
    if (!card) return;
    const hasDecodedFrame = !!(
        img.dataset.frameKey && img.complete && img.naturalWidth > 0
    );
    // При обновлении не прячем уже показанный кадр: placeholder нужен только
    // до первой успешной загрузки этой камеры.
    const visibleState = hasDecodedFrame && loadState !== 'ready'
        ? 'ready' : loadState;
    card.classList.remove('preview-waiting', 'preview-loading', 'preview-error', 'preview-ready');
    card.classList.add(`preview-${visibleState}`);
    const status = card.querySelector('.preview-cam-state');
    if (!status) return;
    if (visibleState === 'ready') status.textContent = '';
    else if (visibleState === 'error') status.textContent = 'НЕТ КАДРА · ПОВТОР';
    else if (visibleState === 'loading') status.textContent = 'ЗАГРУЗКА КАДРА';
    else status.textContent = 'ОЖИДАНИЕ КАДРА';
}

let _camerasFetchSeq = 0;
async function fetchCameras() {
    if (!state.bootDone) return;
    if (state.camerasFetchBusy) return;
    const mySeq = ++_camerasFetchSeq;
    state.camerasFetchBusy = true;
    const data = await apiGet('/api/cameras');
    state.camerasFetchBusy = false;
    if (mySeq !== _camerasFetchSeq) return; // пришёл более свежий запрос
    if (!data || !data.cameras || !data.cameras.length) return;
    const changed = state.cameras.length !== data.cameras.length || state.cameras.some((c, i) => c !== data.cameras[i]);
    state.cameraIds = (data.camera_ids && typeof data.camera_ids === 'object') ? data.camera_ids : {};
    if (!changed) return;
    state.cameras = data.cameras;
    if (!state.currentCamera || !state.cameras.includes(state.currentCamera)) {
        state.currentCamera = state.cameras[0];
        sendActiveCameraIfChanged(state.currentCamera);
    }
    renderPreviewStrip();
    updateMainCameraLabel();
    if (!state.splashActive) applyMainCameraSource();
    if (typeof updateThresholdsPanel === 'function') updateThresholdsPanel();
    checkUiReady();
}

function cameraIdSuffix(role) {
    // Физический Camera ID («CAM <номер>») не показывается в названии
    // камеры ни в превью, ни в главном окне.
    return '';
}

function renderPreviewStrip() {
    if (!els.previewStrip) return;
    els.previewStrip.innerHTML = state.cameras.map((role, i) => `
        <div class="preview-cam preview-waiting ${role === state.currentCamera ? 'active' : ''}" data-role="${role}" data-index="${i}">
            <img alt="" data-frame-key="" data-requested-key="" data-requesting="0" data-req-seq="0" data-retry-at="0">
            <div class="preview-cam-state">ОЖИДАНИЕ КАДРА</div>
            <div class="preview-cam-label">${cameraRoleLabel(role)}${cameraIdSuffix(role)}</div>
        </div>
    `).join('');
    els.previewStrip.querySelectorAll('.preview-cam').forEach(el => {
        el.addEventListener('click', () => selectCamera(el.dataset.role));
    });
    // Первичная загрузка превью сразу после построения списка камер:
    // тот же защищённый механизм, что и плановое обновление.
    els.previewStrip.querySelectorAll('.preview-cam img').forEach(requestPreviewImage);
}

function selectCamera(role) {
    if (state.selectedAnalysisActive || state.selectedAnalysisPending) return;
    if (!state.cameras.includes(role)) return;
    if (state.currentCamera === role) return;
    state.currentCamera = role;
    if (els.previewStrip) {
        els.previewStrip.querySelectorAll('.preview-cam').forEach(el => {
            el.classList.toggle('active', el.dataset.role === role);
        });
    }
    updateMainCameraLabel();
    sendActiveCameraIfChanged(role).finally(requestImmediateStatus);
    if (typeof updateThresholdsPanel === 'function') updateThresholdsPanel();
    applyMainCameraSource();
    requestImmediateStatus();
}

function navigateCamera(direction) {
    if (!state.cameras.length) return;
    const idx = state.cameras.indexOf(state.currentCamera);
    const next = (idx + direction + state.cameras.length) % state.cameras.length;
    selectCamera(state.cameras[next]);
}

function updateMainCameraLabel() {
    const role = state.currentCamera;
    const suffix = role ? cameraIdSuffix(role) : '';
    setIfChanged(els.cameraLabel, `${cameraRoleLabel(role)}${suffix}`);
}

function showSelectedAnalysisFrame(role) {
    if (!role) return;
    setIfChanged(els.cameraLabel, `${cameraRoleLabel(role)} · АНАЛИЗ`);
    clearLivePullTimer();
    const analysisKey = `${role}|${state.mode}|${state.currentVersion}`;
    if (state.mainCamMode === 'analysis' && state.mainCamAnalysisKey === analysisKey) return;
    state.mainCamMode = 'analysis';
    state.mainCamStreamRole = null;
    state.mainCamStreamView = null;
    state.mainCamAnalysisKey = analysisKey;
    mainBufferLoading = false;
    const source = `/frame/${encodeURIComponent(role)}?mode=${encodeURIComponent(state.mode)}&v=${state.currentVersion}&analysis=1`;
    if (typeof showMainCameraFrame === 'function') {
        showMainCameraFrame(source, 'analysis');
    } else if (els.mainCamera) {
        els.mainCamera.src = source;
    }
}

function returnSelectedCameraToLive() {
    updateMainCameraLabel();
    state.mainCamMode = 'pull';
    state.mainCamStreamRole = null;
    state.mainCamStreamView = null;
    state.mainCamAnalysisKey = null;
    mainBufferLoading = false;
    if (els.mainCamera) els.mainCamera.removeAttribute('src');
    applyMainCameraSource();
}

// ——— переключение источника главной камеры ———

function clearLivePullTimer() {
    if (state.livePullTimer) { clearTimeout(state.livePullTimer); state.livePullTimer = null; }
}
function scheduleNextLiveFrame(delay = LIVE_CAM_MIN_GAP) {
    clearLivePullTimer();
    if (state.mainCamMode !== 'live-pull') return;
    state.livePullTimer = setTimeout(() => {
        state.livePullTimer = null;
        maybeRequestMainFrame();
    }, Math.max(1, delay));
}

function applyMainCameraSource() {
    if (state.selectedAnalysisActive) {
        clearLivePullTimer();
        showSelectedAnalysisFrame(state.selectedAnalysisRole || state.currentCamera);
        return;
    }
    if (!state.currentCamera) return;
    if (state.splashActive) return;

    const shouldLivePull = state.jogActive || state.liveStreaming;
    if (shouldLivePull) {
        const desiredRole = state.currentCamera;
        const desiredView = state.mode;
        if (state.mainCamMode === 'live-pull' && state.mainCamStreamRole === desiredRole && state.mainCamStreamView === desiredView) {
            maybeRequestMainFrame();
            return;
        }
        clearLivePullTimer();
        state.mainCamMode = 'live-pull';
        state.mainCamStreamRole = desiredRole;
        state.mainCamStreamView = desiredView;
        state.mainCamAnalysisKey = null;
        mainBufferLoading = false;
        maybeRequestMainFrame();
    } else {
        clearLivePullTimer();
        if (state.mainCamMode === 'pull') { maybeRequestMainFrame(); return; }
        state.mainCamMode = 'pull';
        state.mainCamStreamRole = null;
        state.mainCamStreamView = null;
        state.mainCamAnalysisKey = null;
        if (els.mainCamera) els.mainCamera.removeAttribute('src');
        maybeRequestMainFrame();
    }
}

function maybeRequestMainFrame() {
    if (state.mainCamMode !== 'pull' && state.mainCamMode !== 'live-pull') return;
    if (mainBufferLoading) return;
    if (!state.currentCamera) return;
    if (state.splashActive) return;

    const now = Date.now();
    const gap = now - state.lastFrameTime;
    const minimumGap = state.mainCamMode === 'live-pull' ? LIVE_CAM_MIN_GAP : MAIN_CAM_MIN_GAP;
    if (gap < minimumGap) {
        if (state.mainCamMode === 'live-pull') scheduleNextLiveFrame(minimumGap - gap);
        else setTimeout(maybeRequestMainFrame, minimumGap - gap);
        return;
    }

    state.lastFrameTime = now;
    state.lastSeenVersion = state.currentVersion;
    mainBufferLoading = true;
    mainBufferRequestRole = state.currentCamera;
    mainBufferRequestView = state.mode;
    mainBufferRequestVersion = state.currentVersion;

    // sequence для защиты от гонки
    try { _mainBufferSeq += 1; _mainBufferExpectedSeq = _mainBufferSeq; mainBuffer._seq = _mainBufferSeq; } catch (_) {}

    const versionQuery = state.mainCamMode === 'live-pull' ? `live=1&t=${Date.now()}` : `v=${state.currentVersion}`;
    mainBuffer.src = `/frame/${state.currentCamera}?mode=${state.mode}&${versionQuery}`;
}

// Превью — каждый img с sequence, чтобы устаревший onload не перетёр новый кадр
let _previewReqSeq = 0;

// Зависший запрос не должен навсегда блокировать превью камеры:
// по таймауту снимаем флаг «запрос в полёте», следующий тик повторит.
const PREVIEW_REQUEST_TIMEOUT = 4000;

function requestPreviewImage(img) {
    if (!img) return;
    if (img.dataset.requesting === '1') return;
    if (Number(img.dataset.retryAt || 0) > Date.now()) return;

    const role = img.parentElement.dataset.role;
    const roleVersion = Number(state.frameVersions[role] || 0);
    if (roleVersion <= 0) {
        setPreviewLoadState(img, 'waiting');
        return;
    }

    const frameKey = `${roleVersion}|${state.mode}`;
    if (
        img.dataset.frameKey === frameKey
        && img.complete
        && img.naturalWidth > 0
    ) {
        setPreviewLoadState(img, 'ready');
        return;
    }
    if (img.dataset.requestedKey === frameKey) return;

    const tmp = new Image();
    const mySeq = ++_previewReqSeq;
    img.dataset.requesting = '1';
    img.dataset.requestedKey = frameKey;
    img.dataset.reqSeq = String(mySeq);
    setPreviewLoadState(img, 'loading');

    const scheduleRetry = () => {
        img.dataset.requestedKey = '';
        img.dataset.requesting = '0';
        img.dataset.retryAt = String(Date.now() + 750);
        setPreviewLoadState(img, 'error');
    };
    const clearWatchdog = () => {
        if (img._previewWatchdog) {
            clearTimeout(img._previewWatchdog);
            img._previewWatchdog = null;
        }
    };
    clearWatchdog();
    img._previewWatchdog = setTimeout(() => {
        img._previewWatchdog = null;
        const curSeq = Number(img.dataset.reqSeq || 0);
        if (curSeq !== mySeq || img.dataset.requesting !== '1') return;
        scheduleRetry();
    }, PREVIEW_REQUEST_TIMEOUT);

    tmp.onload = () => {
        // Если за время загрузки уже запросили новее — игнорируем.
        const curSeq = Number(img.dataset.reqSeq || 0);
        if (curSeq !== mySeq) return;
        clearWatchdog();
        img.src = tmp.src;
        img.dataset.frameKey = frameKey;
        img.dataset.requestedKey = '';
        img.dataset.requesting = '0';
        img.dataset.retryAt = '0';
        setPreviewLoadState(img, 'ready');
        if (typeof checkUiReady === 'function') checkUiReady();
        // Пока кадр грузился, могла выйти новая версия — добираем только её,
        // не отправляя второй запрос того же уже загруженного кадра.
        const latestKey = `${Number(state.frameVersions[role] || 0)}|${state.mode}`;
        if (latestKey !== frameKey) requestPreviewImage(img);
    };
    tmp.onerror = () => {
        const curSeq = Number(img.dataset.reqSeq || 0);
        if (curSeq !== mySeq) return;
        clearWatchdog();
        scheduleRetry();
    };
    tmp.src = `/frame/${encodeURIComponent(role)}?mode=${encodeURIComponent(state.mode)}&preview=1&rv=${roleVersion}`;
}

function refreshPreviewStrip() {
    if (!els.previewStrip) return;
    if (state.pendingAnalysisVersion !== null) return;

    // Миниатюры загружаются и под splash: основной интерфейс откроется уже
    // с декодированными кадрами всех трёх камер, а не с пустыми карточками.
    els.previewStrip.querySelectorAll('.preview-cam img').forEach(requestPreviewImage);
}

function updateUptime() {
    const elapsed = Math.floor((Date.now() - state.startTime) / 1000);
    const h = String(Math.floor(elapsed / 3600)).padStart(2, '0');
    const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, '0');
    const s = String(elapsed % 60).padStart(2, '0');
    setIfChanged(els.metricUptime, `${h}:${m}:${s}`, false);
}
