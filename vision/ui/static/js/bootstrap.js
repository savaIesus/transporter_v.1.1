// bootstrap.js — Line Monitor UI module
'use strict';

// ─── Hotkeys ─────────────────────────────────────────────────

function setupHotkeys() {
    window.addEventListener('keydown', (e) => {
        const tag = (e.target && e.target.tagName) || '';
        const inInput = tag === 'INPUT' || tag === 'TEXTAREA';

        if (e.key === 'Escape') {
            if (els.archiveSettingsModal && !els.archiveSettingsModal.classList.contains('is-hidden')) {
                closeArchiveSettings();
                e.preventDefault();
                return;
            }

            const fullscreen = document.querySelector('.gallery-fullscreen');
            if (fullscreen) {
                fullscreen.remove();
                e.preventDefault();
                return;
            }

            if (
                els.galleryModal
                && !els.galleryModal.classList.contains('is-hidden')
            ) {
                closeGallery();
                e.preventDefault();
                return;
            }

            if (inInput) return;

            els.btnExit.click();
            return;
        }

        if (inInput) return;

        if (e.key === 'F5') {
            e.preventDefault();
            els.btnStart.click();
            return;
        }

        if (e.key === 'F6') {
            e.preventDefault();
            els.btnStop.click();
            return;
        }

        if (e.key === 'F7' || e.key === 'p' || e.key === 'P') {
            e.preventDefault();
            if (els.btnPause && !els.btnPause.classList.contains('is-hidden') && !els.btnPause.disabled) {
                els.btnPause.click();
            } else if (els.btnResume && !els.btnResume.classList.contains('is-hidden') && !els.btnResume.disabled) {
                els.btnResume.click();
            }
            return;
        }

        if (e.key === 'F11') {
            e.preventDefault();
            if (
                !document.fullscreenElement
                && typeof document.documentElement.requestFullscreen === 'function'
            ) {
                document.documentElement.requestFullscreen().catch(() => {});
            } else if (
                document.fullscreenElement
                && typeof document.exitFullscreen === 'function'
            ) {
                document.exitFullscreen().catch(() => {});
            }
            return;
        }

        if (state.jogActive) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                if (!e.repeat && !state.jogHoldDirection) {
                    const direction = e.key === 'ArrowLeft' ? '-' : '+';
                    const button = els.jogPanel.querySelector(
                        `.jog-hold-btn[data-direction="${direction}"]`
                    );
                    if (button) beginJogHold(direction, button);
                }
                return;
            }
            if (e.key === 'ArrowUp' || e.key === 'ArrowDown') return;
        }

        if (e.key === 'Tab') {
            e.preventDefault();
            if (viewModeAllowed()) toggleMode();
            return;
        }

        if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') {
            navigateCamera(-1);
            return;
        }

        if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') {
            navigateCamera(1);
            return;
        }

        if (e.key >= '1' && e.key <= '9') {
            const idx = parseInt(e.key, 10) - 1;
            if (idx < state.cameras.length) {
                selectCamera(state.cameras[idx]);
            }
        }
    });
}

window.addEventListener('keyup', e => {
    if (
        state.jogHoldDirection
        && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')
    ) {
        e.preventDefault();
        releaseJogHold(`key released: ${e.key}`);
    }
});

// ─── Init ────────────────────────────────────────────────────

function init() {
    els.cameraContainer = document.querySelector('.camera-container');

    setupButtons();
    setupHotkeys();
    setupGallery();
    if (typeof setupArchiveSettings === 'function') setupArchiveSettings();
    setupCameraHover();
    setupViewModeControls();
    setupJogControls();
    setupDistributorDiagnostics();
    setupSelectedFrameAnalysis();
    setupThresholdsControls();

    fetchBoot();
    state.bootInterval = setInterval(fetchBoot, BOOT_INTERVAL);

    startUiReadyWatcher();

    setInterval(fetchCameras, 2000);
    setInterval(refreshPreviewStrip, PREVIEW_INTERVAL);
    setInterval(updateUptime, UPTIME_INTERVAL);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
