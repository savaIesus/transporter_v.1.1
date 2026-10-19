// core.js — Line Monitor UI module (reworked for stability)
// - устранены гонки в поллинге статуса и загрузке кадров
// - mainBuffer защищён счётчиком последовательности
// - адаптивный polling на setTimeout вместо setInterval
'use strict';

// ─── Config ──────────────────────────────────────────────────

const BOOT_INTERVAL        = 150;
const STATUS_INTERVAL_FAST   = 100;
const STATUS_INTERVAL_MOTION = 60;
const STATUS_INTERVAL_IDLE   = 500;
const STATUS_OFFLINE_AFTER   = 1500;
const PREVIEW_INTERVAL       = 180;
const UPTIME_INTERVAL      = 1000;
const MAIN_CAM_MIN_GAP     = 16;
const LIVE_CAM_MIN_GAP     = 1000 / 30;

const JOG_ALLOWED_STATES = ["IDLE", "STOPPED", "PAUSED"];
const JOG_HEARTBEAT_INTERVAL = 100;

const CAMERA_ROLE_LABELS = {
    LEFT:   'ЛЕВАЯ',
    MIDDLE: 'ЦЕНТРАЛЬНАЯ',
    RIGHT:  'ПРАВАЯ',
};

// Трёхкамерная линия: все камеры смотрят в +0, маршрут готовится на +3,
// визуальный сброс — в окне +4. На ленте одновременно до 4 корпусов.
const LINE_INSPECT_POS = 0;
const LINE_SORT_POS = 3;
const LINE_DROP_POS = 4;
const LINE_SLOT_COUNT = 4;

const LINE_STATE_LABELS = {
    IDLE: 'ГОТОВА К ПУСКУ',
    RUNNING: 'РАБОТАЕТ',
    PAUSED: 'ПАУЗА · КОРРЕКЦИЯ ЛЕНТЫ',
    STOPPING: 'ОСТАНОВКА ЛИНИИ',
    STOPPED: 'ОСТАНОВЛЕНА',
    FAULT: 'АВАРИЯ',
    OFFLINE: 'НЕТ СВЯЗИ',
};

const CATEGORY_LABELS = {
    GOOD: 'ГОДНО',
    BAD: 'БРАК',
    CLEANUP: 'НА ЗАЧИСТКУ',
    UNKNOWN: 'НЕ ОПРЕДЕЛЕНО',
};

const UI_READY_TIMEOUT   = 20000;
const UI_READY_CHECK_INT = 100;

// ─── State ───────────────────────────────────────────────────

const state = {
    cameras:             [],
    // role -> физический Camera ID (из camera_mapping.json), для показа в интерфейсе
    cameraIds:           {},
    currentCamera:       null,
    mode:                'RULES',
    modePending:         false,
    startTime:           Date.now(),
    splashActive:        true,
    lastFrameTime:       0,
    lineState:           'IDLE',
    serverExitRequested: false,
    lastSeenVersion:     -1,
    currentVersion:      0,
    frameVersions:       {},
    bootDone:            false,
    bootDoneAt:          0,
    cameraHovered:       false,
    statusInterval:      null,
    bootInterval:        null,
    bootFetchBusy:       false,
    statusFetchBusy:     false,
    camerasFetchBusy:    false,

    statusReceived:      false,
    jogReceived:         false,
    uiRevealed:          false,

    jogActive:           false,
    jogBusy:             false,
    jogTogglePending:    false,
    jogHoldDirection:    null,
    jogHeartbeatTimer:   null,
    jogHeartbeatBusy:    false,
    jogReleasePending:   false,
    jogStartPromise:     null,
    distributorDiagnosticPending: false,
    distributorDiagnosticBackendBusy: false,

    selectedAnalysisActive: false,
    selectedAnalysisRole:   null,
    selectedAnalysisPending: false,

    liveFps:              0.0,
    // Backend сообщает целевой режим, но изображение меняется асинхронно.
    // Бейдж использует displayedFrameKind — режим кадра, уже загруженного в
    // главное окно, а не только состояние камеры на сервере.
    liveStreaming:        false,
    displayedFrameKind:   null, // 'live' | 'static' | 'analysis' | null
    pendingDisplayKind:   null,
    pendingDisplaySeq:    0,
    controlPending:       false,
    startPending:         false,
    backendControls:      {},
    offline:              false,
    lastStatusAt:         0,

    lastSentActiveCamera: null,
    pendingActiveCamera:  null,
    activeCameraRequestBusy: false,
    thresholdsRevision:   null,

    mainCamMode:          'pull',
    mainCamStreamRole:    null,
    mainCamStreamView:    null,
    mainCamAnalysisKey:   null,
    livePullTimer:        null,

    pendingAnalysisVersion: null,
    pendingFlushTimer:      null,
    lastLineStatus:         null,
};

