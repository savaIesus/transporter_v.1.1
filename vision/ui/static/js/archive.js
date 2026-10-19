// archive.js — выбор папки обязательного архива партий
'use strict';

let archiveSettingsData = null;
let archiveSettingsDirty = false;
let archiveSettingsBusy = false;

function archiveSettingsEditable() {
    return (
        !state.splashActive
        && !state.offline
        && ['IDLE', 'STOPPED'].includes(state.lineState)
        && !!archiveSettingsData
        && archiveSettingsData.available === true
        && archiveSettingsData.editable === true
    );
}

function archiveSettingsVisible() {
    // Сервисные настройки хранения показываются только на остановленной
    // линии: в IDLE/STOPPED. При работе, паузе, остановке линии и аварии
    // группа «АРХИВ» скрывается, чтобы не занимать место и не отвлекать.
    return (
        !state.splashActive
        && !state.offline
        && ['IDLE', 'STOPPED'].includes(state.lineState)
    );
}

function updateArchiveButton() {
    const button = els.archiveSettingsOpen;
    if (!button) return;
    const visible = archiveSettingsVisible();
    const group = els.archiveSettingsGroup;
    if (group) group.classList.toggle('is-hidden', !visible);
    button.disabled = !visible || !archiveSettingsEditable() || archiveSettingsBusy;
}

function setArchiveSettingsStatus(text, kind = '') {
    if (!els.archiveSettingsStatus) return;
    setIfChanged(els.archiveSettingsStatus, text || '');
    els.archiveSettingsStatus.classList.toggle('is-error', kind === 'error');
}

function renderArchiveSettings(data, preserveForm = false) {
    if (!data || data.available !== true) {
        updateArchiveButton();
        return;
    }
    archiveSettingsData = data;
    updateArchiveButton();

    if (!preserveForm) {
        if (els.archiveRootPath) els.archiveRootPath.value = data.root_path || '';
        archiveSettingsDirty = false;
    }

    const validation = data.validation || {};
    if (els.archiveSettingsValidation) {
        if (validation.writable) {
            els.archiveSettingsValidation.className = 'archive-settings-validation ok';
            const free = validation.free_mb == null ? '' : ` · свободно ${validation.free_mb} МБ`;
            setIfChanged(els.archiveSettingsValidation, `Папка доступна${free}`);
        } else if (validation.error) {
            els.archiveSettingsValidation.className = 'archive-settings-validation error';
            setIfChanged(els.archiveSettingsValidation, validation.error);
        }
    }

    const stats = data.batch_stats || {};
    setIfChanged(els.archiveBatchId, data.batch_id || '—');
    setIfChanged(els.archiveBatchGood, stats.good || 0);
    setIfChanged(els.archiveBatchBad, stats.bad || 0);
    setIfChanged(els.archiveBatchCleanup, stats.cleanup || 0);
}

function updateArchiveStatus(data) {
    if (!data || data.available !== true) {
        archiveSettingsData = data || null;
        updateArchiveButton();
        return;
    }
    archiveSettingsData = data;
    updateArchiveButton();
    if (els.archiveSettingsModal && !els.archiveSettingsModal.classList.contains('is-hidden') && !archiveSettingsDirty) {
        renderArchiveSettings(data);
    }
}

async function fetchArchiveSettings() {
    if (archiveSettingsBusy) return;
    archiveSettingsBusy = true;
    updateArchiveButton();
    const data = await apiGet('/api/archive/settings');
    archiveSettingsBusy = false;
    if (data) {
        renderArchiveSettings(data);
        setArchiveSettingsStatus('');
    }
    updateArchiveButton();
}

function markArchiveSettingsDirty() {
    archiveSettingsDirty = true;
    setArchiveSettingsStatus('Есть несохранённые изменения');
}

async function chooseArchiveFolder() {
    if (!archiveSettingsEditable()) return;
    if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.choose_archive_folder === 'function') {
        const result = await window.pywebview.api.choose_archive_folder();
        if (result && result.ok && result.path) {
            if (els.archiveRootPath) els.archiveRootPath.value = result.path;
            markArchiveSettingsDirty();
        } else if (result && result.error) {
            setArchiveSettingsStatus(result.error, 'error');
        }
        return;
    }
    // При открытии UI в обычном браузере абсолютный путь сервера нельзя
    // прочитать из input type=file. Оставляем текстовое поле рабочим.
    if (els.archiveRootPath) {
        els.archiveRootPath.focus();
        els.archiveRootPath.select();
        setArchiveSettingsStatus('Введите путь к папке вручную');
    }
}

async function saveArchiveSettings() {
    if (!archiveSettingsEditable() || archiveSettingsBusy) return;
    const payload = {
        root_path: String(els.archiveRootPath && els.archiveRootPath.value || '').trim(),
    };
    archiveSettingsBusy = true;
    updateArchiveButton();
    if (els.archiveSettingsSave) els.archiveSettingsSave.disabled = true;
    setArchiveSettingsStatus('Проверка и сохранение…');
    const result = await apiPostJson('/api/archive/settings', payload, false);
    archiveSettingsBusy = false;
    if (!result || result.ok !== true) {
        setArchiveSettingsStatus('Не удалось сохранить настройки', 'error');
        updateArchiveButton();
        if (els.archiveSettingsSave) els.archiveSettingsSave.disabled = false;
        return;
    }
    archiveSettingsDirty = false;
    renderArchiveSettings(result.archive);
    setArchiveSettingsStatus('Сохранено');
    if (els.archiveSettingsSave) els.archiveSettingsSave.disabled = false;
    updateArchiveButton();
}

function closeArchiveSettings() {
    if (!els.archiveSettingsModal) return;
    els.archiveSettingsModal.classList.add('is-hidden');
    archiveSettingsDirty = false;
    setArchiveSettingsStatus('');
}

async function openArchiveSettings() {
    if (!archiveSettingsEditable() && state.lineState !== 'IDLE' && state.lineState !== 'STOPPED') return;
    if (!els.archiveSettingsModal) return;
    els.archiveSettingsModal.classList.remove('is-hidden');
    await fetchArchiveSettings();
}

function setupArchiveSettings() {
    if (!els.archiveSettingsOpen) return;
    els.archiveSettingsOpen.addEventListener('click', openArchiveSettings);
    els.archiveSettingsClose?.addEventListener('click', closeArchiveSettings);
    els.archiveSettingsCancel?.addEventListener('click', closeArchiveSettings);
    els.archiveSettingsBackdrop?.addEventListener('click', closeArchiveSettings);
    els.archivePickFolder?.addEventListener('click', chooseArchiveFolder);
    els.archiveSettingsSave?.addEventListener('click', saveArchiveSettings);

    els.archiveRootPath?.addEventListener('input', markArchiveSettingsDirty);
    els.archiveRootPath?.addEventListener('change', markArchiveSettingsDirty);
    updateArchiveButton();
}

if (typeof window !== 'undefined') {
    window.updateArchiveStatus = updateArchiveStatus;
    window.setupArchiveSettings = setupArchiveSettings;
    window.closeArchiveSettings = closeArchiveSettings;
}
