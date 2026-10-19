// status.js — Line Monitor UI module (stabilized)
// - защита от гонок в fetchStatus через очередность и версионирование
// - стабильный polling через core.js (setTimeout loop)
// - синхронизация монитор->UI сохранена
'use strict';

function isInspectionDisplayPhase(phase) {
    const value = String(phase || '').toUpperCase();
    return value.includes('CAMERA')
        || value.includes('ANALYSIS')
        || value.includes('MODELS')
        || value.includes('GEOMETRY')
        || value.includes('PRESENCE')
        || value.includes('DECISION')
        || value.includes('RECORD')
        || value === 'INSPECT_ANALYSIS'
        || value === 'PUBLISH';
}

function updateOperationalAccordions(lineState) {
    const fullyStopped = lineState === 'IDLE' || lineState === 'STOPPED';
    if (els.statsBody) els.statsBody.classList.remove('is-collapsed');
    if (els.statsSummary) els.statsSummary.classList.add('is-open');
    if (els.distributorDiagnostics) {
        const compact = !fullyStopped;
        els.distributorDiagnostics.classList.toggle('controls-collapsed', compact);
        els.distributorDiagnostics.querySelectorAll('.blade-diagnostic-grid').forEach(grid => {
            grid.classList.toggle('is-collapsed', compact);
        });
    }
}

function setBladeMarkerPosition(marker, percent) {
    if (!marker) return;
    const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    marker.style.left = `${normalized}%`;
}

// Очередность запросов статуса — избегаем наложения
let _statusSeq = 0;
let _lastHandledStatusSeq = 0;

async function fetchStatus() {
    if (!state.bootDone) return;
    if (state.statusFetchBusy) {
        // Если уже идёт запрос, пропускаем тик — следующий тик возьмёт свежие данные
        return;
    }
    const mySeq = ++_statusSeq;
    state.statusFetchBusy = true;

    const status = await apiGet('/api/status');
    state.statusFetchBusy = false;

    if (!status) {
        const reference = state.lastStatusAt || state.bootDoneAt;
        if (reference > 0 && Date.now() - reference >= STATUS_OFFLINE_AFTER) markUiOffline();
        return;
    }

    // Если за время запроса пришёл более свежий ответ (через immediate), старый игнорируем
    if (mySeq < _lastHandledStatusSeq) return;
    _lastHandledStatusSeq = mySeq;

    state.lastStatusAt = Date.now();
    state.statusReceived = true;
    if (state.offline) {
        state.offline = false;
        els.main.classList.remove('ui-offline');
        clearControlError();
    }

    if (status.frame_versions && typeof status.frame_versions === 'object') {
        state.frameVersions = {...status.frame_versions};
    }

    if (typeof status.thresholds_revision === 'number') {
        if (state.thresholdsRevision !== null && status.thresholds_revision !== state.thresholdsRevision && typeof updateThresholdsPanel === 'function') {
            updateThresholdsPanel(true);
        }
        state.thresholdsRevision = status.thresholds_revision;
    }

    const incomingVersion = typeof status.frame_version === 'number' ? status.frame_version : null;
    const newPublishArrived = incomingVersion !== null && incomingVersion !== state.lastSeenVersion;

    if (incomingVersion !== null) {
        state.currentVersion = incomingVersion;
        if (newPublishArrived && state.mainCamMode === 'pull' && !state.splashActive) {
            maybeRequestMainFrame();
        }
    }

    if (typeof updateArchiveStatus === 'function') {
        updateArchiveStatus(status.archive || null);
    }

    const lineStatusPayload = status.line_status || {};
    const liveInfo = lineStatusPayload.live || {};
    const staticRoles = Array.isArray(liveInfo.static_roles) ? liveInfo.static_roles : [];
    const processInfo = lineStatusPayload.process || {};
    const inspectionRoles = Array.isArray(processInfo.inspection_roles) ? processInfo.inspection_roles : [];
    const processPhase = String(processInfo.phase || '').toUpperCase();
    const inspectionDisplay = inspectionRoles.includes(state.currentCamera)
        && isInspectionDisplayPhase(processPhase);
    const selectedRoleStatic = liveInfo.all_roles_static === true
        || staticRoles.includes(state.currentCamera)
        || inspectionDisplay
        // Совместимость со статусом backend до ролевых пауз.
        || (liveInfo.static === true && liveInfo.streaming === false && staticRoles.length === 0);
    const staticPublish = (
        newPublishArrived
        && incomingVersion > 0
        && !state.splashActive
        && selectedRoleStatic
        && state.mainCamMode === 'pull'
    );
    if (staticPublish) {
        state.pendingAnalysisVersion = incomingVersion;
        armPendingFlushFallback();
    }
    state.lastLineStatus = lineStatusPayload;

    const oldState = state.lineState;
    updateLineStatus(lineStatusPayload);
    updateRecentParts(status.recent_parts || []);
    updateMode(status.mode || 'RULES');

    if (state.lineState !== oldState) {
        startStatusPolling();
    }

    checkUiReady();
}

