// frame-analysis.js — переработанный анализ кадра
// - без лишних обводок и кликабельности
// - с кастомным ползунком как в порогах правил
// - защита от гонок: версионирование рендеров, стабильная прокрутка,
//   идемпотентные обновления DOM
'use strict';

const FA_RULE_NAMES = {
    part_presence: 'Наличие корпуса',
    uneven_heights: 'Разновысотность окон',
    window_sinks: 'Раковины окон',
    bottom_glass: 'Стекло на дне',
    welding: 'Брак сварки',
};

// ——— состояние ———
let _faLastKey = null;
let _faLastScrollContext = null;
let _faRenderSeq = 0;
let _faScrollBinding = null;
let _faDragState = null;
let _faRafSync = 0;
let _faResizeObserver = null;

// ——— утилиты ———
function faNewRuleBadge(rule) {
    if (!rule) return {className: 'ok', text: '—'};
    if (rule.part_absent) return {className: 'warn', text: 'ПУСТО'};
    if (rule.name === 'part_presence') return {className: 'ok', text: 'КОРПУС'};
    if (rule.triggered) return {className: 'bad', text: 'СРАБОТАЛО'};
    if (rule.skipped) return {className: 'warn', text: 'НЕТ ИЗМЕРЕНИЯ'};
    return {className: 'ok', text: 'НОРМА'};
}

function faNewFormatValue(v) {
    if (v == null || v === '') return '—';
    return String(v);
}

function faNewFormatLimit(metric) {
    if (!metric) return '—';
    if (metric.limit != null && metric.limit !== '') return String(metric.limit);
    if (typeof metric.limit_raw === 'number' && Number.isFinite(metric.limit_raw)) return String(metric.limit_raw);
    return '—';
}

function faNewCollectGroups(cards) {
    const generalMap = new Map();
    const objectsMap = new Map();
    const list = Array.isArray(cards) ? cards : [];
    for (const card of list) {
        const role = card.role || '';
        const metrics = Array.isArray(card.metrics) ? card.metrics : [];
        for (const m of metrics) {
            const key = m.key || m.label;
            if (!key) continue;
            const row = {
                label: m.label || m.key || '—',
                limit: faNewFormatLimit(m),
                value: faNewFormatValue(m.value != null ? m.value : null),
                ok: m.ok == null ? null : !!m.ok,
                value_raw: typeof m.value_raw === 'number' ? m.value_raw : null,
            };
            const objectName = m.object || null;
            if (!objectName) {
                if (generalMap.has(key)) continue;
                generalMap.set(key, row);
            } else {
                const groupKey = role + '::' + objectName;
                let group = objectsMap.get(groupKey);
                if (!group) {
                    group = {name: objectName, rowsMap: new Map()};
                    objectsMap.set(groupKey, group);
                }
                if (group.rowsMap.has(key)) continue;
                group.rowsMap.set(key, row);
            }
        }
    }
    return {
        general: [...generalMap.values()],
        objects: [...objectsMap.values()].map(g => ({
            name: g.name,
            rows: [...g.rowsMap.values()],
        })),
    };
}

function faNewObjectStatus(rows) {
    let hasBad = false, hasOk = false, measured = false;
    for (const row of rows) {
        if (row.ok == null) continue;
        measured = true;
        if (row.ok) hasOk = true; else hasBad = true;
    }
    if (hasBad) return {cls: 'bad', text: 'ОТКЛОНЕНИЕ'};
    if (measured && hasOk) return {cls: 'ok', text: 'В НОРМЕ'};
    return {cls: 'muted', text: '—'};
}

function faNewStripObjectPrefix(label) {
    return String(label).replace(
        /^(?:Окно|Контакт|Раковина|Стекло|Shell|Glass)\s*#\d+(?:\s*\([^)]*\))?(?:\s*→\s*(?:контакт\s*)?#\d+)?\s*:\s*/i,
        '',
    );
}