// ─── Gallery state ───────────────────────────────────────────

let galleryMode   = 'debug';
let galleryPartId = null;
let galleryData   = null;

// ─── DOM cache ───────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

const els = {
    splash:           $('splash'),
    splashMessage:    $('splash-message'),
    splashProgress:   $('splash-progress-fill'),
    splashError:      $('splash-error'),
    splashErrorMsg:   $('splash-error-message'),
    splashExit:       $('splash-exit'),

    main:             $('main'),

    metricStep:       $('metric-step'),
    metricUptime:     $('metric-uptime'),

    previewStrip:     $('preview-strip'),
    cameraLabel:      $('camera-label'),
    modeBadge:        $('mode-badge'),
    mainCamera:       $('main-camera'),
    cameraOverlay:    $('camera-overlay'),
    viewModeToggle:   $('view-mode-toggle'),
    analyzeSelectedFrame: $('analyze-selected-frame'),
    cameraContainer:  null,

    dist1Blade:       $('dist1-blade'),
    dist2Blade:       $('dist2-blade'),
    distributorDiagnostics: $('distributor-diagnostics'),
    controlError:      $('control-error'),

    statsSummary:         $('stats-summary'),
    statsBody:            $('stats-body'),

    historyCards:     $('history-cards'),
    statsPanel:       $('stats-panel'),

    statTotal:        $('stat-total'),
    statGood:         $('stat-good'),
    statBad:          $('stat-bad'),
    statCleanup:      $('stat-cleanup'),
    statInline:       $('stat-inline'),
    lineCells:        $('line-cells'),
    processPhaseLabel: $('process-phase-label'),
    processPhaseStep: $('process-phase-step'),
    processStageTrack: $('process-stage-track'),

    jogPanel:         $('jog-panel'),

    frameAnalysisPanel: $('frame-analysis-panel'),

    archiveSettingsOpen: $('archive-settings-open'),
    archiveSettingsGroup: $('archive-settings-group'),
    archiveSettingsModal: $('archive-settings-modal'),
    archiveSettingsBackdrop: document.querySelector('.archive-settings-backdrop'),
    archiveSettingsClose: $('archive-settings-close'),
    archiveSettingsCancel: $('archive-settings-cancel'),
    archivePickFolder: $('archive-pick-folder'),
    archiveSettingsSave: $('archive-settings-save'),
    archiveRootPath: $('archive-root-path'),
    archiveSettingsValidation: $('archive-settings-validation'),
    archiveSettingsStatus: $('archive-settings-status'),
    archiveBatchId: $('archive-batch-id'),
    archiveBatchGood: $('archive-batch-good'),
    archiveBatchBad: $('archive-batch-bad'),
    archiveBatchCleanup: $('archive-batch-cleanup'),

    btnStart:         $('btn-start'),
    btnPause:         $('btn-pause'),
    btnResume:        $('btn-resume'),
    btnStop:          $('btn-stop'),
    btnExit:          $('btn-exit'),

    thresholdsPanel:      $('thresholds-panel'),
    thresholdsBody:       $('thresholds-body'),
    thresholdsStatus:     $('thresholds-status'),
    thresholdsSave:       $('thresholds-save'),
    thresholdsReset:      $('thresholds-reset'),

    galleryModal:     $('gallery-modal'),
    galleryGrid:      $('gallery-grid'),
    galleryPartId:    $('gallery-part-id'),
    galleryCategory:  $('gallery-category'),
    galleryDecision:  $('gallery-decision'),
    galleryTime:      $('gallery-time'),
    galleryBatch:     $('gallery-batch'),
    galleryDefects:   $('gallery-defects-list'),
    galleryClose:     $('gallery-close'),
    galleryModeDebug: $('gallery-mode-debug'),
    galleryModeRaw:   $('gallery-mode-raw'),
};