function markUiOffline() {
    if (state.offline) return;
    state.offline = true;
    state.controlPending = false;
    state.startPending = false;
    state.jogTogglePending = false;
    state.distributorDiagnosticPending = false;
    state.jogActive = false;
    state.jogBusy = false;
    if (typeof clearLivePullTimer === 'function') clearLivePullTimer();
    state.mainCamMode = 'pull';
    mainBufferLoading = false;
    els.main.classList.add('ui-offline');
    updateProcessPhaseLabel('OFFLINE');
    showControlError('Нет связи с backend. Все команды заблокированы.');
    releaseJogHoldBestEffort('backend offline');
    applyButtonsForState('OFFLINE', true, {});
    updateDistributorDiagnosticControls({diagnostic_allowed: false, diagnostic_busy: true});
    if (els.analyzeSelectedFrame) els.analyzeSelectedFrame.disabled = true;
    updateViewModeControls();
    if (els.jogPanel) {
        els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(button => { button.disabled = true; });
    }
    if (typeof updateThresholdsPanel === 'function') updateThresholdsPanel();
    if (typeof updateArchiveStatus === 'function') updateArchiveStatus(null);
    updateStateOverlay({state: 'OFFLINE', in_line: 0});
}

const PROCESS_PHASE_LABELS = {
    START_POSITIONING: 'ПОДГОТОВКА · ПОЗИЦИОНИРОВАНИЕ',
    READY: 'ЦИКЛ ЗАПУЩЕН',
    INITIAL_INSPECTION: 'СТАРТ · КОНТРОЛЬ ПОД КАМЕРАМИ',
    ROUTE_PREPARE: 'ДВИЖЕНИЕ · ПОДГОТОВКА МАРШРУТА',
    CONVEYOR_COMMAND: 'ДВИЖЕНИЕ · КОМАНДА ЛЕНТЕ',
    CONVEYOR_MOVING: 'ДВИЖЕНИЕ · ЛЕНТА В ХОДЕ',
    MOTION: 'ДВИЖЕНИЕ · ЛЕНТА В ХОДЕ',
    CONVEYOR_CONFIRMED: 'СТОП · ОСТАНОВКА ПОДТВЕРЖДЕНА',
    PART_TRANSFER: 'СТОП · ПЕРЕДАЧА ЧЕРЕЗ РАСПРЕДЕЛИТЕЛЬ',
    SETTLE: 'СТОП · ЗАТУХАНИЕ ВИБРАЦИИ',
    CAMERA_CAPTURE: 'КАДР · ЗАХВАТ СТОП-КАДРА',
    CAPTURE: 'КАДР · ЗАХВАТ СТОП-КАДРА',
    INSPECT_ANALYSIS: 'ИНСПЕКЦИЯ · АНАЛИЗ',
    INSPECT_MODELS: 'ИНСПЕКЦИЯ · МОДЕЛИ',
    INSPECT_PRESENCE: 'ИНСПЕКЦИЯ · ПРОВЕРКА НАЛИЧИЯ',
    INSPECT_PRESENT: 'ИНСПЕКЦИЯ · НАЛИЧИЕ ПОДТВЕРЖДЕНО',
    INSPECT_GEOMETRY: 'ИНСПЕКЦИЯ · ПОСТРОЕНИЕ ГЕОМЕТРИИ',
    INSPECT_DECISION: 'ИНСПЕКЦИЯ · РЕШЕНИЕ ПРАВИЛ',
    INSPECT_FRAME_RECORD: 'ИНСПЕКЦИЯ · ЗАПИСЬ РАЗМЕТКИ',
    INSPECT_FRAME_RECORDED: 'ИНСПЕКЦИЯ · РАЗМЕТКА ГОТОВА',
    INSPECT_RESULT_RECORDED: 'ИНСПЕКЦИЯ · РЕШЕНИЕ ЗАПИСАНО',
    ANALYSIS: 'АНАЛИЗ · МОДЕЛИ И ГЕОМЕТРИЯ',
    ANALYSIS_REVIEW: 'РЕВЬЮ · ПРОСМОТР РЕЗУЛЬТАТА',
    STEP_COMPLETE: 'ИТОГ · ШАГ ЗАВЕРШЁН',
    PUBLISH: 'ИТОГ · ПУБЛИКАЦИЯ РЕЗУЛЬТАТА',
    FINAL_DECISION_ARCHIVED: 'ИТОГ · РЕШЕНИЕ В АРХИВЕ',
    FINAL_DECISION_NOT_ARCHIVED: 'ИТОГ · РЕШЕНИЕ БЕЗ АРХИВА',
    PAUSE_REQUESTED: 'ПАУЗА · ОЖИДАНИЕ ГРАНИЦЫ ШАГА',
    RESUMED: 'ВОЗОБНОВЛЕНИЕ · СВЕЖИЙ CAPTURE',
    STOPPING: 'ОСТАНОВКА · ВЫВОД КОРПУСОВ',
    DRAINING: 'ОСТАНОВКА · ВЫВОД КОРПУСОВ',
    STOPPED: 'ОСТАНОВЛЕНО · ЛИНИЯ ПУСТА',
    JOG: 'JOG · РУЧНОЕ ПЕРЕМЕЩЕНИЕ',
    JOG_HOLD: 'JOG · УДЕРЖИВАЕМОЕ ДВИЖЕНИЕ',
    JOG_STOPPED: 'JOG · ДВИЖЕНИЕ ОСТАНОВЛЕНО',
    SELECTED_ANALYSIS: 'ДИАГНОСТИКА · АНАЛИЗ КАДРА',
    SELECTED_MODEL_ANALYSIS: 'ДИАГНОСТИКА · МОДЕЛИ И ПРАВИЛА',
    SELECTED_MODEL_READY: 'ДИАГНОСТИКА · РЕЗУЛЬТАТ ГОТОВ',
    CAMERA_DIAGNOSTIC: 'ДИАГНОСТИКА · КАМЕРЫ',
    VISION_RULE_DIAGNOSTIC: 'ДИАГНОСТИКА · МОДЕЛИ И ПРАВИЛА',
    DISTRIBUTOR_DIAGNOSTIC: 'ДИАГНОСТИКА · РАСПРЕДЕЛИТЕЛЬ',
    DIAGNOSTIC_DONE: 'ДИАГНОСТИКА · ПРОВЕРКА ЗАВЕРШЕНА',
    FAULT: 'АВАРИЯ · ЦИКЛ ОСТАНОВЛЕН',
    OFFLINE: 'НЕТ СВЯЗИ',
};

