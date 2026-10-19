(() => {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const els = {
        connection: $('connection-state'), setup: $('setup-panel'), work: $('work-panel'),
        result: $('result-panel'), datasetName: $('dataset-name'), classes: $('defect-classes'),
        roles: $('camera-roles'), create: $('create-session'), setupMessage: $('setup-message'),
        sessionTitle: $('session-title'), sessionStatus: $('session-status'), partsCount: $('parts-count'),
        framesCount: $('frames-count'), partNumber: $('part-number'), partProgress: $('part-progress'),
        newPart: $('new-part'), finishPart: $('finish-part'), stepConveyor: $('step-conveyor'),
        export: $('export-dataset'),
        finishSession: $('finish-session'), workflowMessage: $('workflow-message'), cameraTitle: $('camera-title'),
        frameId: $('frame-id'), wrap: $('canvas-wrap'), emptyState: $('empty-state'), image: $('frame-image'),
        canvas: $('annotation-canvas'), frameRole: $('frame-role'), frameSize: $('frame-size'),
        annotationHelp: $('annotation-help'), palette: $('element-palette'),
        newElement: $('new-element'), addElement: $('add-element'),
        classSelect: $('defect-class'), finishPolygon: $('finish-polygon'),
        noDefect: $('no-defect'), clearBox: $('clear-box'), save: $('save-annotation'),
        nextCamera: $('next-camera'), annotationMessage: $('annotation-message'),
        resultText: $('result-text'), resultYaml: $('result-yaml'),
    };

    let config = null;
    let state = null;
    let currentFrame = null;
    let annotations = [];
    let draftPoints = [];
    let draftClass = '';
    let hoverPoint = null;
    let noDefectSelected = false;
    let lastError = '';

    function setMessage(element, text, kind = '') {
        if (!element) return;
        element.textContent = text || '';
        element.className = element.className.replace(/\b(error|ok)\b/g, '').trim();
        if (kind) element.classList.add(kind);
    }

    async function api(path, options = {}) {
        const response = await fetch(path, {
            ...options,
            headers: {'Content-Type': 'application/json', ...(options.headers || {})},
        });
        let payload = null;
        try { payload = await response.json(); } catch (_) { payload = {}; }
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        return payload;
    }

    function selectedValues(container) {
        return [...container.querySelectorAll('input[type="checkbox"]:checked')]
            .map((item) => item.value);
    }

    function fillSetup(data) {
        config = data;
        els.classes.value = data.defect_classes.join(', ');
        els.roles.replaceChildren();
        data.camera_roles.forEach((role) => {
            const label = document.createElement('label');
            label.className = 'check-item';
            label.innerHTML = `<input type="checkbox" value="${escapeHtml(role)}" checked><span>${escapeHtml(role)}</span>`;
            els.roles.appendChild(label);
        });
    }

    function fillClasses(names) {
        const selected = els.classSelect.value;
        els.classSelect.replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'Выберите элемент…';
        els.classSelect.appendChild(placeholder);
        (names || []).forEach((name) => {
            const option = document.createElement('option');
            option.value = name;
            option.textContent = name;
            els.classSelect.appendChild(option);
        });
        if ([...(names || [])].includes(selected)) els.classSelect.value = selected;
        renderPalette(names || []);
    }

    function renderPalette(names) {
        if (!els.palette) return;
        els.palette.replaceChildren();
        (names || []).forEach((name) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'palette-item';
            button.dataset.element = name;
            button.textContent = name;
            button.disabled = !annotationEditable();
            button.addEventListener('click', () => selectElement(name));
            els.palette.appendChild(button);
        });
        updatePaletteSelection();
    }

    function annotationEditable() {
        return Boolean(currentFrame) && !els.save.disabled;
    }

    function selectElement(name) {
        if (draftPoints.length && name !== draftClass) {
            setMessage(els.annotationMessage, 'Сначала завершите текущую область.', 'error');
            return;
        }
        els.classSelect.value = name || '';
        noDefectSelected = false;
        els.noDefect.classList.remove('selected');
        updatePaletteSelection();
        redrawAnnotations();
    }

    function updatePaletteSelection() {
        if (!els.palette) return;
        [...els.palette.querySelectorAll('.palette-item')].forEach((button) => {
            button.classList.toggle(
                'active',
                button.dataset.element === els.classSelect.value,
            );
            button.disabled = !annotationEditable();
        });
    }

    function escapeHtml(value) {
        return String(value).replace(/[&<>'"]/g, (char) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
        }[char]));
    }

    function renderState(next) {
        state = next || state;
        if (!state) return;
        const session = state.session || {};
        const part = state.part;
        const expected = (state.camera_roles || []).length;
        const frameIds = part && Array.isArray(part.frame_ids) ? part.frame_ids : [];
        els.sessionTitle.textContent = session.session_id || '—';
        els.partsCount.textContent = session.part_count ?? 0;
        els.framesCount.textContent = session.frame_count ?? 0;
        els.sessionStatus.textContent = state.active ? (part ? 'СБОРКА' : 'ГОТОВА') : 'ЗАВЕРШЕНА';
        els.partNumber.textContent = part ? `#${part.part_id}` : '—';
        if (part) {
            els.partProgress.textContent = `${frameIds.length} из ${expected} камер снято · ${state.next_role ? `следующая: ${state.next_role}` : 'все камеры готовы'}`;
        } else {
            els.partProgress.textContent = state.active ? 'Начните новую деталь' : 'Сессия завершена';
        }
        const busy = Boolean(currentFrame) || Boolean(state.current_frame);
        els.newPart.disabled = !state.active || Boolean(part) || busy;
        els.finishPart.disabled = !state.active || !part || busy || frameIds.length !== expected;
        const conveyorEnabled = !config || config.conveyor_step !== false;
        els.stepConveyor.classList.toggle('hidden', !conveyorEnabled);
        els.stepConveyor.disabled = !state.active || Boolean(part) || busy;
        els.finishSession.disabled = !state.active || Boolean(part) || busy || Number(session.frame_count || 0) === 0;
        els.export.disabled = !state.active || Number(session.frame_count || 0) === 0;
        els.nextCamera.disabled = true;
        if (state.next_role && !currentFrame && part) {
            els.cameraTitle.textContent = `Готова камера ${state.next_role}`;
        }
        if (!currentFrame) {
            els.save.disabled = true;
            els.finishPolygon.disabled = true;
            els.noDefect.disabled = true;
            els.clearBox.disabled = true;
            els.classSelect.disabled = true;
            els.newElement.disabled = !state.active;
            els.addElement.disabled = !state.active;
        }
        updatePaletteSelection();
    }

    function resetFrame() {
        currentFrame = null;
        annotations = [];
        draftPoints = [];
        draftClass = '';
        hoverPoint = null;
        noDefectSelected = false;
        els.wrap.classList.add('empty');
        els.wrap.style.aspectRatio = '16 / 9';
        els.image.removeAttribute('src');
        const ctx = els.canvas.getContext('2d');
        ctx.clearRect(0, 0, els.canvas.width, els.canvas.height);
        els.emptyState.classList.remove('hidden');
        els.frameId.textContent = 'FRAME —';
        els.frameRole.textContent = 'КАМЕРА —';
        els.frameSize.textContent = '— × —';
        els.cameraTitle.textContent = 'Ожидание детали';
        els.annotationHelp.textContent = 'Кликайте точки контура, затем нажмите «Завершить область»';
        els.classSelect.value = '';
        els.classSelect.disabled = true;
        els.finishPolygon.disabled = true;
        els.noDefect.disabled = true;
        els.clearBox.disabled = true;
        els.save.disabled = true;
        els.nextCamera.disabled = true;
        els.newElement.disabled = !(state && state.active);
        els.addElement.disabled = !(state && state.active);
        els.canvas.style.pointerEvents = 'none';
        updatePaletteSelection();
    }

    function showFrame(frame, annotated = false) {
        currentFrame = frame;
        annotations = [];
        draftPoints = [];
        draftClass = '';
        hoverPoint = null;
        noDefectSelected = false;
        els.wrap.classList.remove('empty');
        els.emptyState.classList.add('hidden');
        els.image.onload = () => {
            const width = els.image.naturalWidth || frame.width;
            const height = els.image.naturalHeight || frame.height;
            els.canvas.width = width;
            els.canvas.height = height;
            els.wrap.style.aspectRatio = `${width} / ${height}`;
            redrawAnnotations();
        };
        const url = (annotated && frame.annotated_url) ? frame.annotated_url : frame.original_url;
        els.image.src = `${url}${url.includes('?') ? '&' : '?'}v=${Date.now()}`;
        els.frameId.textContent = `FRAME #${frame.image_id}`;
        els.frameRole.textContent = frame.camera_role || frame.role || 'КАМЕРА —';
        els.frameSize.textContent = `${frame.width} × ${frame.height}`;
        els.cameraTitle.textContent = `${frame.camera_role || frame.role} · разметка`;
        els.annotationHelp.textContent = annotated
            ? 'Разметка сохранена. Можно переходить к следующей камере.'
            : 'Кликайте точки контура, затем нажмите «Завершить область»';
        els.canvas.style.pointerEvents = annotated ? 'none' : 'auto';
        els.classSelect.disabled = annotated;
        els.finishPolygon.disabled = annotated;
        els.noDefect.disabled = annotated;
        els.clearBox.disabled = annotated;
        els.save.disabled = annotated;
        els.nextCamera.disabled = !annotated;
        els.newElement.disabled = annotated || !(state && state.active);
        els.addElement.disabled = annotated || !(state && state.active);
        els.canvas.getContext('2d').clearRect(0, 0, els.canvas.width, els.canvas.height);
        updatePaletteSelection();
    }

    function canvasPoint(event) {
        const rect = els.canvas.getBoundingClientRect();
        return {
            x: Math.max(0, Math.min(els.canvas.width, (event.clientX - rect.left) * els.canvas.width / rect.width)),
            y: Math.max(0, Math.min(els.canvas.height, (event.clientY - rect.top) * els.canvas.height / rect.height)),
        };
    }

    function distance(left, right) {
        return Math.hypot(left.x - right.x, left.y - right.y);
    }

    function drawPolygon(ctx, points, className, index, draft = false) {
        if (!points.length) return;
        const hue = (index * 67) % 360;
        ctx.strokeStyle = `hsl(${hue}, 80%, 62%)`;
        ctx.fillStyle = `hsla(${hue}, 80%, 55%, ${draft ? '.08' : '.14'})`;
        ctx.lineWidth = Math.max(2, els.canvas.width / 600);
        ctx.beginPath();
        ctx.moveTo(points[0].x, points[0].y);
        points.slice(1).forEach((point) => ctx.lineTo(point.x, point.y));
        if (!draft && points.length >= 3) {
            ctx.closePath();
            ctx.fill();
        }
        ctx.stroke();
        points.forEach((point) => {
            ctx.beginPath();
            ctx.fillStyle = `hsl(${hue}, 80%, 70%)`;
            ctx.arc(point.x, point.y, Math.max(4, els.canvas.width / 180), 0, Math.PI * 2);
            ctx.fill();
        });
        if (className) {
            ctx.fillStyle = `hsl(${hue}, 80%, 70%)`;
            ctx.font = `${Math.max(14, els.canvas.width / 75)}px Segoe UI, Arial`;
            ctx.fillText(className, points[0].x + 6, Math.max(18, points[0].y - 8));
        }
    }

    function redrawAnnotations() {
        const ctx = els.canvas.getContext('2d');
        ctx.clearRect(0, 0, els.canvas.width, els.canvas.height);
        annotations.forEach((annotation, index) => {
            drawPolygon(ctx, annotation.points, annotation.class_name, index);
        });
        if (draftPoints.length) {
            const preview = draftPoints.slice();
            if (hoverPoint) preview.push(hoverPoint);
            drawPolygon(ctx, preview, draftClass || els.classSelect.value, annotations.length, true);
        }
        els.finishPolygon.disabled = !annotationEditable() || draftPoints.length < 3;
    }

    function finishCurrentPolygon() {
        if (!draftPoints.length) {
            setMessage(els.annotationMessage, 'Сначала поставьте точки контура.', 'error');
            return false;
        }
        if (draftPoints.length < 3) {
            setMessage(els.annotationMessage, 'Для области нужно минимум три точки.', 'error');
            return false;
        }
        annotations.push({
            class_name: draftClass || els.classSelect.value,
            points: draftPoints.map((point) => ({x: point.x, y: point.y})),
        });
        draftPoints = [];
        draftClass = '';
        hoverPoint = null;
        redrawAnnotations();
        setMessage(els.annotationMessage, 'Область добавлена. Можно выбрать другой элемент.', 'ok');
        return true;
    }

    els.canvas.addEventListener('pointerdown', (event) => {
        if (!annotationEditable() || noDefectSelected) return;
        if (!els.classSelect.value) {
            setMessage(els.annotationMessage, 'Сначала выберите элемент в палитре.', 'error');
            return;
        }
        event.preventDefault();
        const point = canvasPoint(event);
        if (!draftPoints.length) draftClass = els.classSelect.value;
        const closeDistance = Math.max(8, els.canvas.width / 100);
        if (draftPoints.length >= 3 && distance(point, draftPoints[0]) <= closeDistance) {
            finishCurrentPolygon();
            return;
        }
        draftPoints.push(point);
        hoverPoint = null;
        redrawAnnotations();
    });

    els.canvas.addEventListener('pointermove', (event) => {
        if (!draftPoints.length || !annotationEditable()) return;
        hoverPoint = canvasPoint(event);
        redrawAnnotations();
    });

    els.canvas.addEventListener('pointerleave', () => {
        if (!hoverPoint) return;
        hoverPoint = null;
        redrawAnnotations();
    });

    els.canvas.addEventListener('dblclick', (event) => {
        event.preventDefault();
        finishCurrentPolygon();
    });

    els.finishPolygon.addEventListener('click', finishCurrentPolygon);

    els.classSelect.addEventListener('change', () => {
        if (draftPoints.length && els.classSelect.value !== draftClass) {
            els.classSelect.value = draftClass;
            setMessage(els.annotationMessage, 'Сначала завершите текущую область.', 'error');
            return;
        }
        noDefectSelected = false;
        els.noDefect.classList.remove('selected');
        updatePaletteSelection();
        redrawAnnotations();
    });

    els.noDefect.addEventListener('click', () => {
        draftPoints = [];
        draftClass = '';
        hoverPoint = null;
        annotations = [];
        noDefectSelected = true;
        els.classSelect.value = '';
        els.noDefect.classList.add('selected');
        updatePaletteSelection();
        redrawAnnotations();
    });

    els.clearBox.addEventListener('click', () => {
        annotations = [];
        draftPoints = [];
        draftClass = '';
        hoverPoint = null;
        noDefectSelected = false;
        els.classSelect.value = '';
        els.noDefect.classList.remove('selected');
        updatePaletteSelection();
        redrawAnnotations();
    });

    els.addElement.addEventListener('click', async () => {
        const name = els.newElement.value.trim();
        if (!name) {
            setMessage(els.annotationMessage, 'Введите название элемента палитры.', 'error');
            return;
        }
        els.addElement.disabled = true;
        try {
            const result = await api('/api/training/classes', {
                method: 'POST', body: JSON.stringify({class_name: name}),
            });
            fillClasses(result.defect_classes || []);
            selectElement(name);
            els.newElement.value = '';
            setMessage(els.annotationMessage, `Элемент «${name}» добавлен в палитру.`, 'ok');
        } catch (error) {
            setMessage(els.annotationMessage, error.message, 'error');
        } finally {
            els.addElement.disabled = !(state && state.active);
        }
    });

    els.newElement.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') els.addElement.click();
    });

    async function loadCapture() {
        clearMessages();
        try {
            const result = await api('/api/training/capture', {method: 'POST', body: '{}'});
            renderState(result.state);
            showFrame(result.frame, false);
            setMessage(els.annotationMessage, `Кадр #${result.frame.image_id} сохранён. Выберите элемент и поставьте точки контура.`, 'ok');
        } catch (error) {
            setMessage(els.annotationMessage, error.message, 'error');
        }
    }

    els.create.addEventListener('click', async () => {
        const roles = selectedValues(els.roles);
        const classes = els.classes.value.split(',').map((item) => item.trim()).filter(Boolean);
        if (!roles.length) { setMessage(els.setupMessage, 'Выберите хотя бы одну камеру.', 'error'); return; }
        if (!classes.length) { setMessage(els.setupMessage, 'Укажите хотя бы один элемент палитры.', 'error'); return; }
        els.create.disabled = true;
        try {
            const result = await api('/api/training/session', {
                method: 'POST',
                body: JSON.stringify({
                    dataset_name: els.datasetName.value.trim() || 'defects',
                    camera_roles: roles,
                    defect_classes: classes,
                }),
            });
            fillClasses(result.defect_classes || classes);
            renderState(result);
            els.setup.classList.add('hidden');
            els.work.classList.remove('hidden');
            setMessage(els.workflowMessage, 'Набор создан. Нажмите «Новая деталь», затем мастер покажет первую камеру.', 'ok');
        } catch (error) {
            setMessage(els.setupMessage, error.message, 'error');
            els.create.disabled = false;
        }
    });

    els.newPart.addEventListener('click', async () => {
        els.newPart.disabled = true;
        try {
            const result = await api('/api/training/parts', {method: 'POST', body: '{}'});
            renderState(result);
            setMessage(els.workflowMessage, `Деталь #${result.part.part_id} начата. Снимаем ${result.next_role}.`);
            await loadCapture();
        } catch (error) {
            setMessage(els.workflowMessage, error.message, 'error');
            renderState(state);
        }
    });

    els.save.addEventListener('click', async () => {
        if (!currentFrame) return;
        if (draftPoints.length) {
            setMessage(els.annotationMessage, 'Сначала нажмите «Завершить область».', 'error');
            return;
        }
        if (!noDefectSelected && !annotations.length) {
            setMessage(els.annotationMessage, 'Выберите элемент и обведите область. Если дефекта нет — нажмите «Дефекта нет».', 'error');
            return;
        }
        els.save.disabled = true;
        const wasNoDefect = noDefectSelected;
        try {
            const result = await api('/api/training/annotations', {
                method: 'POST',
                body: JSON.stringify({
                    image_id: currentFrame.image_id,
                    annotations: wasNoDefect ? [] : annotations,
                }),
            });
            renderState(result.state);
            showFrame(result.frame, true);
            setMessage(els.annotationMessage, wasNoDefect
                ? 'Кадр сохранён как отрицательный пример.'
                : 'Готово: оригинал и изображение с polygon-областями записаны.', 'ok');
            setMessage(els.workflowMessage, result.state.next_role
                ? `Готово. Следующая камера: ${result.state.next_role}.`
                : 'Все камеры детали готовы. Завершите деталь.', 'ok');
        } catch (error) {
            els.save.disabled = false;
            setMessage(els.annotationMessage, error.message, 'error');
        }
    });

    els.nextCamera.addEventListener('click', async () => {
        resetFrame();
        if (state && state.next_role) await loadCapture();
        else renderState(state);
    });

    els.finishPart.addEventListener('click', async () => {
        els.finishPart.disabled = true;
        setMessage(els.workflowMessage, 'Деталь сохранена. Перемещаем ленту к следующему корпусу…');
        try {
            const result = await api('/api/training/parts/finish', {method: 'POST', body: '{}'});
            resetFrame();
            renderState(result);
            const conveyor = result.conveyor || {};
            const partId = result.finished_part ? result.finished_part.part_id : '—';
            const step = Number.isInteger(conveyor.step) ? ` (STEP=${conveyor.step})` : '';
            if (conveyor.moved) {
                setMessage(
                    els.workflowMessage,
                    `Деталь #${partId} сохранена. Конвейер сделал шаг к следующему корпусу${step}. Можно начинать следующую деталь.`,
                    'ok',
                );
            } else if (conveyor.enabled === false) {
                setMessage(els.workflowMessage, `Деталь #${partId} сохранена. Шаг конвейера отключён.`, 'ok');
            } else {
                setMessage(
                    els.workflowMessage,
                    `Деталь #${partId} сохранена, но лента не сдвинулась: ${conveyor.error || 'неизвестная ошибка'}. Проверьте контроллер и нажмите «СДВИНУТЬ ЛЕНТУ».`,
                    'error',
                );
            }
        } catch (error) {
            setMessage(els.workflowMessage, error.message, 'error');
            renderState(state);
        }
    });

    els.stepConveyor.addEventListener('click', async () => {
        els.stepConveyor.disabled = true;
        setMessage(els.workflowMessage, 'Перемещаем ленту к следующему корпусу…');
        try {
            const result = await api('/api/training/conveyor/step', {method: 'POST', body: '{}'});
            renderState(result);
            const conveyor = result.conveyor || {};
            const step = Number.isInteger(conveyor.step) ? ` (STEP=${conveyor.step})` : '';
            if (conveyor.moved) {
                setMessage(els.workflowMessage, `Лента перемещена к следующему корпусу${step}.`, 'ok');
            } else {
                setMessage(
                    els.workflowMessage,
                    `Лента не сдвинулась: ${conveyor.error || 'неизвестная ошибка'}`,
                    'error',
                );
            }
        } catch (error) {
            setMessage(els.workflowMessage, error.message, 'error');
            renderState(state);
        }
    });

    els.export.addEventListener('click', async () => {
        try {
            const result = await api('/api/training/export', {method: 'POST', body: '{}'});
            setMessage(els.workflowMessage, `YOLO-${result.yolo.task || 'segment'}-экспорт обновлён: train ${result.yolo.train_count}, val ${result.yolo.validation_count}.`, 'ok');
        } catch (error) { setMessage(els.workflowMessage, error.message, 'error'); }
    });

    els.finishSession.addEventListener('click', async () => {
        if (!confirm('Завершить набор и подготовить структуру YOLO segmentation?')) return;
        try {
            const result = await api('/api/training/session/finish', {method: 'POST', body: '{}'});
            els.work.classList.add('hidden');
            els.result.classList.remove('hidden');
            const yolo = result.yolo || {};
            els.resultText.textContent = `Полигоны экспортированы: train (${yolo.train_count || 0}), val (${yolo.validation_count || 0}). Пустые label-файлы сохранены для кадров без дефекта.`;
            els.resultYaml.textContent = yolo.yaml_path || 'dataset.yaml создан в папке партии';
        } catch (error) { setMessage(els.workflowMessage, error.message, 'error'); }
    });

    function clearMessages() {
        setMessage(els.annotationMessage, '');
    }

    async function init() {
        try {
            const data = await api('/api/training/config');
            fillSetup(data);
            const existing = await api('/api/training/state');
            els.connection.textContent = 'СЕРВЕР ГОТОВ';
            els.connection.classList.add('ok');
            resetFrame();
            if (existing && existing.active && existing.session) {
                fillClasses(existing.defect_classes || data.defect_classes);
                renderState(existing);
                els.setup.classList.add('hidden');
                els.work.classList.remove('hidden');
                if (existing.current_frame) {
                    showFrame(existing.current_frame, false);
                    setMessage(els.annotationMessage, 'Сессия восстановлена. Продолжите обводку точками.', 'ok');
                } else {
                    setMessage(els.workflowMessage, 'Сессия восстановлена. Можно продолжить текущую деталь или начать новую.', 'ok');
                }
            }
        } catch (error) {
            els.connection.textContent = 'ОШИБКА СОЕДИНЕНИЯ';
            els.connection.classList.add('error');
            lastError = error.message;
            setMessage(els.setupMessage, error.message, 'error');
        }
    }

    init();
})();