// ─── Double buffering — защищён от гонок счётчиком —──────────

const mainBuffer = new Image();
let _mainBufferSeq = 0;
let _mainBufferExpectedSeq = 0;
let mainBufferLoading = false;
let mainBufferRequestRole = null;
let mainBufferRequestView = null;
let mainBufferRequestVersion = null;

function showMainCameraFrame(source, kind) {
    if (!els.mainCamera) return;
    // Меняем показанный режим только когда именно этот src будет загружен
    // главным <img>. Последний уже видимый кадр сохраняет честный бейдж.
    state.pendingDisplayKind = kind;
    state.pendingDisplaySeq += 1;
    els.mainCamera.dataset.displaySeq = String(state.pendingDisplaySeq);
    els.mainCamera.src = source;
}

if (els.mainCamera) {
    els.mainCamera.addEventListener('load', () => {
        const seq = Number(els.mainCamera.dataset.displaySeq || 0);
        if (!seq || seq !== state.pendingDisplaySeq) return;
        state.displayedFrameKind = state.pendingDisplayKind;
        state.pendingDisplayKind = null;
        if (typeof applyLiveBadge === 'function') applyLiveBadge(state.jogActive);
    });
}

mainBuffer.addEventListener('load', () => {
    const pullMode = (state.mainCamMode === 'pull' || state.mainCamMode === 'live-pull');
    const mySeq = mainBuffer._seq || 0;
    // Если пришёл не последний запрос — игнорируем (гонка быстрой смены камеры)
    if (mySeq !== _mainBufferExpectedSeq) {
        mainBufferLoading = false;
        if (state.mainCamMode === 'pull' && typeof maybeRequestMainFrame === 'function') {
            setTimeout(maybeRequestMainFrame, 5);
        }
        return;
    }
    const requestIsCurrent = (
        pullMode
        && mainBufferRequestRole === state.currentCamera
        && mainBufferRequestView === state.mode
        && (state.mainCamMode === 'live-pull' || mainBufferRequestVersion === state.currentVersion)
    );
    if (requestIsCurrent) {
        showMainCameraFrame(
            mainBuffer.src,
            state.mainCamMode === 'live-pull' ? 'live' : 'static',
        );
    }
    mainBufferLoading = false;

    if (requestIsCurrent && state.pendingAnalysisVersion !== null && typeof flushPendingAnalysis === 'function') {
        flushPendingAnalysis();
    }

    if (state.mainCamMode === 'live-pull' && typeof scheduleNextLiveFrame === 'function') {
        scheduleNextLiveFrame(requestIsCurrent ? LIVE_CAM_MIN_GAP : 1);
    } else if (state.mainCamMode === 'pull' && !requestIsCurrent && typeof maybeRequestMainFrame === 'function') {
        maybeRequestMainFrame();
    }
});

mainBuffer.addEventListener('error', () => {
    // Если ошибка, но это уже не актуальный запрос — просто отпускаем загрузку
    const mySeq = mainBuffer._seq || 0;
    if (mySeq !== _mainBufferExpectedSeq) {
        mainBufferLoading = false;
        return;
    }
    mainBufferLoading = false;
    if (state.mainCamMode === 'live-pull' && typeof scheduleNextLiveFrame === 'function') {
        scheduleNextLiveFrame(60);
    } else if (state.mainCamMode === 'pull' && typeof maybeRequestMainFrame === 'function') {
        setTimeout(maybeRequestMainFrame, 60);
    }
});

// ─── API ─────────────────────────────────────────────────────

async function api(path, options = {}, controlFeedback = false) {
    try {
        const res = await fetch(path, options);
        const ct = res.headers.get('content-type') || '';
        const payload = ct.includes('json') ? await res.json() : await res.text();
        if (!res.ok) {
            const message = payload && payload.error ? payload.error : `${res.status}`;
            throw new Error(message);
        }
        return payload;
    } catch (err) {
        console.warn(`[API] ${path}:`, err.message);
        if (controlFeedback) showControlError(err.message || `Ошибка запроса ${path}`);
        return null;
    }
}

const apiGet = (path) => api(path);
const apiPost = (path, feedback = false) => api(path, {method: 'POST'}, feedback);
async function apiPostJson(path, payload, feedback = false) {
    return api(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload || {}),
    }, feedback);
}