const PROCESS_STAGE_ORDER = ['START', 'MOTION', 'SETTLE', 'CAPTURE', 'ANALYSIS', 'REVIEW', 'PUBLISH'];
const PROCESS_STAGE_PHASES = {
    START: new Set(['START_POSITIONING', 'READY', 'INITIAL_INSPECTION']),
    MOTION: new Set(['ROUTE_PREPARE', 'CONVEYOR_COMMAND', 'CONVEYOR_MOVING', 'MOTION']),
    SETTLE: new Set(['CONVEYOR_CONFIRMED', 'PART_TRANSFER', 'SETTLE']),
    CAPTURE: new Set(['CAMERA_CAPTURE', 'CAPTURE']),
    ANALYSIS: new Set([
        'ANALYSIS', 'INSPECT_ANALYSIS', 'INSPECT_MODELS',
        'INSPECT_PRESENCE', 'INSPECT_PRESENT', 'INSPECT_GEOMETRY',
        'INSPECT_DECISION', 'INSPECT_FRAME_RECORD', 'INSPECT_FRAME_RECORDED',
        'INSPECT_RESULT_RECORDED', 'VISION_RULE_DIAGNOSTIC',
        'SELECTED_MODEL_ANALYSIS', 'SELECTED_MODEL_READY',
    ]),
    REVIEW: new Set(['ANALYSIS_REVIEW']),
    PUBLISH: new Set([
        'STEP_COMPLETE', 'PUBLISH', 'FINAL_DECISION_ARCHIVED',
        'FINAL_DECISION_NOT_ARCHIVED',
    ]),
};

