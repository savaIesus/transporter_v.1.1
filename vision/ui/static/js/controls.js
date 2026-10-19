// controls.js — Line Monitor UI module
'use strict';

// ─── Buttons ─────────────────────────────────────────────────

function applyButtonsForState(lineState, exitRequested, controls = {}) {
    const offline = state.offline || lineState === 'OFFLINE';
    const pending = state.controlPending;
    const startVisible = (
        (lineState === 'IDLE' || lineState === 'STOPPED')
        && !exitRequested
    );
    const stopVisible = (lineState === 'RUNNING' || lineState === 'PAUSED');
    const pauseVisible = lineState === 'RUNNING' && !exitRequested;
    const resumeVisible = lineState === 'PAUSED' && !exitRequested;
    const exitText = (
        lineState === 'FAULT'
        || (exitRequested && lineState === 'STOPPING')
    ) ? 'ПРИНУДИТЕЛЬНЫЙ ВЫХОД' : 'ВЫХОД';

    if (els.btnStart) {
        els.btnStart.classList.toggle('is-hidden', !startVisible);
        els.btnStart.disabled = (
            !startVisible
            || controls.start !== true
            || pending
            || offline
        );
    }

    if (els.btnPause) {
        els.btnPause.classList.toggle('is-hidden', !pauseVisible);
        els.btnPause.disabled = (
            !pauseVisible
            || controls.pause !== true
            || pending
            || offline
        );
    }

    if (els.btnResume) {
        els.btnResume.classList.toggle('is-hidden', !resumeVisible);
        els.btnResume.disabled = (
            !resumeVisible
            || controls.resume !== true
            || pending
            || offline
        );
    }

    if (els.btnStop) {
        els.btnStop.classList.toggle('is-hidden', !stopVisible);
        els.btnStop.disabled = (
            !stopVisible
            || controls.stop !== true
            || pending
            || offline
        );
    }

    if (els.btnExit) {
        els.btnExit.classList.remove('is-hidden');
        els.btnExit.textContent = exitText;
        els.btnExit.className = 'btn btn-exit';
        els.btnExit.disabled = (
            controls.exit !== true
            || pending
            || offline
        );
    }
}

function flashButton(btn) {
    if (!btn) return;
    btn.classList.add('btn-flash');
    setTimeout(() => btn.classList.remove('btn-flash'), 250);
}

async function submitControl(path) {
    if (state.controlPending || state.offline) return null;
    const isStart = path === '/api/start';
    let result = null;
    state.controlPending = true;
    if (isStart) {
        state.startPending = true;
        updateStateOverlay({state: state.lineState, in_line: 0});
    }
    startStatusPolling();
    updateViewModeControls();
    applyButtonsForState(
        state.lineState,
        state.serverExitRequested,
        state.backendControls,
    );
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.querySelectorAll('button').forEach(
            button => { button.disabled = true; }
        );
    }
    disableSelectedAnalysisButton();
    if (els.jogPanel) {
        if (path === '/api/start') {
            els.jogPanel.classList.add('is-collapsed');
        }
        els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(
            button => { button.disabled = true; }
        );
    }
    try {
        clearControlError();
        result = await apiPost(path, true);
        return result;
    } finally {
        state.controlPending = false;
        if (isStart && !result) {
            state.startPending = false;
        }
        startStatusPolling();
        updateViewModeControls();
        requestImmediateStatus();
    }
}

function setupButtons() {
    if (els.splashExit) {
        els.splashExit.addEventListener('click', async () => {
            els.splashExit.disabled = true;
            clearControlError();
            const result = await apiPost('/api/exit', true);
            if (!result) els.splashExit.disabled = false;
        });
    }

    els.btnStart.addEventListener('click', async () => {
        if (els.btnStart.classList.contains('is-hidden')
            || els.btnStart.disabled) return;
        flashButton(els.btnStart);
        await submitControl('/api/start');
    });

    if (els.btnPause) {
        els.btnPause.addEventListener('click', async () => {
            if (els.btnPause.classList.contains('is-hidden')
                || els.btnPause.disabled) return;
            flashButton(els.btnPause);
            await submitControl('/api/pause');
        });
    }

    if (els.btnResume) {
        els.btnResume.addEventListener('click', async () => {
            if (els.btnResume.classList.contains('is-hidden')
                || els.btnResume.disabled) return;
            flashButton(els.btnResume);
            await submitControl('/api/resume');
        });
    }

    els.btnStop.addEventListener('click', async () => {
        if (els.btnStop.classList.contains('is-hidden')
            || els.btnStop.disabled) return;
        flashButton(els.btnStop);
        await submitControl('/api/stop');
    });

    els.btnExit.addEventListener('click', async () => {
        if (els.btnExit.classList.contains('is-hidden')
            || els.btnExit.disabled) return;
        flashButton(els.btnExit);
        await submitControl('/api/exit');
    });

}

