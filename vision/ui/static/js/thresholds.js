// thresholds.js — стабилизированная панель порогов
// - исправлены гонки при быстром переключении камер и при drag ползунка
'use strict';

const THRESHOLD_EDITABLE_STATES = ['IDLE', 'STOPPED'];

let thresholdsData = null;
let thresholdsBusy = false;
let thresholdsSaveBusy = false;
let thresholdsBodyKey = null;
let thresholdsDirty = false;
let thresholdsCardIndex = 0;

// Глобальные для drag — переживают перерисовку, легко отменяются
let _threshDragState = null;
let _threshFetchSeq = 0;

function thresholdsPanelVisible() {
    // Пороги настраиваются только в JOG. Во время анализа выбранного
    // стоп-кадра они уже применены к показанному результату, поэтому
    // панель скрыта, чтобы исключить изменение задним числом.
    const jogMode = !!(state.jogActive || state.jogTogglePending);
    return (
        !state.splashActive
        && !state.offline
        && !state.serverExitRequested
        && jogMode
        && !state.selectedAnalysisActive
        && !state.selectedAnalysisPending
        && THRESHOLD_EDITABLE_STATES.includes(state.lineState)
        && !!state.currentCamera
    );
}

function thresholdsEditableNow() {
    return (
        thresholdsPanelVisible()
        && !state.controlPending
        && !state.startPending
        && !state.jogBusy
        && !state.distributorDiagnosticPending
        && !state.distributorDiagnosticBackendBusy
        && !state.selectedAnalysisPending
    );
}

function setThresholdsStatus(message, kind) {
    if (!els.thresholdsStatus) return;
    setIfChanged(els.thresholdsStatus, message || '');
    els.thresholdsStatus.classList.toggle('is-error', kind === 'error');
}

async function fetchThresholds(role) {
    if (!role) return;
    if (thresholdsBusy) return;
    const mySeq = ++_threshFetchSeq;
    thresholdsBusy = true;
    const data = await apiGet(`/api/thresholds?role=${encodeURIComponent(role)}`);
    thresholdsBusy = false;
    if (mySeq !== _threshFetchSeq) return; // устаревший ответ
    if (!data) return;
    if (data.role !== state.currentCamera) {
        // Роль сменилась пока грузили — грузим актуальную
        fetchThresholds(state.currentCamera);
        return;
    }
    thresholdsData = data;
    renderThresholdsPanel();
}

function updateThresholdsPanel(force) {
    if (!thresholdsPanelVisible()) {
        if (els.thresholdsPanel) els.thresholdsPanel.classList.add('is-hidden');
        return;
    }
    if (els.thresholdsPanel) els.thresholdsPanel.classList.remove('is-hidden');
    thresholdsSyncScroll();

    if (!thresholdsData || thresholdsData.role !== state.currentCamera) {
        thresholdsDirty = false;
        thresholdsData = null;
        thresholdsBodyKey = null;
        setThresholdsStatus('', '');
        fetchThresholds(state.currentCamera);
        return;
    }
    if (force && !thresholdsDirty && !thresholdsBusy) {
        fetchThresholds(state.currentCamera);
        return;
    }
    renderThresholdsPanel();
}

function renderThresholdsPanel() {
    if (!thresholdsData) return;
    const editable = thresholdsEditableNow() && thresholdsData.editable !== false;
    if (els.thresholdsPanel) els.thresholdsPanel.classList.toggle('is-locked', !editable);
    const key = `${thresholdsData.role}|${thresholdsData.revision}`;
    if (key !== thresholdsBodyKey) {
        // Если был активный drag — прерываем, чтобы не держать ссылку на удалённый DOM
        if (_threshDragState) {
            try { _threshDragState.track.classList.remove('is-dragging'); } catch (_) {}
            _threshDragState = null;
            document.removeEventListener('mousemove', _onThreshMouseMove);
            document.removeEventListener('mouseup', _onThreshMouseUp);
        }
        thresholdsBodyKey = key;
        renderThresholdsBody();
    }
    setThresholdInputsEditable(editable);
    updateThresholdsActions();
}