function processStageForPhase(phase) {
    return PROCESS_STAGE_ORDER.find(stage => PROCESS_STAGE_PHASES[stage].has(phase)) || null;
}

function updateProcessStageTrack(stage, lineState) {
    const track = els.processStageTrack || document.getElementById('process-stage-track');
    if (!track) return;
    const activeState = String(lineState || 'IDLE').toUpperCase();
    track.classList.toggle('is-paused', activeState === 'PAUSED');
    track.dataset.lineState = activeState;
    const resetTrack = !stage && ['IDLE', 'STOPPED', 'OFFLINE'].includes(activeState);
    const effectiveStage = stage || (resetTrack ? null : (track.dataset.activeStage || null));
    track.dataset.activeStage = effectiveStage || '';
    const activeIndex = PROCESS_STAGE_ORDER.indexOf(effectiveStage);
    track.querySelectorAll('[data-process-stage]').forEach(node => {
        const nodeStage = String(node.dataset.processStage || '').toUpperCase();
        const nodeIndex = PROCESS_STAGE_ORDER.indexOf(nodeStage);
        node.classList.toggle('is-active', nodeStage === effectiveStage);
        node.classList.toggle('is-done', activeIndex >= 0 && nodeIndex >= 0 && nodeIndex < activeIndex);
    });
}

function updateProcessPhaseLabel(lineState, process = {}) {
    const phaseEl = els.processPhaseLabel || document.getElementById('process-phase-label');
    if (!phaseEl) return;
    const activeState = String(lineState || 'IDLE').toUpperCase();
    const phase = String(process.phase || '').toUpperCase();
    const processLabel = String(process.label || '').trim();
    const mappedLabel = PROCESS_PHASE_LABELS[phase];
    const hasProcessPhase = !!phase && phase !== 'IDLE' && phase !== activeState;
    const label = hasProcessPhase
        ? (mappedLabel || processLabel || phase.replace(/_/g, ' '))
        : lineStateLabel(activeState);
    const detailParts = [];
    if (processLabel && processLabel !== label) detailParts.push(processLabel);
    if (process.part_id != null) detailParts.push(`КОРПУС \u2116\u00a0${process.part_id}`);
    const captureRoles = Array.isArray(process.capture_roles) ? process.capture_roles : [];
    if (captureRoles.length && isInspectionDisplayPhase(phase)) {
        detailParts.push(`КАМЕР: ${captureRoles.length}`);
    }
    if (!detailParts.length && hasProcessPhase && (mappedLabel || processLabel)) {
        detailParts.push(mappedLabel || processLabel);
    }
    const detail = detailParts.join(' · ') || lineStateLabel(activeState);
    const processStep = process.step != null ? process.step : null;
    setIfChanged(phaseEl, label);
    if (els.processPhaseStep && processStep !== null) setIfChanged(els.processPhaseStep, `ШАГ ${processStep}`);
    phaseEl.dataset.lineState = activeState;
    phaseEl.dataset.processPhase = phase;
    phaseEl.title = detail;
    phaseEl.style.opacity = '1';
    updateProcessStageTrack(processStageForPhase(phase), activeState);
    if (activeState === 'FAULT' || activeState === 'OFFLINE') phaseEl.style.color = 'var(--bad)';
    else if (activeState === 'PAUSED' || activeState === 'STOPPING') phaseEl.style.color = 'var(--warn)';
    else if (processStageForPhase(phase) === 'CAPTURE' || processStageForPhase(phase) === 'SETTLE') phaseEl.style.color = 'var(--warn)';
    else if (processStageForPhase(phase) === 'ANALYSIS' || processStageForPhase(phase) === 'REVIEW') phaseEl.style.color = 'var(--accent)';
    else if (activeState === 'RUNNING') phaseEl.style.color = 'var(--ok)';
    else phaseEl.style.color = 'var(--text-dim)';
}

