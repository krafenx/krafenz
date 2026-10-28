(() => {
    const init = () => {
        if (document.getElementById('krafen-atmosphere')) return;

        const atmosphere = document.createElement('div');
        atmosphere.id = 'krafen-atmosphere';
        atmosphere.className = 'krafen-atmosphere';
        atmosphere.setAttribute('aria-hidden', 'true');
        atmosphere.innerHTML = `
            <div class="krafen-glow"></div>
            <div class="krafen-fog"></div>
            <div class="krafen-fog f2"></div>
            <div class="krafen-vignette"></div>
            <div class="krafen-grain"></div>
        `;
        document.body.prepend(atmosphere);
        document.body.classList.add('krafen-unified');

        const targets = [...document.querySelectorAll(`
            main,
            main section,
            main .glass,
            main .media-card,
            main .game-card,
            main .wish-card,
            main .task-card,
            main .music-player-card,
            main .time-panel,
            main .widget-stat,
            main .search-result,
            main .profile,
            main .spec-card
        `)].filter((el) => !el.closest('.modal') && !el.closest('[data-no-reveal]'));

        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (reduced || !('IntersectionObserver' in window)) {
            targets.forEach((el) => el.classList.add('visible'));
            return;
        }

        targets.forEach((el, index) => {
            el.classList.add('reveal');
            el.style.setProperty('--d', `${Math.min((index % 8) * 0.045, 0.32)}s`);
        });

        const observer = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('visible');
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.08, rootMargin: '0px 0px -6% 0px' });

        targets.forEach((el) => observer.observe(el));
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();
