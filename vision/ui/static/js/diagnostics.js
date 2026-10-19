// diagnostics.js — стабилизированный модуль диагностики и анализа кадра
'use strict';

function disableSelectedAnalysisButton() {
    if (els.analyzeSelectedFrame) els.analyzeSelectedFrame.disabled = true;
}

function updateDistributorDiagnosticControls(ls) {
    if (!els.distributorDiagnostics) return;
    const allowed = ls.diagnostic_allowed === true && (!ls.controls || ls.controls.distributor_diagnostic === true) && !state.offline;
    state.distributorDiagnosticBackendBusy = ls.diagnostic_busy === true;
    const busy = state.distributorDiagnosticBackendBusy || state.distributorDiagnosticPending;
    els.distributorDiagnostics.querySelectorAll('button').forEach(button => {
        button.disabled = !allowed || busy;
        button.classList.toggle('pending', busy);
    });
}

function setupDistributorDiagnostics() {
    if (!els.distributorDiagnostics) return;
    els.distributorDiagnostics.querySelectorAll('button').forEach(button => {
        button.addEventListener('click', async () => {
            if (button.disabled || state.distributorDiagnosticPending) return;
            const command = button.dataset.distributorCommand;
            if (!command) return;
            state.distributorDiagnosticPending = true;
            startStatusPolling();
            updateViewModeControls();
            state.backendControls = {
                ...state.backendControls,
                start: false, exit: false, jog_hold: false,
                distributor_diagnostic: false, camera_diagnostic: false, vision_rule_diagnostic: false,
            };
            applyButtonsForState(state.lineState, state.serverExitRequested, state.backendControls);
            if (els.jogPanel) els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(b => { b.disabled = true; });
            disableSelectedAnalysisButton();
            updateDistributorDiagnosticControls({diagnostic_allowed: false, diagnostic_busy: true});
            try {
                clearControlError();
                await apiPost(`/api/distributor/diagnostic/${command}`, true);
            } finally {
                state.distributorDiagnosticPending = false;
                startStatusPolling();
                updateViewModeControls();
                requestImmediateStatus();
            }
        });
    });
}

function updateSelectedAnalysisStatus(ls) {
    if (!els.analyzeSelectedFrame) return;
    const selected = ls.selected_analysis || {};
    const wasActive = state.selectedAnalysisActive;
    const wasLiveStreaming = state.liveStreaming;
    state.selectedAnalysisActive = selected.active === true;
    state.selectedAnalysisRole = selected.role || null;
    if (wasActive !== state.selectedAnalysisActive && typeof updateThresholdsPanel === 'function') {
        updateThresholdsPanel();
    }

    const selectedActive = state.selectedAnalysisActive && JOG_ALLOWED_STATES.includes(state.lineState);
    if (els.statsSummary) els.statsSummary.classList.toggle('is-collapsed', selectedActive);
    if (els.distributorDiagnostics) els.distributorDiagnostics.classList.toggle('is-collapsed', selectedActive);

    const live = ls.live || {};
    state.liveFps = Number(live.fps || (ls.jog || {}).live_fps || 0);
    const staticRoles = Array.isArray(live.static_roles) ? live.static_roles : [];
    const process = ls.process || {};
    const inspectionRoles = Array.isArray(process.inspection_roles) ? process.inspection_roles : [];
    const phase = String(process.phase || '').toUpperCase();
    const inspectionDisplay = inspectionRoles.includes(state.currentCamera)
        && isInspectionDisplayPhase(phase);
    const selectedRoleStatic = live.all_roles_static === true
        || staticRoles.includes(state.currentCamera)
        || inspectionDisplay
        // Совместимость со статусом backend до ролевых пауз.
        || (live.static === true && live.streaming === false && staticRoles.length === 0);
    // Источник главной камеры зависит от её собственной роли, а не от
    // inspection другой группы камер.
    state.liveStreaming = live.running === true && !selectedRoleStatic;

    if (wasLiveStreaming !== state.liveStreaming && typeof applyMainCameraSource === 'function') {
        applyMainCameraSource();
    }

    const controls = ls.controls || {};
    const allowed = state.selectedAnalysisActive ? controls.selected_model_release === true : controls.selected_model_analysis === true;
    const lineState = state.lineState;
    const showAnalysis = (lineState === 'IDLE' || lineState === 'STOPPED') || state.selectedAnalysisActive;
    els.analyzeSelectedFrame.classList.toggle('is-hidden', !showAnalysis);
    els.analyzeSelectedFrame.disabled = !allowed || state.selectedAnalysisPending || state.offline || !state.currentCamera;
    els.analyzeSelectedFrame.textContent = state.selectedAnalysisActive ? 'ВЕРНУТЬ ПОТОК' : 'АНАЛИЗ КАДРА';
    els.analyzeSelectedFrame.classList.toggle('analysis-active', state.selectedAnalysisActive);

    if (typeof applyLiveBadge === 'function') applyLiveBadge(state.jogActive);
    updateViewModeControls();

    if (state.selectedAnalysisActive) {
        showSelectedAnalysisFrame(state.selectedAnalysisRole);
    } else if (wasActive) {
        returnSelectedCameraToLive();
    }
}