function updateLineStatus(ls) {
    const lineState = ls.state || 'IDLE';
    const exitRequested = !!ls.exit_requested;

    state.lineState = lineState;
    state.serverExitRequested = exitRequested;
    if (!['IDLE', 'STOPPED'].includes(lineState)) state.startPending = false;
    updateOperationalAccordions(lineState);

    updateProcessPhaseLabel(lineState, ls.process || {});
    setIfChanged(els.metricStep, ls.step || 0);

    state.backendControls = ls.controls || {};
    applyButtonsForState(lineState, exitRequested, state.backendControls);
    updateViewModeControls();

    setIfChanged(els.statTotal, ls.total || 0);
    setIfChanged(els.statGood, ls.good || 0);
    setIfChanged(els.statBad, ls.rejected || 0);
    setIfChanged(els.statCleanup, ls.cleanup || 0);

    const pendingAnalysis = state.pendingAnalysisVersion !== null;
    const inLine = pendingAnalysis ? _appliedInLine : (ls.in_line || 0);
    setIfChanged(els.statInline, `${inLine} / ${LINE_SLOT_COUNT}`);

    const process = ls.process || {};
    _updateDistributorRoute(ls);
    updateLineCells(ls.line_parts || [], process);
    updateDistributorDiagnosticControls(ls);
    updateSelectedAnalysisStatus(ls);
    if (!pendingAnalysis && typeof updateNewFrameAnalysisStatus === 'function') {
        updateNewFrameAnalysisStatus(ls);
    }

    // В блоке распределителя остались только ползунки: бегунки двигаются
    // по координатам осей, текстовые значения из HMI убраны.
    const d1Pos = Math.max(0, Number(ls.dist1_position || 0));
    const d1Max = Math.max(1, Number(ls.dist1_max || 340));
    if (els.dist1Blade) {
        const d1Percent = Math.max(0, Math.min(100, d1Pos / d1Max * 100));
        setBladeMarkerPosition(els.dist1Blade, d1Percent);
    }

    const d2Pos = Math.max(0, Number(ls.dist2_position || 0));
    const d2Max = Math.max(1, Number(ls.dist2_max || 340));
    if (els.dist2Blade) {
        const d2Percent = Math.max(0, Math.min(100, d2Pos / d2Max * 100));
        setBladeMarkerPosition(els.dist2Blade, d2Percent);
    }

    updateJogState(ls.jog || null);
    updateStateOverlay(ls);
    handleJogAutoToggle(lineState, ls.jog || null);

    if (typeof updatePanelFace === 'function') updatePanelFace(ls);

    if (typeof updateThresholdsPanel === 'function') updateThresholdsPanel();
    if (typeof updateArchiveButton === 'function') updateArchiveButton();
}

// ─── Line cells ──────────────────────────────────────────────
const _lineTokens = new Map();
// Bodies removed by the backend at +3 remain physically on the stencil until
// the next conveyor step carries them to +4.
const _lineDepartingTokens = new Set();
let _lineSyncDone = false;
let _appliedLineParts = [];
let _appliedInLine = 0;

const LINE_APPEAR_DURATION_MS = 560;
const LINE_MOVE_SLOWDOWN = 1.5;
const LINE_MOVE_MIN_MS = 420;
const LINE_MOVE_MAX_MS = 900;
const LINE_DROP_MIN_MS = 720;
const LINE_DROP_MAX_MS = 1100;

function lineMoveDuration(process = {}) {
    const conv = process.conveyor || {};
    const speed = Number(conv.speed) || 0;
    if (!speed) return 630;
    // Keep the visual step deliberately slower than the physical speed so
    // the arrival, horizontal transfer and exit remain easy to follow.
    const physicalDuration = 8400000 / speed;
    return Math.max(
        LINE_MOVE_MIN_MS,
        Math.min(LINE_MOVE_MAX_MS, Math.round(physicalDuration * LINE_MOVE_SLOWDOWN)),
    );
}

function lineDropDuration(process = {}) {
    return Math.max(
        LINE_DROP_MIN_MS,
        Math.min(LINE_DROP_MAX_MS, Math.round(lineMoveDuration(process) * 1.1)),
    );
}