function buildThresholdItem(param) {
    const item = document.createElement('div');
    item.className = 'thresholds-item';
    const span = document.createElement('span');
    span.className = 'thresholds-item-label';
    span.textContent = param.label || param.key;
    const description = String(param.description || '').trim();
    // Подсказка всегда начинается с полного читаемого названия, чтобы
    // усечённую подпись можно было прочитать целиком; технический ключ —
    // только дополнение для тех, кому он нужен.
    const fullName = param.label || param.key;
    const tooltip = [
        fullName,
        description,
        param.key && param.key !== fullName ? `Технический ключ: ${param.key}` : '',
    ].filter(Boolean).join('\n\n');
    span.title = tooltip;
    span.setAttribute('aria-label', `${param.label || param.key}. ${tooltip}`);
    item.appendChild(span);
    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'thresholds-input';
    input.dataset.key = param.key;
    input.step = param.step || 'any';
    if (typeof param.min === 'number') input.min = param.min;
    if (typeof param.max === 'number') input.max = param.max;
    input.dataset.readonly = param.readonly === true ? 'true' : 'false';
    if (param.readonly === true) input.disabled = true;
    input.title = tooltip;
    input.setAttribute('aria-label', param.label || param.key);
    input.value = param.value;
    item.appendChild(input);
    return item;
}

function setThresholdInputsEditable(editable) {
    if (!els.thresholdsBody) return;
    els.thresholdsBody.querySelectorAll('input.thresholds-input').forEach(input => {
        input.disabled = !editable || input.dataset.readonly === 'true';
    });
}

function _clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function _onThreshMouseMove(e) {
    if (!_threshDragState) return;
    const {rows, track, thumb, startY, startTop, maxScroll, maxThumbTop} = _threshDragState;
    const dy = e.clientY - startY;
    const nt = _clamp(startTop + dy, 0, maxThumbTop);
    thumb.style.top = nt + 'px';
    if (maxThumbTop > 0 && maxScroll > 0) rows.scrollTop = (nt / maxThumbTop) * maxScroll;
}
function _onThreshMouseUp() {
    if (!_threshDragState) return;
    try { _threshDragState.track.classList.remove('is-dragging'); } catch (_) {}
    _threshDragState = null;
    document.removeEventListener('mousemove', _onThreshMouseMove);
    document.removeEventListener('mouseup', _onThreshMouseUp);
}