// ——— выбранный кадр: pending UI ———

function showPendingSelectedFrameAnalysis() {
    // Показываем панель анализа кадра, скрываем остальные блоки правой колонки
    const panel = els.frameAnalysisPanel || document.getElementById('frame-analysis-panel');
    if (!panel) return;
    panel.classList.remove('is-collapsed');
    if (els.statsSummary) els.statsSummary.classList.add('is-collapsed');
    if (els.distributorDiagnostics) els.distributorDiagnostics.classList.add('is-collapsed');

    // Плейсхолдер в новом анализе — через основной рендер
    const tbody = document.getElementById('fa-new-tbody');
    if (tbody) {
        tbody.replaceChildren();
        const empty = document.createElement('div');
        empty.className = 'fa-new-empty';
        empty.textContent = 'Подготовка анализа кадра…';
        tbody.appendChild(empty);
    }
    const verdictEl = document.getElementById('fa-new-verdict');
    if (verdictEl) {
        verdictEl.className = 'fa-new-verdict warn';
        setIfChanged(verdictEl, 'АНАЛИЗ');
    }
    const ctxEl = document.getElementById('fa-new-context');
    if (ctxEl) setIfChanged(ctxEl, 'Ожидание моделей');
}

// ——— выбранный кадр: кнопки ———

function setupSelectedFrameAnalysis() {
    if (!els.analyzeSelectedFrame) return;
    els.analyzeSelectedFrame.addEventListener('click', async () => {
        if (els.analyzeSelectedFrame.disabled || state.selectedAnalysisPending) return;
        state.selectedAnalysisPending = true;
        // Скрываем редактирование порогов сразу: анализируемый стоп-кадр
        // должен оставаться привязанным к уже применённым значениям.
        if (typeof updateThresholdsPanel === 'function') updateThresholdsPanel();
        if (!state.selectedAnalysisActive) showPendingSelectedFrameAnalysis();
        updateViewModeControls();
        els.analyzeSelectedFrame.disabled = true;
        state.backendControls = {
            ...state.backendControls,
            start: false, jog_hold: false, distributor_diagnostic: false,
            camera_diagnostic: false, vision_rule_diagnostic: false, selected_model_analysis: false,
        };
        applyButtonsForState(state.lineState, state.serverExitRequested, state.backendControls);
        if (els.distributorDiagnostics) els.distributorDiagnostics.querySelectorAll('button').forEach(b => { b.disabled = true; });
        if (els.jogPanel) {
            els.jogPanel.classList.add('is-collapsed');
            els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(b => { b.disabled = true; });
        }
        clearControlError();
        try {
            if (state.selectedAnalysisActive) {
                await apiPost('/api/diagnostics/selected/release', true);
            } else {
                const role = state.currentCamera;
                if (!role) return;
                await apiPost(`/api/diagnostics/selected/${encodeURIComponent(role)}`, true);
            }
        } finally {
            state.selectedAnalysisPending = false;
            updateViewModeControls();
            requestImmediateStatus();
        }
    });
}

// Совместимость с тестами — минимальный экспорт
if (typeof window !== 'undefined') {
    window.updateSelectedAnalysisStatus = updateSelectedAnalysisStatus;
    window.showPendingSelectedFrameAnalysis = showPendingSelectedFrameAnalysis;
}