// ``CONVEYOR_CONFIRMED`` is published after the controller has already
// advanced the logical positions.  Treating every phase containing the word
// CONVEYOR as motion advances the same token for a second time, then makes it
// jump back on SETTLE.  Keep the transport phases explicit so the animation
// has exactly one step per physical move.
function isConveyorTransportPhase(phase) {
    const p = String(phase || '').toUpperCase();
    return p === 'CONVEYOR'
        || p === 'CONVEYOR_COMMAND'
        || p === 'CONVEYOR_MOVING'
        || p === 'MOTION'
        || p.startsWith('MOTION_');
}

function _lineCellRects(cells) {
    if (!els.lineCells) return {containerRect: {width: 0}, rects: {}};
    const containerRect = els.lineCells.getBoundingClientRect();
    const rects = {};
    cells.forEach(cell => {
        const r = cell.getBoundingClientRect();
        rects[Number(cell.dataset.pos)] = {
            left: r.left - containerRect.left,
            top: r.top - containerRect.top,
            width: r.width,
            height: r.height,
        };
    });
    return {containerRect, rects};
}

function _applyTokenCategory(el, category) {
    el.classList.remove('cell-good', 'cell-bad', 'cell-cleanup');
    if (category === 'BAD') el.classList.add('cell-bad');
    else if (category === 'CLEANUP') el.classList.add('cell-cleanup');
    else if (category === 'GOOD') el.classList.add('cell-good');
}


const ROUTE_CATEGORIES = ['GOOD', 'BAD', 'CLEANUP'];

function _resolveDistributorRoute(ls) {
    const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
    const process = ls.process || {};
    const phaseText = String(process.phase || '').toUpperCase();
    const routingPhase = phaseText.includes('ROUTE') || phaseText.includes('DROP');
    let part = null;
    if (routingPhase && process.part_id != null) {
        part = parts.find(item => Number(item.id) === Number(process.part_id)) || null;
    }
    if (!part) part = parts.find(item => Number(item.position) === 7) || null;
    let category = part ? String(part.category || '').toUpperCase() : '';
    if (!ROUTE_CATEGORIES.includes(category)) category = '';
    if (!category) {
        const d1State = String(ls.dist1_state || '').toUpperCase();
        const d1ToDist2 = ['TO_DIST2', 'MOVING_TO_DIST2'].includes(d1State)
            || (d1State !== 'MOVING_TO_GOOD' && Number(ls.dist1_position || 0) > 0);
        // Если деталей на +3 нет (в частности, при ручной диагностике),
        // цвет обязан всё равно показывать выбранное положение заслонок.
        // Раньше fallback назначал только BAD/CLEANUP, поэтому возврат
        // DIST1 в GOOD снимал все route-классы и панель теряла зелёный цвет.
        category = d1ToDist2
            ? (String(ls.dist2_target || '').toUpperCase() === 'CLEANUP' ? 'CLEANUP' : 'BAD')
            : 'GOOD';
    }
    return category;
}

function _updateDistributorRoute(ls) {
    const panel = els.distributorDiagnostics;
    if (!panel) return;
    panel.classList.remove('route-good', 'route-bad', 'route-cleanup');
    const category = _resolveDistributorRoute(ls);
    if (category === 'GOOD') panel.classList.add('route-good');
    else if (category === 'BAD') panel.classList.add('route-bad');
    else if (category === 'CLEANUP') panel.classList.add('route-cleanup');
}

function _removeStencilToken(token) {
    (token.pieces || []).forEach(piece => piece.remove());
    if (token.exitTimer) clearTimeout(token.exitTimer);
}