function renderThresholdsBody() {
    const body = els.thresholdsBody;
    if (!body) return;
    body.innerHTML = '';
    const rules = (thresholdsData && thresholdsData.rules) || [];
    if (!rules.length) {
        const empty = document.createElement('div');
        empty.className = 'thresholds-empty';
        empty.textContent = 'Пороги для выбранной камеры не найдены';
        body.appendChild(empty);
        return;
    }
    thresholdsCardIndex = 0;
    const scroll = document.createElement('div');
    scroll.className = 'thresholds-scroll';
    const tabs = document.createElement('div');
    tabs.className = 'thresholds-tabs';
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Правила камеры');
    rules.forEach((group, index) => {
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'thresholds-tab';
        tab.dataset.rule = group.rule || '';
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', 'false');
        tab.title = group.label || group.rule;
        const tabLabel = document.createElement('span');
        tabLabel.className = 'thresholds-tab-label';
        tabLabel.textContent = group.label || group.rule;
        tab.appendChild(tabLabel);
        tab.addEventListener('click', () => {
            if (thresholdsCardIndex === index) { thresholdsSyncScroll(); return; }
            thresholdsCardIndex = index;
            updateCardVisibility();
        });
        tabs.appendChild(tab);
    });
    const THRESHOLDS_TAB_PAD = 16;
    const hoverCapable = !window.matchMedia || window.matchMedia('(hover: hover)').matches;
    tabs.addEventListener('pointerover', event => {
        if (!hoverCapable) return;
        const tab = event.target.closest('.thresholds-tab');
        if (!tab) return;
        const label = tab.querySelector('.thresholds-tab-label');
        const textWidth = label ? label.scrollWidth : 0;
        tab.classList.add('is-title-expanded');
        tab.style.flexBasis = (textWidth + THRESHOLDS_TAB_PAD) + 'px';
    });
    tabs.addEventListener('pointerout', event => {
        if (!hoverCapable) return;
        const tab = event.target.closest('.thresholds-tab');
        if (!tab) return;
        const next = event.relatedTarget;
        if (next && tab.contains(next)) return;
        // Активная вкладка остаётся раскрытой после ухода курсора.
        if (tab.classList.contains('is-active')) return;
        tab.classList.remove('is-title-expanded');
        tab.style.flexBasis = '';
    });
    scroll.appendChild(tabs);

    const cards = document.createElement('div');
    cards.className = 'thresholds-cards';
    rules.forEach((group, index) => {
        const card = document.createElement('section');
        card.className = 'thresholds-card';
        card.dataset.rule = group.rule || '';
        card.dataset.index = String(index);
        // Полное название правила видно в открытой карточке, а не только
        // в усечённой вкладке сверху.
        const cardTitle = document.createElement('div');
        cardTitle.className = 'thresholds-card-title';
        cardTitle.textContent = group.label || group.rule;
        card.appendChild(cardTitle);
        const cardBody = document.createElement('div');
        cardBody.className = 'thresholds-card-body';
        const rows = document.createElement('div');
        rows.className = 'thresholds-rows';
        for (const param of group.params || []) rows.appendChild(buildThresholdItem(param));
        cardBody.appendChild(rows);
        const track = document.createElement('div');
        track.className = 'thresholds-scroll-track thresholds-card-scroll-track';
        track.setAttribute('aria-label', 'Прокрутка карточки правил');
        track.title = 'Прокрутка карточки правил';
        track.tabIndex = 0;
        const thumb = document.createElement('div');
        thumb.className = 'thresholds-scroll-thumb';
        track.appendChild(thumb);
        cardBody.appendChild(track);
        card.appendChild(cardBody);
        cards.appendChild(card);
    });
    scroll.append(cards);
    body.appendChild(scroll);

    const syncTabHints = () => {
        [...tabs.querySelectorAll('.thresholds-tab')].forEach((tab, index) => {
            tab.title = rules[index].label || rules[index].rule;
        });
    };

    const updateCardVisibility = () => {
        const total = rules.length;
        thresholdsCardIndex = Math.max(0, Math.min(total - 1, thresholdsCardIndex));
        [...cards.querySelectorAll('.thresholds-card')].forEach((card, index) => {
            const isActive = index === thresholdsCardIndex;
            card.classList.toggle('is-expanded', isActive);
            card.classList.toggle('is-active', isActive);
        });
        [...tabs.querySelectorAll('.thresholds-tab')].forEach((tab, index) => {
            const isActive = index === thresholdsCardIndex;
            tab.classList.toggle('is-active', isActive);
            tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
            // Активная вкладка раскрывается до полного названия: открытое
            // правило читается сразу, а не только при наведении.
            if (isActive) {
                const label = tab.querySelector('.thresholds-tab-label');
                tab.classList.add('is-title-expanded');
                tab.style.flexBasis = ((label ? label.scrollWidth : 0) + THRESHOLDS_TAB_PAD) + 'px';
            } else {
                tab.classList.remove('is-title-expanded');
                tab.style.flexBasis = '';
            }
        });
        syncTabHints();
        thresholdsSyncScroll();
        thresholdsScrollActiveTabIntoView();
        requestAnimationFrame(() => { thresholdsSyncScroll(); thresholdsScrollActiveTabIntoView(); });
    };

    cards.querySelectorAll('.thresholds-card').forEach(card => {
        const rows = card.querySelector('.thresholds-rows');
        const track = card.querySelector('.thresholds-scroll-track');
        const thumb = track ? track.querySelector('.thresholds-scroll-thumb') : null;
        if (!rows || !track || !thumb) return;
        rows.addEventListener('scroll', () => thresholdsSyncCard(rows, track, thumb));
        track.addEventListener('mousedown', (e) => {
            if (e.target === thumb) return;
            if (track.classList.contains('is-idle')) return;
            const rect = track.getBoundingClientRect();
            const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
            if (maxScroll <= 0) return;
            const trackH = track.clientHeight;
            const thumbH = thumb.offsetHeight || 22;
            const maxThumbTop = Math.max(0, trackH - thumbH);
            const clickY = e.clientY - rect.top;
            const desiredTop = _clamp(clickY - thumbH / 2, 0, maxThumbTop);
            thumb.style.top = desiredTop + 'px';
            rows.scrollTop = maxThumbTop > 0 ? (desiredTop / maxThumbTop) * maxScroll : 0;
        });
        thumb.addEventListener('mousedown', (e) => {
            if (track.classList.contains('is-idle')) return;
            e.preventDefault(); e.stopPropagation();
            const trackH = track.clientHeight;
            const thumbH = thumb.offsetHeight || 22;
            const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
            const maxThumbTop = Math.max(0, trackH - thumbH);
            const currentTop = parseFloat(thumb.style.top) || 0;
            // если был предыдущий drag — отменяем
            if (_threshDragState) {
                try { _threshDragState.track.classList.remove('is-dragging'); } catch (_) {}
                document.removeEventListener('mousemove', _onThreshMouseMove);
                document.removeEventListener('mouseup', _onThreshMouseUp);
            }
            _threshDragState = {rows, track, thumb, startY: e.clientY, startTop: currentTop, maxScroll, maxThumbTop};
            track.classList.add('is-dragging');
            document.addEventListener('mousemove', _onThreshMouseMove);
            document.addEventListener('mouseup', _onThreshMouseUp);
        });
        track.addEventListener('wheel', (e) => {
            if (track.classList.contains('is-idle')) return;
            e.preventDefault();
            rows.scrollTop += e.deltaY;
        }, {passive: false});
    });

    updateCardVisibility();
}