function faNewReportKey(report) {
    try {
        return JSON.stringify({
            kind: report.kind,
            role: report.role,
            stage: report.stage,
            part_id: report.part_id,
            updated_at: report.updated_at,
            rules: report.rules,
        });
    } catch (_) {
        return [report.kind, report.role, report.stage, report.part_id, report.updated_at].join('|');
    }
}

function faNewVerdict(report, ls) {
    const rules = Array.isArray(report.rules) ? report.rules : [];
    const absent = rules.find(r => r && r.part_absent === true);
    if (absent) return {cls: 'warn', text: 'КОРПУС НЕ ОБНАРУЖЕН'};
    const triggered = rules.filter(r => r && r.triggered === true);
    let category = null;
    if (ls && report.part_id != null) {
        const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
        const part = parts.find(p => Number(p.id) === Number(report.part_id));
        if (part) category = String(part.category || '').toUpperCase();
    }
    if (category === 'CLEANUP') return {cls: 'warn', text: 'ЗАЧИСТКА'};
    if (triggered.length) {
        const names = triggered.map(r => FA_RULE_NAMES[r.name] || r.name).join(', ');
        return {cls: 'bad', text: 'БРАК: ' + names};
    }
    if (category === 'BAD') return {cls: 'bad', text: 'БРАК'};
    if (rules.some(r => r && r.skipped === true)) return {cls: 'warn', text: 'ЕСТЬ ПРОПУЩЕННЫЕ ПРАВИЛА'};
    if (category === 'GOOD') return {cls: 'ok', text: 'ГОДНОЕ'};
    return {cls: 'ok', text: 'ГОДНО'};
}

// ——— ползунок ———
function faGetScrollEls() {
    // Fallback для тестового харнесса: если нового контейнера нет, используем старое тело
    const scroll = document.getElementById('fa-new-scroll') || document.getElementById('fa-new-body');
    return {
        scroll: scroll,
        track: document.getElementById('fa-new-scroll-track'),
        thumb: document.getElementById('fa-new-scroll-thumb'),
        body: document.getElementById('fa-new-body'),
        tbody: document.getElementById('fa-new-tbody'),
        panel: document.getElementById('frame-analysis-panel'),
    };
}

function faScrollMetrics(scroll, track, thumb) {
    const maxScroll = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
    const trackH = Math.max(0, track.clientHeight || 0);
    const ratio = scroll.clientHeight / Math.max(1, scroll.scrollHeight);
    const thumbH = maxScroll > 0
        ? Math.max(22, Math.min(Math.round(trackH * 0.55), Math.round(trackH * ratio)))
        : 0;
    return {
        maxScroll,
        trackH,
        thumbH,
        maxThumbTop: Math.max(0, trackH - thumbH),
    };
}

function faUpdateScrollbarAria(track, scroll, maxScroll) {
    const percent = maxScroll > 0
        ? Math.round((scroll.scrollTop / maxScroll) * 100)
        : 0;
    track.setAttribute('aria-valuenow', String(Math.max(0, Math.min(100, percent))));
    track.setAttribute('aria-disabled', maxScroll > 0 ? 'false' : 'true');
}

function faSyncScroll() {
    const {scroll, track, thumb} = faGetScrollEls();
    if (!scroll || !track || !thumb) return;
    const metrics = faScrollMetrics(scroll, track, thumb);
    if (metrics.maxScroll <= 0) {
        track.classList.add('is-idle');
        thumb.style.top = '0px';
        if (scroll.scrollTop !== 0) scroll.scrollTop = 0;
        faUpdateScrollbarAria(track, scroll, 0);
        return;
    }
    track.classList.remove('is-idle');

    // После display:none дорожка получает размеры только на следующем layout.
    // Пересчитываем метрики уже для видимой дорожки, иначе первый drag мог
    // получить maxThumbTop=0 и визуально «не работать».
    const visibleMetrics = faScrollMetrics(scroll, track, thumb);
    const top = visibleMetrics.maxScroll > 0 && visibleMetrics.maxThumbTop > 0
        ? (scroll.scrollTop / visibleMetrics.maxScroll) * visibleMetrics.maxThumbTop
        : 0;
    thumb.style.height = visibleMetrics.thumbH + 'px';
    thumb.style.top = top + 'px';
    faUpdateScrollbarAria(track, scroll, visibleMetrics.maxScroll);
}

