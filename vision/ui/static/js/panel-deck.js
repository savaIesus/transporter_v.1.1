// panel-deck.js — варианты правой панели с переворотом (rotateY)
// 'use strict';
//
// Правая панель — «колода лиц»: ready / production / fault. Лицо
// выбирается автоматически по состоянию линии. Смена лица — переворот
// вокруг вертикальной оси:
//   0° → 90°  (уходим на ребро), меняем data-face,
//   -90° → 0° (въезжаем с «задней» стороны с новым содержимым).
// Задняя сторона не показывается: на ребре панель невидима.

const PANEL_FACE_ORDER = ['ready', 'production', 'fault'];

const PANEL_FACE_BY_STATE = {
    IDLE: 'ready',
    STOPPED: 'ready',
    RUNNING: 'production',
    PAUSED: 'production',
    STOPPING: 'production',
    FAULT: 'fault',
};

const PANEL_FLIP_HALF_MS = 280;
const PANEL_FLIP_TOTAL_MS = PANEL_FLIP_HALF_MS * 2;
const PANEL_FLIP_EASE = 'cubic-bezier(0.36, 0.07, 0.19, 0.97)';

let _panelFace = 'ready';
let _panelFlipping = false;
let _panelQueuedFace = null;
let _lastLineStatus = null;

function panelFaceForState(lineState) {
    return PANEL_FACE_BY_STATE[String(lineState || '').toUpperCase()] || 'ready';
}

function applyPanelFace(face) {
    if (!els.statsPanel) return;
    els.statsPanel.dataset.face = face;
}

// Ручное управление (JOG) заблокировано только тогда, когда линия реально
// едет или доезжает: RUNNING / STOPPING. На паузе JOG разрешён — это и есть
// назначение паузы (коррекция ленты), поэтому шторка поднята.
function panelJogLocked(ls) {
    const s = String((ls && ls.state) || '').toUpperCase();
    return s === 'RUNNING' || s === 'STOPPING';
}

function applyJogLock() {
    if (!els.statsPanel) return;
    const locked = panelJogLocked(_lastLineStatus);
    els.statsPanel.classList.toggle('jog-locked', locked);
}

// Карточка «АВАРИЯ» заполняется всегда (дёшево): при входе в лицо fault
// текст причины и число корпусов уже актуальны.
function fillFaultFace(ls) {
    const reason = document.getElementById('fault-reason');
    const inline = document.getElementById('fault-inline');
    if (reason) setIfChanged(reason, (ls && ls.fault_reason) || 'Неизвестная причина');
    if (inline) setIfChanged(inline, (ls && ls.in_line) || 0);
}

function flipPanelTo(face) {
    if (!els.statsPanel) return;
    if (face === _panelFace) {
        // Лицо не меняется (например, ПАУЗА ↔ РАБОТА): переворота нет,
        // но доступность ручного управления могла измениться — применяем
        // сразу, чтобы шторка поднялась/опустилась синхронно.
        applyJogLock();
        return;
    }

    // Переворот уже идёт: запоминаем последний запрошенный вариант и
    // доворачиваем до него после текущего. Быстрая смена фаз не ломает
    // анимацию и не оставляет панель «на ребре».
    if (_panelFlipping) {
        _panelQueuedFace = face;
        return;
    }

    const panel = els.statsPanel;
    const fromIndex = PANEL_FACE_ORDER.indexOf(_panelFace);
    const toIndex = PANEL_FACE_ORDER.indexOf(face);
    const dir = toIndex >= fromIndex ? 1 : -1;

    _panelFlipping = true;
    _panelFace = face;
    _panelQueuedFace = null;
    panel.style.setProperty('--panel-flip-duration', `${PANEL_FLIP_TOTAL_MS}ms`);
    panel.classList.add('panel-flipping');

    // Шторка уже получила целевое состояние из updatePanelFace(). Её ход
    // длится столько же, сколько весь переворот, поэтому обе механики
    // начинаются и заканчиваются вместе без отдельного скачка на ребре.
    // Первая половина: к ребру.
    panel.style.transition = `transform ${PANEL_FLIP_HALF_MS}ms ${PANEL_FLIP_EASE}`;
    panel.style.transform = `perspective(1400px) rotateY(${dir * 90}deg)`;

    setTimeout(() => {
        // На ребре меняем только лицо. Шторка уже движется синхронно с
        // переворотом и не получает повторного переключения в середине.
        applyPanelFace(face);
        panel.style.transition = 'none';
        panel.style.transform = `perspective(1400px) rotateY(${dir * -90}deg)`;
        // Перечитываем раскладку, чтобы сброс transition применился
        // до старта второй половины.
        void panel.offsetWidth;
        panel.style.transition = `transform ${PANEL_FLIP_HALF_MS}ms ${PANEL_FLIP_EASE}`;
        panel.style.transform = 'perspective(1400px) rotateY(0deg)';
    }, PANEL_FLIP_HALF_MS);

    setTimeout(() => {
        panel.style.transition = '';
        panel.style.transform = '';
        panel.classList.remove('panel-flipping');
        panel.style.removeProperty('--panel-flip-duration');
        _panelFlipping = false;

        const queued = _panelQueuedFace;
        _panelQueuedFace = null;
        if (queued && queued !== _panelFace) flipPanelTo(queued);
    }, PANEL_FLIP_TOTAL_MS + 40);
}

// Вызывается из status.js.updateLineStatus после обновления состояния.
function updatePanelFace(ls) {
    _lastLineStatus = ls || null;
    fillFaultFace(ls);
    // Целевое положение шторки задаём до запуска переворота. Если лицо
    // меняется, браузер запускает оба transform в одном кадре; если лицо то
    // же (RUNNING ↔ PAUSED), работает только обычная короткая анимация шторки.
    applyJogLock();
    flipPanelTo(panelFaceForState(ls && ls.state));
}

if (typeof window !== 'undefined') {
    window.updatePanelFace = updatePanelFace;
    window.flipPanelTo = flipPanelTo;
    window.panelFaceForState = panelFaceForState;
}