function thresholdsSyncCard(rows, track, thumb) {
    if (!rows || !track || !thumb) return;
    if (!thumb) thumb = track.querySelector('.thresholds-scroll-thumb');
    if (!thumb) return;
    const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
    if (maxScroll <= 0) {
        track.classList.add('is-idle');
        thumb.style.top = '0px';
        if (rows.scrollTop) rows.scrollTop = 0;
        return;
    }
    track.classList.remove('is-idle');
    const trackH = track.clientHeight || 56;
    const ratio = rows.clientHeight / Math.max(1, rows.scrollHeight);
    const thumbH = Math.max(22, Math.min(Math.round(trackH * 0.6), Math.round(trackH * ratio)));
    const maxThumbTop = Math.max(0, trackH - thumbH);
    const top = maxScroll > 0 ? (rows.scrollTop / maxScroll) * maxThumbTop : 0;
    thumb.style.height = thumbH + 'px';
    thumb.style.top = top + 'px';
}

function thresholdsSyncScroll() {
    const body = els.thresholdsBody;
    if (!body) return;
    const card = body.querySelector('.thresholds-card.is-active');
    if (!card) return;
    const rows = card.querySelector('.thresholds-rows');
    const track = card.querySelector('.thresholds-scroll-track');
    const thumb = track ? track.querySelector('.thresholds-scroll-thumb') : null;
    thresholdsSyncCard(rows, track, thumb);
}