// ─── State overlay ───────────────────────────────────────────

function updateStateOverlay(ls) {
    const lineState     = ls.state || 'IDLE';
    const exitRequested = ls.exit_requested;

    let mainCode = '';
    let mainText = '';
    let subText  = '';
    let peekable = false;

    if (lineState === 'OFFLINE' || state.offline) {
        mainCode = 'OFFLINE';
        mainText = 'НЕТ СВЯЗИ';
        subText = 'Нет связи с системой управления. Команды заблокированы.';
        peekable = false;
    } else if (state.startPending) {
        mainCode = 'STARTING';
        mainText = 'ПОДГОТОВКА К ПУСКУ';
        subText = 'Распределитель устанавливается в рабочее положение';
        peekable = false;
    } else if (lineState === 'FAULT') {
        mainCode = 'FAULT';
        mainText = 'АВАРИЯ';
        const remaining = ls.in_line || 0;
        const reason = ls.fault_reason || 'Произошла ошибка';
        subText = remaining > 0
            ? `${reason}. Корпусов на линии: ${remaining}.`
            : reason;
        peekable = false;
    } else if (exitRequested && lineState === 'STOPPING') {
        mainCode = 'DRAINING';
        mainText = 'ОСТАНОВКА ЛИНИИ';
        const remaining = ls.in_line || 0;
        subText = remaining > 0
            ? `На линии осталось корпусов: ${remaining}`
            : 'Завершение работы...';
        peekable = true;
    } else if (state.selectedAnalysisActive) {
        // Оператор запросил стоп-кадр выбранной камеры: не закрываем его
        // большим состоянием IDLE/STOPPED поверх изображения.
        mainText = '';
    } else if (lineState === 'PAUSED') {
        if (state.jogActive) {
            mainText = '';
        } else {
            mainCode = 'PAUSED';
            mainText = 'ПАУЗА В ЦИКЛЕ';
            subText = 'Доступна ручная коррекция ленты. Нажмите ПРОДОЛЖИТЬ';
            peekable = true;
        }
    } else if (lineState === 'STOPPED') {
        if (state.jogActive) {
            mainText = '';
        } else {
            mainCode = 'STOPPED';
            mainText = 'ЛИНИЯ ОСТАНОВЛЕНА';
            subText  = 'Нажмите ПУСК для продолжения';
            peekable = false;
        }
    } else if (lineState === 'STOPPING') {
        mainCode = 'STOPPING';
        mainText = 'ОСТАНОВКА ЛИНИИ';
        subText  = `На линии осталось корпусов: ${ls.in_line || 0}`;
        peekable = true;
    } else if (lineState === 'IDLE') {
        if (state.jogActive) {
            mainText = '';
        } else {
            mainCode = 'IDLE';
            mainText = 'ГОТОВА К ПУСКУ';
            subText  = 'Нажмите ПУСК для начала работы';
            peekable = false;
        }
    }

    if (mainText) {
        els.cameraOverlay.dataset.mainText = mainCode;
        els.cameraOverlay.dataset.peekable = peekable ? '1' : '0';

        if (els.cameraOverlay.dataset.renderedMain !== mainText
            || els.cameraOverlay.dataset.renderedSub !== subText) {
            els.cameraOverlay.innerHTML = `
                <div class="camera-overlay-main"></div>
                <div class="camera-overlay-sub"></div>
            `;
            els.cameraOverlay.dataset.renderedMain = mainText;
            els.cameraOverlay.dataset.renderedSub  = subText;
        }

        const mainEl = els.cameraOverlay.querySelector('.camera-overlay-main');
        const subEl  = els.cameraOverlay.querySelector('.camera-overlay-sub');
        if (mainEl) setIfChanged(mainEl, mainText);
        if (subEl)  setIfChanged(subEl, subText);

        els.cameraOverlay.classList.remove('is-hidden');

        if (peekable && state.cameraHovered) {
            els.cameraOverlay.classList.add('overlay-peek');
        } else {
            els.cameraOverlay.classList.remove('overlay-peek');
        }
    } else {
        els.cameraOverlay.classList.add('is-hidden');
        els.cameraOverlay.dataset.mainText    = '';
        els.cameraOverlay.dataset.peekable    = '0';
        els.cameraOverlay.dataset.renderedMain = '';
        els.cameraOverlay.dataset.renderedSub  = '';
    }
}