function faOnScroll() {
    if (_faRafSync) cancelAnimationFrame(_faRafSync);
    _faRafSync = requestAnimationFrame(() => {
        _faRafSync = 0;
        faSyncScroll();
    });
}

function faClamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function faSetScrollFromThumbTop(scroll, track, thumb, desiredTop) {
    const metrics = faScrollMetrics(scroll, track, thumb);
    if (metrics.maxScroll <= 0 || metrics.maxThumbTop <= 0) return;
    const top = faClamp(desiredTop, 0, metrics.maxThumbTop);
    // scrollTop — единственный источник истины. Событие scroll затем точно
    // синхронизирует бегунок, в том числе после изменения высоты панели.
    scroll.scrollTop = (top / metrics.maxThumbTop) * metrics.maxScroll;
    faSyncScroll();
}

function faEndDrag(event) {
    if (!_faDragState) return;
    if (
        event
        && event.pointerId != null
        && _faDragState.pointerId != null
        && event.pointerId !== _faDragState.pointerId
    ) return;
    const {track, thumb, pointerId, moveEvent, upEvent, cancelEvent, onMove, onEnd} = _faDragState;
    track.classList.remove('is-dragging');
    if (pointerId != null && typeof thumb.releasePointerCapture === 'function') {
        try {
            if (!thumb.hasPointerCapture || thumb.hasPointerCapture(pointerId)) {
                thumb.releasePointerCapture(pointerId);
            }
        } catch (_) {}
    }
    document.removeEventListener(moveEvent, onMove);
    document.removeEventListener(upEvent, onEnd);
    if (cancelEvent) document.removeEventListener(cancelEvent, onEnd);
    _faDragState = null;
    faSyncScroll();
}