function thresholdsScrollActiveTabIntoView() {
    const body = els.thresholdsBody;
    if (!body) return;
    const tabs = body.querySelector('.thresholds-tabs');
    const activeTab = tabs && tabs.querySelector('.thresholds-tab.is-active');
    if (!tabs || !activeTab) return;
    const left = activeTab.offsetLeft;
    const right = left + activeTab.offsetWidth;
    if (left < tabs.scrollLeft) tabs.scrollLeft = left;
    else if (right > tabs.scrollLeft + tabs.clientWidth) tabs.scrollLeft = right - tabs.clientWidth;
}

function collectThresholdValues() {
    const values = {};
    if (els.thresholdsBody) {
        els.thresholdsBody.querySelectorAll('input.thresholds-input').forEach(input => {
            const raw = String(input.value).trim();
            if (raw === '') return;
            const number = Number(raw);
            if (Number.isNaN(number)) return;
            values[input.dataset.key] = number;
        });
    }
    return values;
}

function hasChangedThresholds() {
    if (!thresholdsData || !thresholdsData.values) return false;
    if (els.thresholdsBody) {
        const hasEmpty = [...els.thresholdsBody.querySelectorAll('input.thresholds-input')].some(input => String(input.value).trim() === '');
        if (hasEmpty) return true;
    }
    const current = collectThresholdValues();
    return Object.entries(current).some(([key, value]) => thresholdsData.values[key] !== value);
}

function updateThresholdsActions() {
    if (!els.thresholdsSave || !els.thresholdsReset) return;
    const editable = thresholdsEditableNow() && !!thresholdsData && thresholdsData.editable !== false;
    const changed = thresholdsDirty || hasChangedThresholds();
    els.thresholdsReset.disabled = !editable || thresholdsSaveBusy;
    els.thresholdsSave.disabled = !editable || thresholdsSaveBusy || !changed;
}

async function saveThresholds() {
    if (!thresholdsEditableNow() || thresholdsSaveBusy || !thresholdsData) return;
    if (els.thresholdsBody) {
        const hasEmpty = [...els.thresholdsBody.querySelectorAll('input.thresholds-input')].some(input => String(input.value).trim() === '');
        if (hasEmpty) { setThresholdsStatus('Заполните все поля', 'error'); return; }
    }
    const values = collectThresholdValues();
    if (!Object.keys(values).length) return;
    thresholdsSaveBusy = true;
    updateThresholdsActions();
    setThresholdsStatus('Сохранение...', '');
    try {
        const result = await apiPostJson('/api/thresholds', {role: state.currentCamera, values}, true);
        if (!result || !result.thresholds) { setThresholdsStatus('Не удалось сохранить пороги', 'error'); return; }
        thresholdsData = result.thresholds;
        thresholdsDirty = false;
        if (typeof result.thresholds.revision === 'number') state.thresholdsRevision = result.thresholds.revision;
        setThresholdsStatus('Сохранено', '');
        renderThresholdsPanel();
    } finally {
        thresholdsSaveBusy = false;
        updateThresholdsActions();
    }
}

async function resetThresholds() {
    if (!thresholdsEditableNow() || thresholdsSaveBusy) return;
    setThresholdsStatus('', '');
    thresholdsDirty = false;
    thresholdsBodyKey = null;
    thresholdsData = null;
    await fetchThresholds(state.currentCamera);
}

function setupThresholdsControls() {
    if (els.thresholdsSave) els.thresholdsSave.addEventListener('click', saveThresholds);
    if (els.thresholdsReset) els.thresholdsReset.addEventListener('click', resetThresholds);
    if (els.thresholdsBody) {
        const markThresholdsChanged = event => {
            if (!event.target.matches('input.thresholds-input')) return;
            thresholdsDirty = true;
            if (!hasChangedThresholds()) thresholdsDirty = false;
            updateThresholdsActions();
        };
        els.thresholdsBody.addEventListener('input', markThresholdsChanged);
        els.thresholdsBody.addEventListener('change', markThresholdsChanged);
    }
    window.addEventListener('resize', () => { thresholdsSyncScroll(); thresholdsScrollActiveTabIntoView(); });
    updateThresholdsPanel();
}
