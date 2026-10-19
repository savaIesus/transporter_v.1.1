// history.js — Line Monitor UI module
'use strict';

// ─── Recent parts ────────────────────────────────────────────

const HISTORY_SLOT_COUNT = 10;

function historyPlaceholderCard() {
    return `
        <div class="history-card history-card-placeholder" aria-hidden="true">
            <div class="history-card-id">&nbsp;</div>
            <div class="history-card-symbol">—</div>
        </div>
    `;
}

function updateRecentParts(parts) {
    const visible = (parts || []).slice(-HISTORY_SLOT_COUNT);
    const hash = visible.map(p =>
        `${p.id}:${p.category}:${p.decision}`
    ).join('|');

    if (els.historyCards.dataset.hash === hash) {
        return;
    }
    els.historyCards.dataset.hash = hash;

    const filled = visible.map(p => {
        const cat = (p.category || 'GOOD').toLowerCase();
        let symbol = 'ГОДНО';
        if (cat === 'bad')     symbol = 'БРАК';
        if (cat === 'cleanup') symbol = 'ЗАЧИСТКА';

        return `
            <div class="history-card cat-${cat}"
                 onclick="window._openPartGallery(${p.id})"
                 title="Корпус \u2116\u00a0${p.id} · ${symbol}"
                 style="cursor:pointer">
                <div class="history-card-id">\u2116\u00a0${p.id}</div>
                <div class="history-card-symbol">${symbol}</div>
            </div>
        `;
    });
    const empty = Array.from(
        {length: HISTORY_SLOT_COUNT - filled.length},
        historyPlaceholderCard,
    );
    els.historyCards.innerHTML = [...filled, ...empty].join('');
    animateUiElement(els.historyCards, 'ui-content-change');
}

// ─── Archive Gallery ─────────────────────────────────────────

function setupGallery() {
    if (els.galleryClose) {
        els.galleryClose.addEventListener('click', closeGallery);
    }

    const backdrop = document.querySelector('.gallery-backdrop');
    if (backdrop) {
        backdrop.addEventListener('click', closeGallery);
    }

    if (els.galleryModeDebug) {
        els.galleryModeDebug.addEventListener('click', () => {
            galleryMode = 'debug';
            els.galleryModeDebug.classList.add('active');
            if (els.galleryModeRaw) {
                els.galleryModeRaw.classList.remove('active');
            }
            renderGalleryImages();
        });
    }

    if (els.galleryModeRaw) {
        els.galleryModeRaw.addEventListener('click', () => {
            galleryMode = 'raw';
            if (els.galleryModeDebug) {
                els.galleryModeDebug.classList.remove('active');
            }
            els.galleryModeRaw.classList.add('active');
            renderGalleryImages();
        });
    }
}

async function openGallery(partId) {
    galleryPartId = partId;
    galleryMode   = 'debug';

    if (!els.galleryModal || !els.galleryGrid) return;

    if (els.galleryModeDebug) {
        els.galleryModeDebug.classList.add('active');
    }
    if (els.galleryModeRaw) {
        els.galleryModeRaw.classList.remove('active');
    }

    els.galleryGrid.innerHTML =
        '<div class="gallery-loading">Загрузка...</div>';
    els.galleryModal.classList.remove('is-hidden');

    if (els.galleryPartId) {
        els.galleryPartId.textContent = partId;
    }

    const data = await apiGet(`/api/archive/part/${partId}`);
    if (!data) {
        els.galleryGrid.innerHTML =
            '<div class="gallery-loading">Корпус не найден в архиве</div>';
        return;
    }

    galleryData = data;
    const meta = data.meta || {};

    if (els.galleryCategory) {
        els.galleryCategory.textContent = `КАТЕГОРИЯ: ${categoryLabel(meta.category)}`;
        const cat = (meta.category || '').toLowerCase();

        if (cat === 'good') {
            els.galleryCategory.style.color = 'var(--ok)';
        } else if (cat === 'bad') {
            els.galleryCategory.style.color = 'var(--bad)';
        } else if (cat === 'cleanup') {
            els.galleryCategory.style.color = 'var(--warn)';
        } else {
            els.galleryCategory.style.color = 'var(--text-dim)';
        }
    }

    if (els.galleryDecision) {
        const decision = (!meta.decision || meta.decision === 'none')
            ? 'БЕЗ ДЕФЕКТОВ'
            : meta.decision;
        els.galleryDecision.textContent = `РЕШЕНИЕ: ${decision}`;
    }
    if (els.galleryTime) {
        els.galleryTime.textContent = `ВРЕМЯ: ${meta.time || '—'}`;
    }
    if (els.galleryBatch) {
        els.galleryBatch.textContent = `ПАРТИЯ: ${meta.batch_id || '—'}`;
    }
    if (els.galleryDefects) {
        const defects = meta.defects || [];
        els.galleryDefects.textContent = defects.length
            ? defects.join(', ')
            : 'нет';
        els.galleryDefects.style.color = defects.length
            ? 'var(--bad)'
            : 'var(--ok)';
    }

    renderGalleryImages();
}