function faStartThumbDrag(event, scroll, track, thumb, pointerEvents) {
    if (track.classList.contains('is-idle')) return;
    if (pointerEvents && event.isPrimary === false) return;
    if (event.button != null && event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();

    // Предыдущий drag мог быть прерван потерей фокуса WebView.
    faEndDrag();
    faSyncScroll();
    const metrics = faScrollMetrics(scroll, track, thumb);
    if (metrics.maxScroll <= 0 || metrics.maxThumbTop <= 0) return;

    const startTop = (scroll.scrollTop / metrics.maxScroll) * metrics.maxThumbTop;
    const moveEvent = pointerEvents ? 'pointermove' : 'mousemove';
    const upEvent = pointerEvents ? 'pointerup' : 'mouseup';
    const cancelEvent = pointerEvents ? 'pointercancel' : null;
    const pointerId = pointerEvents ? event.pointerId : null;
    const startY = event.clientY;

    const onMove = moveEventPayload => {
        if (!_faDragState) return;
        if (
            pointerEvents
            && moveEventPayload.pointerId != null
            && moveEventPayload.pointerId !== pointerId
        ) return;
        moveEventPayload.preventDefault();
        faSetScrollFromThumbTop(
            scroll,
            track,
            thumb,
            startTop + moveEventPayload.clientY - startY,
        );
    };
    const onEnd = endEvent => faEndDrag(endEvent);

    _faDragState = {
        scroll, track, thumb, pointerId,
        moveEvent, upEvent, cancelEvent, onMove, onEnd,
    };
    track.classList.add('is-dragging');
    document.addEventListener(moveEvent, onMove, {passive: false});
    document.addEventListener(upEvent, onEnd);
    if (cancelEvent) document.addEventListener(cancelEvent, onEnd);

    if (pointerId != null && typeof thumb.setPointerCapture === 'function') {
        try { thumb.setPointerCapture(pointerId); } catch (_) {}
    }
}

function faInitScrollHandlers() {
    const {scroll, track, thumb, tbody, panel} = faGetScrollEls();
    if (!scroll || !track || !thumb) return;
    if (
        _faScrollBinding
        && _faScrollBinding.scroll === scroll
        && _faScrollBinding.track === track
        && _faScrollBinding.thumb === thumb
    ) return;

    // В обычном UI узлы постоянные. Проверка identity выше также позволяет
    // безопасно привязаться заново, если WebView восстановил часть DOM.
    _faScrollBinding = {scroll, track, thumb};
    const pointerEvents = typeof window !== 'undefined' && 'PointerEvent' in window;
    const downEvent = pointerEvents ? 'pointerdown' : 'mousedown';

    scroll.addEventListener('scroll', faOnScroll, {passive: true});

    // Клик/касание по дорожке — быстрый переход к нужному месту.
    track.addEventListener(downEvent, event => {
        if (event.target === thumb) return;
        if (track.classList.contains('is-idle')) return;
        if (pointerEvents && event.isPrimary === false) return;
        if (event.button != null && event.button !== 0) return;
        event.preventDefault();
        const metrics = faScrollMetrics(scroll, track, thumb);
        if (metrics.maxScroll <= 0 || metrics.maxThumbTop <= 0) return;
        const rect = track.getBoundingClientRect();
        faSetScrollFromThumbTop(
            scroll,
            track,
            thumb,
            event.clientY - rect.top - metrics.thumbH / 2,
        );
        try { track.focus({preventScroll: true}); } catch (_) { try { track.focus(); } catch (_) {} }
    });

    // Pointer Events дают одинаково надёжный drag мышью, пером и пальцем.
    // Для старого WebView остаётся fallback на mouse events.
    thumb.addEventListener(downEvent, event => {
        faStartThumbDrag(event, scroll, track, thumb, pointerEvents);
    });

    // Колесо на дорожке — прокрутка контента.
    track.addEventListener('wheel', event => {
        if (track.classList.contains('is-idle')) return;
        event.preventDefault();
        const unit = event.deltaMode === 1
            ? 16
            : (event.deltaMode === 2 ? scroll.clientHeight : 1);
        scroll.scrollTop += event.deltaY * unit;
        faSyncScroll();
    }, {passive: false});

    // Клавиатура — стрелками.
    track.addEventListener('keydown', event => {
        if (track.classList.contains('is-idle')) return;
        if (event.key === 'ArrowDown') { event.preventDefault(); scroll.scrollTop += 40; }
        if (event.key === 'ArrowUp') { event.preventDefault(); scroll.scrollTop -= 40; }
        if (event.key === 'PageDown') { event.preventDefault(); scroll.scrollTop += scroll.clientHeight * 0.8; }
        if (event.key === 'PageUp') { event.preventDefault(); scroll.scrollTop -= scroll.clientHeight * 0.8; }
        if (event.key === 'Home') { event.preventDefault(); scroll.scrollTop = 0; }
        if (event.key === 'End') { event.preventDefault(); scroll.scrollTop = scroll.scrollHeight; }
        faSyncScroll();
    });

    window.addEventListener('blur', () => faEndDrag());
    window.addEventListener('resize', faOnScroll);

    // Во время процесса соседние аккордеоны меняют высоту с transition.
    // ResizeObserver синхронизирует дорожку после фактического layout, а не
    // только в первый requestAnimationFrame до завершения анимации.
    if (typeof ResizeObserver !== 'undefined') {
        if (_faResizeObserver) _faResizeObserver.disconnect();
        _faResizeObserver = new ResizeObserver(() => faOnScroll());
        [scroll, tbody, track, panel].filter(Boolean).forEach(element => {
            _faResizeObserver.observe(element);
        });
    }
}

// ——— построение DOM ———
function faNewBuildRow(row) {
    const rowEl = document.createElement('div');
    rowEl.className = 'fa-new-thr-row';

    const label = document.createElement('span');
    label.className = 'fa-new-thr-label';
    label.textContent = row.label;
    label.title = row.label;
    rowEl.appendChild(label);

    const limit = document.createElement('span');
    limit.className = 'fa-new-thr-limit';
    limit.textContent = row.limit;
    limit.title = row.limit;
    rowEl.appendChild(limit);

    const meas = document.createElement('span');
    meas.className = 'fa-new-meas' +
        (row.ok === false ? ' is-bad' : '') +
        (row.ok === true ? ' is-ok' : '');
    meas.textContent = row.value;
    meas.title = row.value;
    // не кликабельно: никакого cursor pointer, никаких обработчиков
    rowEl.appendChild(meas);
    return rowEl;
}

function renderNewFrameAnalysis(report, ls) {
    _faRenderSeq += 1;
    const thisSeq = _faRenderSeq;

    const panel = document.getElementById('frame-analysis-panel');
    // Fallback: в тестовом харнессе нет fa-new-scroll, используем fa-new-body как скролл-контейнер
    let scroll = document.getElementById('fa-new-scroll');
    if (!scroll) scroll = document.getElementById('fa-new-body');
    const tbody = document.getElementById('fa-new-tbody');
    if (!panel || !scroll || !tbody) return;

    faInitScrollHandlers();

    const available = report.available === true;
    if (!available) {
        if (!panel.classList.contains('is-collapsed')) panel.classList.add('is-collapsed');
        // очищаем только если был контент
        if (tbody.children.length) tbody.replaceChildren();
        _faLastKey = null;
        _faLastScrollContext = null;
        requestAnimationFrame(() => faSyncScroll());
        return;
    }
    if (panel.classList.contains('is-collapsed')) panel.classList.remove('is-collapsed');

    const rules = Array.isArray(report.rules) ? report.rules : [];

    // вердикт + контекст — точечно
    const verdictEl = document.getElementById('fa-new-verdict');
    if (verdictEl) {
        const verdict = faNewVerdict(report, ls);
        const cls = 'fa-new-verdict ' + verdict.cls;
        if (verdictEl.className !== cls) verdictEl.className = cls;
        setIfChanged(verdictEl, verdict.text);
    }
    const contextEl = document.getElementById('fa-new-context');
    if (contextEl) {
        const parts = [];
        const stage = String(report.stage || '').toUpperCase();
        if (stage) parts.push(stage);
        if (report.role) parts.push(typeof cameraRoleLabel === 'function' ? cameraRoleLabel(report.role) : report.role);
        if (report.part_id != null) parts.push('КОРПУС \u2116\u00a0' + report.part_id);
        setIfChanged(contextEl, parts.join(' · '));
    }

    const key = faNewReportKey(report);
    if (key === _faLastKey && tbody.children.length) {
        // контент тот же — не трогаем DOM, но синхронизируем скролл на всякий случай
        requestAnimationFrame(() => faSyncScroll());
        return;
    }

    // Сохраняем прокрутку перед перестроением
    const keepScroll = scroll.scrollTop;
    const scrollContext = String(report.stage || '') + '|' + (report.part_id == null ? '' : report.part_id);
    const resetScroll = _faLastScrollContext !== null && _faLastScrollContext !== scrollContext;

    _faLastKey = key;

    if (!rules.length) {
        tbody.replaceChildren();
        const emptyRow = document.createElement('div');
        emptyRow.className = 'fa-new-empty';
        emptyRow.textContent = 'Ожидание результатов анализа…';
        tbody.appendChild(emptyRow);
        if (thisSeq === _faRenderSeq) scroll.scrollTop = 0;
        requestAnimationFrame(() => faSyncScroll());
        _faLastScrollContext = scrollContext;
        return;
    }

    // Собираем фрагмент вне DOM для стабильности; fallback для харнесса
    let frag = null;
    let fragList = null;
    if (typeof document.createDocumentFragment === 'function') {
        frag = document.createDocumentFragment();
    } else {
        fragList = [];
        frag = {
            appendChild: (el) => { fragList.push(el); return el; },
            _isFake: true,
            _list: fragList,
        };
    }
    const sorted = [...rules].sort((a, b) => {
        const order = r => r.part_absent ? 0 : (r.triggered ? 1 : (r.skipped ? 2 : 3));
        return order(a) - order(b);
    });

    for (const rule of sorted) {
        const ruleHead = document.createElement('div');
        ruleHead.className = 'fa-new-rule-head' + (rule.triggered || rule.part_absent ? ' triggered' : '');
        const ruleName = document.createElement('span');
        ruleName.className = 'fa-new-rule-name';
        ruleName.textContent = FA_RULE_NAMES[rule.name] || rule.name;
        ruleName.title = FA_RULE_NAMES[rule.name] || rule.name;
        ruleHead.appendChild(ruleName);

        const badgeInfo = faNewRuleBadge(rule);
        const badge = document.createElement('span');
        badge.className = 'fa-new-rule-badge ' + badgeInfo.className;
        badge.textContent = badgeInfo.text;
        ruleHead.appendChild(badge);
        frag.appendChild(ruleHead);

        if (rule.part_absent) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = 'КОРПУС НЕ ОБНАРУЖЕН — измерения недоступны';
            frag.appendChild(emptyRow);
            continue;
        }

        const groups = faNewCollectGroups(rule.measurement_cards);
        if (!groups.general.length && !groups.objects.length) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = rule.skipped ? 'Нет измерений' : 'Нет данных порогов';
            frag.appendChild(emptyRow);
            continue;
        }

        for (const row of groups.general) {
            frag.appendChild(faNewBuildRow(row));
        }

        for (const object of groups.objects) {
            const block = document.createElement('div');
            block.className = 'fa-new-obj-block';

            const head = document.createElement('div');
            head.className = 'fa-new-obj-head';
            const name = document.createElement('span');
            name.className = 'fa-new-obj-name';
            name.textContent = object.name;
            name.title = object.name;
            head.appendChild(name);

            const status = faNewObjectStatus(object.rows);
            const statusBadge = document.createElement('span');
            statusBadge.className = 'fa-new-obj-badge ' + status.cls;
            statusBadge.textContent = status.text;
            head.appendChild(statusBadge);
            block.appendChild(head);

            for (const row of object.rows) {
                const rowEl = faNewBuildRow(row);
                const labelEl = rowEl.children[0];
                if (labelEl) labelEl.textContent = faNewStripObjectPrefix(row.label);
                block.appendChild(rowEl);
            }
            frag.appendChild(block);
        }
    }

    // Проверяем, что рендер всё ещё актуален (защита от гонки быстрых статусов)
    if (thisSeq !== _faRenderSeq) return;

    if (frag && frag._isFake) {
        tbody.replaceChildren();
        frag._list.forEach(el => { try { tbody.appendChild(el); } catch (_) { tbody.children.push(el); } });
    } else {
        tbody.replaceChildren(frag);
    }

    // Восстанавливаем прокрутку
    if (resetScroll) scroll.scrollTop = 0;
    else scroll.scrollTop = keepScroll;

    _faLastScrollContext = scrollContext;
    requestAnimationFrame(() => faSyncScroll());
}

function updateNewFrameAnalysisStatus(ls) {
    const report = ls.frame_analysis || {};
    renderNewFrameAnalysis(report, ls);
}

if (typeof window !== 'undefined') {
    window.renderNewFrameAnalysis = renderNewFrameAnalysis;
    window.updateNewFrameAnalysisStatus = updateNewFrameAnalysisStatus;
    window.FA_RULE_NAMES = FA_RULE_NAMES;
    window.faSyncScroll = faSyncScroll;
}