function showControlError(message) {
    if (!els.controlError) return;
    setIfChanged(els.controlError, message || 'Неизвестная ошибка');
    els.controlError.classList.remove('is-hidden');
}
function clearControlError() {
    if (!els.controlError) return;
    els.controlError.classList.add('is-hidden');
    setIfChanged(els.controlError, '');
}

// ─── Helpers ─────────────────────────────────────────────────

function animateUiElement(el, className = 'ui-value-change') {
    if (!el || !state.uiRevealed) return;
    el.classList.remove(className);
    void el.offsetWidth;
    el.classList.add(className);
    setTimeout(() => el.classList.remove(className), 260);
}
function normalizeDisplayText(value) { return String(value).replace(/#\s*(?=\d)/g, '\u2116\u00a0'); }
function setIfChanged(el, value, animate = true) {
    if (!el) return;
    const text = normalizeDisplayText(value);
    if (el.textContent === text) return;
    el.textContent = text;
    if (animate && el.classList.contains('stats-value')) {
        animateUiElement(el);
    }
}
function cameraRoleLabel(role) { return CAMERA_ROLE_LABELS[role] || role || '—'; }
function lineStateLabel(value) { return LINE_STATE_LABELS[String(value || '').toUpperCase()] || value || '—'; }
function categoryLabel(value) { return CATEGORY_LABELS[String(value || '').toUpperCase()] || value || '—'; }
function formatFrameRate(value) { const n = Math.round(Number(value || 0)); return `${n} КАДР/С`; }
// Немедленный статус — отменяет запланированный тик и делает внеплановый запрос
let _statusLoopTimer = null;
let _statusImmediatePending = false;

function requestImmediateStatus() {
    if (_statusImmediatePending) return;
    _statusImmediatePending = true;
    if (_statusLoopTimer) { clearTimeout(_statusLoopTimer); _statusLoopTimer = null; }
    setTimeout(async () => {
        _statusImmediatePending = false;
        await fetchStatus();
        scheduleNextStatusTick();
    }, 10);
}

function scheduleNextStatusTick() {
    if (_statusLoopTimer) clearTimeout(_statusLoopTimer);
    const next = getStatusInterval();
    _statusLoopTimer = setTimeout(async () => {
        _statusLoopTimer = null;
        await fetchStatus();
        scheduleNextStatusTick();
    }, next);
    state.statusInterval = _statusLoopTimer;
}

async function sendActiveCameraIfChanged(role) {
    if (!role || state.offline) return;
    if (state.lastSentActiveCamera === role && !state.activeCameraRequestBusy) return;
    state.pendingActiveCamera = role;
    if (state.activeCameraRequestBusy) return;

    state.activeCameraRequestBusy = true;
    try {
        while (state.pendingActiveCamera) {
            const target = state.pendingActiveCamera;
            state.pendingActiveCamera = null;
            const result = await apiPost(`/api/active_camera/${encodeURIComponent(target)}`);
            if (result) state.lastSentActiveCamera = target;
            else state.lastSentActiveCamera = null;
        }
    } finally {
        state.activeCameraRequestBusy = false;
    }
    if (!state.offline && state.currentCamera && state.lastSentActiveCamera !== state.currentCamera) {
        setTimeout(() => sendActiveCameraIfChanged(state.currentCamera), 400);
    }
}

// ─── Adaptive polling ────────────────────────────────────────

function getStatusInterval() {
    const s = state.lineState;
    if (state.controlPending || state.distributorDiagnosticPending || state.distributorDiagnosticBackendBusy) return STATUS_INTERVAL_MOTION;
    if (s === 'RUNNING' || s === 'PAUSED' || s === 'STOPPING') return STATUS_INTERVAL_FAST;
    if (state.serverExitRequested) return STATUS_INTERVAL_FAST;
    if (state.jogActive) return STATUS_INTERVAL_FAST;
    return STATUS_INTERVAL_IDLE;
}

function startStatusPolling() {
    if (_statusLoopTimer) clearTimeout(_statusLoopTimer);
    if (state.statusInterval) {
        try { clearInterval(state.statusInterval); } catch (_) {}
        try { clearTimeout(state.statusInterval); } catch (_) {}
    }
    scheduleNextStatusTick();
}