function renderGalleryImages() {
    if (!els.galleryGrid || !galleryData) return;

    const roles = galleryData.roles || [];

    if (!roles.length) {
        els.galleryGrid.innerHTML =
            '<div class="gallery-loading">Изображения отсутствуют</div>';
        return;
    }

    const existingCards = els.galleryGrid.querySelectorAll('.gallery-card');

    if (existingCards.length !== roles.length) {
        els.galleryGrid.innerHTML = roles.map(r => {
            const url = getGalleryUrl(r);
            if (!url) return '';

            return `
                <div class="gallery-card" data-role="${r.role}">
                    <div class="gallery-card-label">${cameraRoleLabel(r.role)}</div>
                    <div class="gallery-card-img-wrap">
                        <img class="gallery-img-fade-in"
                             src="${url}"
                             alt="${r.role}"
                             onload="window._galleryImageLoaded(this)"
                             onerror="window._galleryImageError(this)"
                             onclick="window._galleryFullscreen(this.src)">
                    </div>
                </div>
            `;
        }).join('');
        attachGalleryImageErrorHandlers();
        return;
    }

    existingCards.forEach(card => {
        const role     = card.dataset.role;
        const roleData = roles.find(r => r.role === role);
        if (!roleData) return;

        const wrap = card.querySelector('.gallery-card-img-wrap');
        if (!wrap) return;

        const oldImg = wrap.querySelector('img:not(.gallery-img-fade-out)');
        if (!oldImg) return;

        const newUrl = getGalleryUrl(roleData);
        if (!newUrl) return;

        if (oldImg.src.endsWith(newUrl.split('?')[0])) return;

        const ghost = oldImg.cloneNode(true);
        ghost.className = 'gallery-img-fade-out';
        ghost.removeAttribute('onclick');
        wrap.appendChild(ghost);

        ghost.addEventListener('animationend', () => {
            ghost.remove();
        });

        oldImg.classList.remove('gallery-img-fade-in');
        void oldImg.offsetWidth;
        oldImg.classList.add('gallery-img-fade-in');
        oldImg.src = newUrl;
    });
    attachGalleryImageErrorHandlers();
}

function attachGalleryImageErrorHandlers() {
    if (!els.galleryGrid) return;
    els.galleryGrid.querySelectorAll('.gallery-card-img-wrap img').forEach(img => {
        if (img.dataset.errorHandlerAttached === '1') return;
        img.dataset.errorHandlerAttached = '1';
        img.addEventListener('load', () => markGalleryImageLoaded(img));
        img.addEventListener('error', () => markGalleryImageError(img));
    });
}

function markGalleryImageLoaded(img) {
    const wrap = img && img.closest('.gallery-card-img-wrap');
    if (!wrap) return;
    wrap.classList.remove('image-error');
    wrap.removeAttribute('data-error-label');
}

function markGalleryImageError(img) {
    const wrap = img && img.closest('.gallery-card-img-wrap');
    if (!wrap) return;
    wrap.classList.add('image-error');
    wrap.dataset.errorLabel = 'ИЗОБРАЖЕНИЕ НЕДОСТУПНО';
}

function getGalleryUrl(roleData) {
    if (galleryMode === 'raw') {
        return roleData.raw_overlay_url
            || roleData.raw_url
            || roleData.debug_url
            || '';
    }
    return roleData.debug_url
        || roleData.raw_overlay_url
        || roleData.raw_url
        || '';
}

function closeGallery() {
    if (els.galleryModal) {
        els.galleryModal.classList.add('is-hidden');
    }
    galleryData   = null;
    galleryPartId = null;
}

// ─── Global handlers ─────────────────────────────────────────

window._openPartGallery = function(partId) {
    openGallery(partId);
};

window._galleryImageLoaded = markGalleryImageLoaded;
window._galleryImageError = markGalleryImageError;

window._galleryFullscreen = function(src) {
    const existing = document.querySelector('.gallery-fullscreen');
    if (existing) existing.remove();

    const div = document.createElement('div');
    div.className = 'gallery-fullscreen';
    div.innerHTML = `<img src="${src}" alt="Полноэкранное изображение">`;
    div.addEventListener('click', () => div.remove());
    document.body.appendChild(div);
};