function updateLineCells(lineParts, process = {}) {
    if (!els.lineCells) return;
    const cells = [...els.lineCells.querySelectorAll('.line-cell[data-pos]')];
    const {containerRect, rects} = _lineCellRects(cells);
    if (!containerRect.width || !rects[0] || !rects[0].width) return;

    const phase = String(process.phase || '').toUpperCase();
    const moving = isConveyorTransportPhase(phase);
    const duration = lineMoveDuration(process);
    const dropDuration = lineDropDuration(process);
    els.lineCells.style.setProperty('--appear-duration', `${LINE_APPEAR_DURATION_MS}ms`);
    els.lineCells.style.setProperty('--move-duration', `${duration}ms`);
    els.lineCells.style.setProperty('--drop-duration', `${dropDuration}ms`);

    const pendingAnalysis = state.pendingAnalysisVersion !== null;
    const appliedById = new Map(_appliedLineParts.map(part => [part.id, part.category]));
    const wanted = new Map();
    for (const part of lineParts || []) {
        const id = Number(part.id);
        if (!Number.isFinite(id) || (pendingAnalysis && !appliedById.has(id))) continue;
        const category = pendingAnalysis && appliedById.has(id)
            ? appliedById.get(id) : String(part.category || '').toUpperCase();
        let position = Math.max(0, Math.min(Number(part.position) || 0, LINE_SORT_POS));
        const wasInDropWindow = _lineTokens.get(id)?.position === LINE_DROP_POS;
        // Статус во время хода относится к позиции до подтверждения остановки.
        // Визуально все корпуса делают один и тот же непрерывный шаг. После
        // подтверждения корпус остаётся виден в +4 до отдельного падения.
        // «dropping» на +3 означает, что корпус уже прошёл заслонки и падает
        // в +4 — кроме фазы ROUTE_PREPARE, где маршрут ещё только готовится,
        // а лента стоит. Поэтому не полагаемся только на то, что фронтенд
        // успел увидеть фазу хода: если ход пропущен (редкий медленный тик),
        // корпус всё равно уезжает в +4 и падает, а не «замирает» на +3.
        if (moving) position = part.dropping ? LINE_DROP_POS : Math.min(position + 1, LINE_DROP_POS);
        else if (part.dropping && (wasInDropWindow || phase !== 'ROUTE_PREPARE')) position = LINE_DROP_POS;
        wanted.set(id, {position, category, dropping: !!part.dropping});
    }

    // Удалённый из статуса корпус уже стоит в +4: только теперь он падает
        // под вагон. Никакого исчезновения или выхода во время горизонтального
        // шага нет.
    for (const [id, token] of [..._lineTokens.entries()]) {
        if (wanted.has(id)) continue;
        _lineTokens.delete(id);
        if (token.position === LINE_DROP_POS) {
            token.pieces.forEach(piece => piece.classList.add('token-exiting'));
            token.exitTimer = setTimeout(() => _removeStencilToken(token), dropDuration);
        } else if (token.position === LINE_SORT_POS) {
            // The logical list may release a body at sorting before the next
            // step. Keep its physical body visible until that step reaches +4.
            token.departing = true;
            token.movedToExit = false;
            _lineDepartingTokens.add(token);
        } else {
            _removeStencilToken(token);
        }
    }

    for (const token of [..._lineDepartingTokens]) {
        token.previousPosition = token.position;
        if (moving && token.position === LINE_SORT_POS) {
            token.position = LINE_DROP_POS;
            token.movedToExit = true;
        } else if (!moving && token.position === LINE_DROP_POS && token.movedToExit && !token.exitTimer) {
            token.pieces.forEach(piece => piece.classList.add('token-exiting'));
            token.exitTimer = setTimeout(() => {
                _lineDepartingTokens.delete(token);
                _removeStencilToken(token);
            }, dropDuration);
        }
    }

    for (const [id, meta] of wanted) {
        let token = _lineTokens.get(id);
        if (!token) {
            token = {id, position: meta.position, category: meta.category, pieces: [], entering: _lineSyncDone};
            _lineTokens.set(id, token);
        }
        token.previousPosition = token.position;
        token.position = meta.position;
        token.category = meta.category;
        token.dropping = meta.dropping;
    }

    // Трафарет: корпус находится за стенкой. Для каждого окна создаётся
    // обрезанный фрагмент корпуса. Если в прорезь одновременно попадают два
    // корпуса, фрагменты обоих не рисуются — окно остаётся пустым.
    const bodyWidth = rects[0].width * 0.78;
    const visibleByCell = new Map();
    const visualTokens = [..._lineTokens.values(), ..._lineDepartingTokens];
    for (const token of visualTokens) {
        const center = rects[token.position].left + rects[token.position].width / 2;
        const left = center - bodyWidth / 2;
        token.bodyLeft = left;
        for (let pos = 0; pos <= LINE_DROP_POS; pos += 1) {
            const r = rects[pos];
            if (Math.min(left + bodyWidth, r.left + r.width) > Math.max(left, r.left)) {
                const list = visibleByCell.get(pos) || [];
                list.push(token);
                visibleByCell.set(pos, list);
            }
        }
    }

    for (const token of visualTokens) {
        const relevant = new Set();
        [token.previousPosition, token.position].forEach(pos => {
            for (let i = Math.max(0, pos - 1); i <= Math.min(8, pos + 1); i += 1) relevant.add(i);
        });
        const previous = new Map(token.pieces.map(piece => [Number(piece.parentElement.dataset.pos), piece]));
        const nextPieces = [];
        for (const pos of relevant) {
            const cell = cells.find(item => Number(item.dataset.pos) === pos);
            if (!cell) continue;
            let piece = previous.get(pos);
            const isNewPiece = !piece;
            const movedHorizontally = token.previousPosition !== token.position;
            if (isNewPiece) {
                piece = document.createElement('div');
                piece.className = 'line-token-piece';
                piece.dataset.partId = String(token.id);
                // A fragment entering the neighbouring aperture starts at
                // its real previous coordinate. This makes one body visible
                // in two windows during the continuous horizontal step.
                if (movedHorizontally && rects[token.previousPosition]) {
                    const previousRect = rects[token.previousPosition];
                    const previousLeft = previousRect.left
                        + previousRect.width / 2 - bodyWidth / 2;
                    piece.style.left = `${previousLeft - rects[pos].left}px`;
                }
                cell.appendChild(piece);
                if (token.entering && token.position === 0) piece.classList.add('token-entering');
            }
            const conflict = (visibleByCell.get(pos) || []).length > 1;
            piece.classList.toggle('is-hidden-by-conflict', conflict);
            piece.classList.remove('cell-good', 'cell-bad', 'cell-cleanup', 'token-exiting');
            _applyTokenCategory(piece, token.category);
            piece.style.width = `${bodyWidth}px`;
            const targetLeft = `${token.bodyLeft - rects[pos].left}px`;
            if (isNewPiece && movedHorizontally) {
                requestAnimationFrame(() => { piece.style.left = targetLeft; });
            } else {
                piece.style.left = targetLeft;
            }
            piece.textContent = `\u2116\u00a0${token.id}`;
            piece.title = `Корпус \u2116\u00a0${token.id} · ${categoryLabel(token.category)}`;
            nextPieces.push(piece);
        }
        token.pieces.forEach(piece => { if (!nextPieces.includes(piece)) piece.remove(); });
        token.pieces = nextPieces;
        if (token.entering) {
            requestAnimationFrame(() => token.pieces.forEach(piece => piece.classList.remove('token-entering')));
            token.entering = false;
        }
    }

    if (!pendingAnalysis) {
        _appliedLineParts = (lineParts || []).map(part => ({
            id: Number(part.id), position: Number(part.position) || 0,
            category: String(part.category || '').toUpperCase(),
        }));
        _appliedInLine = _appliedLineParts.length;
    }
    _lineSyncDone = true;
}
const PENDING_VISUAL_TIMEOUT_MS = 1500;
function armPendingFlushFallback() {
    if (state.pendingFlushTimer) clearTimeout(state.pendingFlushTimer);
    state.pendingFlushTimer = setTimeout(() => {
        state.pendingFlushTimer = null;
        flushPendingAnalysis();
    }, PENDING_VISUAL_TIMEOUT_MS);
}
function flushPendingAnalysis() {
    if (state.pendingAnalysisVersion === null) return;
    state.pendingAnalysisVersion = null;
    if (state.pendingFlushTimer) { clearTimeout(state.pendingFlushTimer); state.pendingFlushTimer = null; }
    const ls = state.lastLineStatus;
    if (ls) {
        updateLineCells(ls.line_parts || [], ls.process || {});
        if (typeof updateNewFrameAnalysisStatus === 'function') updateNewFrameAnalysisStatus(ls);
    }
    if (typeof refreshPreviewStrip === 'function') refreshPreviewStrip();
    if (typeof faSyncScroll === 'function') {
        try { requestAnimationFrame(() => faSyncScroll()); } catch (_) {}
    }
}
