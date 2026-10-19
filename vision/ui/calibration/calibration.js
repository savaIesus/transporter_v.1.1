(() => {
    'use strict';

    const elements = {
        stateBox: document.getElementById('state-box'),
        stateLabel: document.getElementById('state-label'),
        foundCount: document.getElementById('found-count'),
        currentRole: document.getElementById('current-role'),
        stepCounter: document.getElementById('step-counter'),
        preview: document.getElementById('camera-preview'),
        placeholder: document.getElementById('preview-placeholder'),
        placeholderTitle: document.getElementById('placeholder-title'),
        placeholderDetail: document.getElementById('placeholder-detail'),
        cameraBadge: document.getElementById('camera-id-badge'),
        previewError: document.getElementById('preview-error'),
        candidateControls: document.getElementById('candidate-controls'),
        candidatePosition: document.getElementById('candidate-position'),
        previousCamera: document.getElementById('previous-camera'),
        nextCamera: document.getElementById('next-camera'),
        assignedCount: document.getElementById('assigned-count'),
        roleList: document.getElementById('role-list'),
        readyActions: document.getElementById('ready-actions'),
        reviewActions: document.getElementById('review-actions'),
        errorActions: document.getElementById('error-actions'),
        savedActions: document.getElementById('saved-actions'),
        assignCamera: document.getElementById('assign-camera'),
        backStep: document.getElementById('back-step'),
        reviewBack: document.getElementById('review-back'),
        saveMapping: document.getElementById('save-mapping'),
        fatalError: document.getElementById('fatal-error'),
        closeError: document.getElementById('close-error'),
        rescan: document.getElementById('rescan'),
        configPath: document.getElementById('config-path'),
        cancel: document.getElementById('cancel-calibration'),
    };

    const statusLabels = {
        WAITING: 'ПОДГОТОВКА',
        SCANNING: 'ПОИСК КАМЕР',
        READY: 'НАЗНАЧЕНИЕ',
        REVIEW: 'ПРОВЕРКА',
        SAVED: 'СОХРАНЕНО',
        ERROR: 'ОШИБКА',
        CANCELLED: 'ОТМЕНЕНО',
    };

    let state = null;
    let actionBusy = false;
    let previewKey = null;
    let previewReadyKey = null;
    let previewGeneration = 0;
    let finishScheduled = false;

    function api() {
        return window.pywebview.api;
    }

    function setHidden(element, hidden) {
        element.classList.toggle('is-hidden', hidden);
    }

    function setPreviewPlaceholder(title, detail) {
        elements.preview.classList.remove('is-visible');
        elements.placeholder.classList.remove('is-hidden');
        elements.placeholderTitle.textContent = title;
        elements.placeholderDetail.textContent = detail || '';
        setHidden(elements.cameraBadge, true);
    }

    function renderRoles(rows) {
        elements.roleList.replaceChildren();
        for (let index = 0; index < rows.length; index += 1) {
            const row = rows[index];
            const item = document.createElement('div');
            item.className = `role-row ${row.status || ''}`.trim();

            const number = document.createElement('span');
            number.className = 'role-index';
            number.textContent = String(index + 1).padStart(2, '0');

            const name = document.createElement('div');
            name.className = 'role-name';
            const label = document.createElement('strong');
            label.textContent = row.label || row.role;
            const code = document.createElement('span');
            code.textContent = row.role;
            name.append(label, code);

            const camera = document.createElement('span');
            camera.className = 'role-camera';
            camera.textContent = row.camera_id === null || row.camera_id === undefined
                ? (row.status === 'current' ? 'ВЫБЕРИТЕ' : '—')
                : `CAM ${row.camera_id}`;

            item.append(number, name, camera);
            elements.roleList.appendChild(item);
        }
    }

    function render(nextState) {
        state = nextState;
        const status = String(state.status || 'WAITING').toUpperCase();
        elements.stateBox.className = `calibration-state state-${status.toLowerCase()}`;
        elements.stateLabel.textContent = statusLabels[status] || status;
        elements.foundCount.textContent = `${state.found ?? 0} / ${state.required ?? 3}`;
        elements.assignedCount.textContent = `${Object.keys(state.assignments || {}).length} / ${state.total_steps || 3}`;
        elements.configPath.textContent = state.config_path || 'camera_mapping.json';
        renderRoles(Array.isArray(state.roles) ? state.roles : []);

        setHidden(elements.readyActions, status !== 'READY');
        setHidden(elements.reviewActions, status !== 'REVIEW');
        setHidden(elements.errorActions, status !== 'ERROR');
        setHidden(elements.savedActions, status !== 'SAVED');
        setHidden(elements.candidateControls, status !== 'READY');
        elements.cancel.classList.toggle('is-hidden', status === 'SAVED');

        if (status === 'SCANNING' || status === 'WAITING') {
            elements.currentRole.textContent = 'ПОИСК КАМЕР';
            elements.stepCounter.textContent = '—';
            setPreviewPlaceholder(
                'ПОИСК ПОДКЛЮЧЁННЫХ КАМЕР',
                'Проверка Camera ID и production-разрешения 1280×720',
            );
            previewKey = null;
            previewGeneration += 1;
        } else if (status === 'READY') {
            elements.currentRole.textContent = state.current_role_label || state.current_role;
            elements.stepCounter.textContent = `ШАГ ${state.step} / ${state.total_steps}`;
            elements.candidatePosition.textContent = `${state.candidate_position} / ${state.candidate_count}`;
            elements.cameraBadge.textContent = `CAMERA ID ${state.current_camera_id}`;
            elements.backStep.disabled = Number(state.step || 1) <= 1;
            const key = `${state.current_role}:${state.current_camera_id}`;
            if (previewKey !== key) {
                previewKey = key;
                previewReadyKey = null;
                previewGeneration += 1;
                elements.previewError.classList.add('is-hidden');
                setPreviewPlaceholder(
                    `ОТКРЫТИЕ CAMERA ID ${state.current_camera_id}`,
                    'Ожидание валидного кадра',
                );
                void pollPreview(previewGeneration);
            }
            elements.assignCamera.disabled = (
                actionBusy || previewReadyKey !== key
            );
        } else if (status === 'REVIEW') {
            elements.currentRole.textContent = 'ПРОВЕРКА НАЗНАЧЕНИЙ';
            elements.stepCounter.textContent = '3 / 3';
            setPreviewPlaceholder(
                'ВСЕ РОЛИ НАЗНАЧЕНЫ',
                'Проверьте таблицу справа и сохраните конфигурацию',
            );
            previewKey = null;
            previewGeneration += 1;
        } else if (status === 'ERROR') {
            elements.currentRole.textContent = 'КАЛИБРОВКА ЗАБЛОКИРОВАНА';
            elements.stepCounter.textContent = 'ОШИБКА';
            elements.fatalError.textContent = state.error || 'Неизвестная ошибка калибровки';
            setPreviewPlaceholder(
                'НЕДОСТАТОЧНО ИСПРАВНЫХ КАМЕР',
                state.error || 'Основное приложение не будет запущено',
            );
            previewKey = null;
            previewGeneration += 1;
        } else if (status === 'SAVED') {
            elements.currentRole.textContent = 'КОНФИГУРАЦИЯ ГОТОВА';
            elements.stepCounter.textContent = '7 / 7';
            setPreviewPlaceholder(
                'CAMERA_MAPPING.JSON СОХРАНЁН',
                'Переход к основному приложению',
            );
            previewKey = null;
            previewGeneration += 1;
            if (!finishScheduled) {
                finishScheduled = true;
                window.setTimeout(() => {
                    void api().finish();
                }, 850);
            }
        }
    }

    async function pollPreview(generation) {
        if (!state || state.status !== 'READY' || generation !== previewGeneration) return;
        try {
            const result = await api().get_frame();
            if (generation !== previewGeneration) return;
            if (result && result.ok && result.data) {
                elements.preview.src = result.data;
                elements.preview.classList.add('is-visible');
                elements.placeholder.classList.add('is-hidden');
                elements.cameraBadge.textContent = `CAMERA ID ${result.camera_id}`;
                elements.cameraBadge.classList.remove('is-hidden');
                elements.previewError.classList.add('is-hidden');
                previewReadyKey = previewKey;
                elements.assignCamera.disabled = actionBusy;
            } else {
                elements.previewError.textContent = (result && result.error) || 'Нет кадра';
                elements.previewError.classList.remove('is-hidden');
            }
        } catch (error) {
            if (generation === previewGeneration) {
                elements.previewError.textContent = String(error);
                elements.previewError.classList.remove('is-hidden');
            }
        }
        if (generation === previewGeneration && state && state.status === 'READY') {
            window.setTimeout(() => {
                void pollPreview(generation);
            }, 90);
        }
    }

    async function refreshState() {
        try {
            const nextState = await api().get_state();
            render(nextState);
        } catch (error) {
            elements.fatalError.textContent = String(error);
            elements.errorActions.classList.remove('is-hidden');
        }
        window.setTimeout(refreshState, 350);
    }

    async function runAction(callback) {
        if (actionBusy) return;
        actionBusy = true;
        document.querySelectorAll('button').forEach(button => {
            button.disabled = true;
        });
        try {
            const nextState = await callback();
            if (nextState && typeof nextState === 'object') render(nextState);
        } catch (error) {
            elements.previewError.textContent = String(error);
            elements.previewError.classList.remove('is-hidden');
        } finally {
            actionBusy = false;
            document.querySelectorAll('button').forEach(button => {
                button.disabled = false;
            });
            if (state) render(state);
        }
    }

    elements.previousCamera.addEventListener('click', () => {
        void runAction(() => api().previous_camera());
    });
    elements.nextCamera.addEventListener('click', () => {
        void runAction(() => api().next_camera());
    });
    elements.assignCamera.addEventListener('click', () => {
        void runAction(() => api().assign_current());
    });
    elements.backStep.addEventListener('click', () => {
        void runAction(() => api().back());
    });
    elements.reviewBack.addEventListener('click', () => {
        void runAction(() => api().back());
    });
    elements.saveMapping.addEventListener('click', () => {
        void runAction(() => api().save());
    });
    elements.rescan.addEventListener('click', () => {
        void runAction(() => api().rescan());
    });
    elements.closeError.addEventListener('click', () => {
        void api().cancel();
    });
    elements.cancel.addEventListener('click', () => {
        void api().cancel();
    });
    window.addEventListener('keydown', event => {
        if (event.key === 'Escape' && (!state || state.status !== 'SAVED')) {
            event.preventDefault();
            void api().cancel();
        }
    });

    window.addEventListener('pywebviewready', () => {
        void refreshState();
    });
})();
