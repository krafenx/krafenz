/* ==========================================================================
   Krafen — единое клиентское ядро (атмосфера, инерционный скролл, утилиты,
   админ-сессия и сохранение коллекций с защитой от конфликта ревизий).
   ========================================================================== */
(() => {
    const ATMO_KEY = 'krafen-atmo';

    document.querySelectorAll('link[data-async-css]').forEach((el) => {
        el.media = 'all';
    });

    const PROXY_IMG_HOSTS = new Set([
        'cdn.discordapp.com',
        'media.discordapp.net',
        'shikimori.one',
        'nyaa.shikimori.one',
        'desu.shikimori.one',
        'dere.shikimori.one',
        'kawai.shikimori.one',
        'image.tmdb.org',
        'images.igdb.com',
        'i.scdn.co',
        'lastfm.freetls.fastly.net',
        'lastfm-img2.akamaized.net',
        'static-cdn.jtvnw.net',
        'drive.google.com',
        'lh3.googleusercontent.com',
        'images.weserv.nl',
        'm.media-amazon.com',
    ]);

    function escapeHTML(value) {
        return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        }[ch]));
    }

    function safeHttpUrl(value, fallback = '') {
        const raw = String(value ?? '').trim();
        if (!raw) return fallback;
        try {
            const parsed = new URL(raw, location.origin);
            if (parsed.protocol === 'https:' || (parsed.protocol === 'http:' && parsed.origin === location.origin)) {
                return parsed.href;
            }
        } catch {}
        return fallback;
    }

    function proxyImgUrl(value, fallback = '') {
        const safe = safeHttpUrl(value, '');
        if (!safe) return fallback;
        try {
            const parsed = new URL(safe, location.origin);
            if (parsed.origin === location.origin) return safe;
            if (parsed.protocol === 'https:' && PROXY_IMG_HOSTS.has(parsed.hostname.toLowerCase())) {
                return `/api/img?u=${encodeURIComponent(parsed.href)}`;
            }
        } catch {}
        return safe;
    }

    /* ---------- уведомления (toast) ---------- */
    let toastTimer = null;
    function toast(message, color) {
        let el = document.getElementById('toast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'toast';
            el.className = 'toast';
            el.setAttribute('role', 'status');
            el.setAttribute('aria-live', 'polite');
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.style.color = color || '';
        el.style.borderColor = color ? `${color}55` : '';
        clearTimeout(toastTimer);
        el.classList.add('show');
        toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
    }

    /* ---------- blur-in появление при скролле ---------- */
    let revealObserver = null;
    function observeReveals(root = document) {
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        const nodes = root.querySelectorAll('.reveal:not(.visible)');
        if (!nodes.length) return;
        if (reduced || !('IntersectionObserver' in window)) {
            nodes.forEach((el) => el.classList.add('visible'));
            return;
        }
        if (!revealObserver) {
            // Двусторонний reveal: блок появляется при входе в экран и снова
            // скрывается при уходе из него, поэтому при прокрутке вверх анимация повторяется.
            revealObserver = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    entry.target.classList.toggle('visible', entry.isIntersecting);
                });
            }, { threshold: 0.08, rootMargin: '0px 0px -6% 0px' });
        }
        nodes.forEach((el) => revealObserver.observe(el));
    }

    /* ---------- лёгкая предгенерация зерна (без live SVG feTurbulence) ---------- */
    let cachedGrainDataUrl = '';
    function initGrain() {
        const grainEl = document.querySelector('.grain');
        if (!grainEl) return;
        if (!cachedGrainDataUrl) {
            try {
                const c = document.createElement('canvas');
                c.width = 128;
                c.height = 128;
                const gctx = c.getContext('2d');
                const img = gctx.createImageData(128, 128);
                const d = img.data;
                let seed = 0x5f3759df;
                for (let i = 0; i < d.length; i += 4) {
                    seed = (seed * 1664525 + 1013904223) >>> 0;
                    const v = (seed >>> 24) & 255;
                    d[i] = v;
                    d[i + 1] = v;
                    d[i + 2] = v;
                    d[i + 3] = 255;
                }
                gctx.putImageData(img, 0, 0);
                cachedGrainDataUrl = `url("${c.toDataURL('image/png')}")`;
            } catch {}
        }
        if (cachedGrainDataUrl) {
            grainEl.style.backgroundImage = cachedGrainDataUrl;
        }
    }

    /* ---------- мгновенный prefetch внутренних страниц при наведении ---------- */
    const prefetchedUrls = new Set();
    function initNavPrefetch() {
        const prefetchHref = (rawHref) => {
            if (!rawHref || rawHref.startsWith('#') || rawHref.startsWith('mailto:') || rawHref.startsWith('javascript:')) return;
            let url;
            try {
                url = new URL(rawHref, location.origin);
            } catch {
                return;
            }
            if (url.origin !== location.origin || url.pathname === location.pathname) return;
            if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/vods/')) return;
            const key = url.pathname + url.search;
            if (prefetchedUrls.has(key)) return;
            prefetchedUrls.add(key);
            const link = document.createElement('link');
            link.rel = 'prefetch';
            link.as = 'document';
            link.href = key;
            document.head.appendChild(link);
        };

        document.addEventListener('pointerover', (e) => {
            const a = e.target.closest && e.target.closest('a[href]');
            if (a && !a.target && !a.hasAttribute('download')) prefetchHref(a.getAttribute('href'));
        }, { passive: true });
        document.addEventListener('touchstart', (e) => {
            const a = e.target.closest && e.target.closest('a[href]');
            if (a && !a.target && !a.hasAttribute('download')) prefetchHref(a.getAttribute('href'));
        }, { passive: true });
    }

    /* ---------- плавная смена фильтров / списков ---------- */
    function smoothSwap(targetOrFn, maybeFn) {
        const fn = typeof targetOrFn === 'function' ? targetOrFn : maybeFn;
        if (typeof fn !== 'function') return;
        const explicitTarget = typeof targetOrFn === 'function' ? null : targetOrFn;
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (!reduced && typeof document.startViewTransition === 'function' && !document.hidden) {
            const targetEl = explicitTarget || document.querySelector('#cards-grid, #game-list, #wish-list, #categories-container');
            const root = document.documentElement;
            root.classList.add('kf-swapping');
            if (targetEl) targetEl.style.viewTransitionName = 'kf-swap-target';
            try {
                const vt = document.startViewTransition(() => { fn(); });
                Promise.resolve(vt.finished).catch(() => {}).finally(() => {
                    root.classList.remove('kf-swapping');
                    if (targetEl) targetEl.style.viewTransitionName = '';
                });
                return;
            } catch {
                root.classList.remove('kf-swapping');
                if (targetEl) targetEl.style.viewTransitionName = '';
            }
        }
        fn();
    }

    /* ---------- атмосфера: дождь + молнии + туман ---------- */
    function setAtmo(on, save) {
        document.body.classList.toggle('calm', !on);
        const btn = document.getElementById('atmo');
        if (btn) btn.style.opacity = on ? '1' : '.6';
        if (save) {
            try { localStorage.setItem(ATMO_KEY, on ? '1' : '0'); } catch {}
        }
    }

    function initRain() {
        const cv = document.getElementById('rain');
        if (!cv) return;
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (reduced) return;
        const ctx = cv.getContext('2d', { alpha: true, desynchronized: true }) || cv.getContext('2d');
        if (!ctx) return;

        let W = 0, H = 0;
        const BUCKET_ALPHAS = [0.08, 0.14, 0.22];
        let buckets = [[], [], []];
        let rainRaf = null, lastTs = 0, resizeTimer = null;

        function spawn(any) {
            return {
                x: Math.random() * W,
                y: any ? Math.random() * H : -20 - Math.random() * 40,
                l: 10 + Math.random() * 18,
                v: 8.5 + Math.random() * 8.5,
            };
        }

        function rebuildDrops() {
            W = cv.width = window.innerWidth;
            H = cv.height = window.innerHeight;
            const total = Math.min(260, Math.max(48, Math.floor(W / 7)));
            buckets = [[], [], []];
            for (let i = 0; i < total; i++) {
                buckets[i % 3].push(spawn(true));
            }
        }

        window.addEventListener('resize', () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(rebuildDrops, 100);
        }, { passive: true });
        rebuildDrops();

        function loop(now) {
            if (document.hidden) {
                rainRaf = null;
                return;
            }
            const dtScale = lastTs ? Math.min(2.4, Math.max(0.2, (now - lastTs) / 16.6667)) : 1;
            lastTs = now;

            ctx.clearRect(0, 0, W, H);
            ctx.strokeStyle = '#9aa4b8';
            ctx.lineWidth = 1;

            const dx = 0.6 * dtScale;
            for (let b = 0; b < 3; b++) {
                const list = buckets[b];
                if (!list.length) continue;
                ctx.globalAlpha = BUCKET_ALPHAS[b];
                ctx.beginPath();
                for (let i = 0; i < list.length; i++) {
                    const d = list[i];
                    ctx.moveTo(d.x, d.y);
                    ctx.lineTo(d.x - 1.5, d.y + d.l);
                    d.y += d.v * dtScale;
                    d.x -= dx;
                    if (d.y > H) {
                        d.x = Math.random() * W;
                        d.y = -20 - Math.random() * 30;
                    }
                }
                ctx.stroke();
            }
            rainRaf = requestAnimationFrame(loop);
        }

        function startRain() {
            if (!rainRaf && !document.hidden) {
                lastTs = 0;
                rainRaf = requestAnimationFrame(loop);
            }
        }
        document.addEventListener('visibilitychange', startRain);
        startRain();

        const flash = document.getElementById('flash');
        function schedule() {
            setTimeout(lightning, 6000 + Math.random() * 14000);
        }
        function lightning() {
            if (!flash || document.hidden || document.body.classList.contains('calm')) return schedule();
            const bursts = 1 + Math.floor(Math.random() * 3);
            let n = 0;
            (function flick() {
                flash.style.transition = 'none';
                flash.style.opacity = String(0.18 + Math.random() * 0.3);
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    flash.style.transition = 'opacity .6s ease-out';
                    flash.style.opacity = '0';
                }));
                if (++n < bursts) setTimeout(flick, 90 + Math.random() * 140);
            })();
            schedule();
        }
        setTimeout(lightning, 2500);
    }

    function initDock() {
        try {
            const saved = localStorage.getItem(ATMO_KEY);
            if (saved !== null) setAtmo(saved === '1', false);
        } catch {}

        const atmo = document.getElementById('atmo');
        if (atmo) {
            atmo.addEventListener('click', () => {
                setAtmo(document.body.classList.contains('calm'), true);
            });
        }
    }

    /* ---------- мобильное меню ---------- */
    function initMobileNav() {
        const toggle = document.getElementById('menu-toggle');
        const drawer = document.getElementById('mobile-nav');
        if (!toggle || !drawer) return;
        const closeBtn = drawer.querySelector('.mobile-nav-close');

        const close = () => {
            drawer.classList.remove('open');
            document.body.classList.remove('nav-open');
            toggle.setAttribute('aria-expanded', 'false');
        };
        const open = () => {
            drawer.classList.add('open');
            document.body.classList.add('nav-open');
            toggle.setAttribute('aria-expanded', 'true');
        };

        toggle.addEventListener('click', () => {
            if (drawer.classList.contains('open')) close();
            else open();
        });
        if (closeBtn) closeBtn.addEventListener('click', close);
        drawer.querySelectorAll('a').forEach((a) => a.addEventListener('click', close));
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && drawer.classList.contains('open')) close();
        });
        window.addEventListener('resize', () => {
            if (window.innerWidth > 980 && drawer.classList.contains('open')) close();
        }, { passive: true });
    }

    /* ---------- плавный инерционный скролл (dt-независимый 60–360 Гц, без reflow) ---------- */
    const damp = (a, b, lambda, dt) => b + (a - b) * Math.exp(-lambda * dt);
    const smooth = { target: 0, current: 0, glow: 0, glowT: 0, hero: 0, heroT: 0, rail: 0, railT: 0 };

    function anyOverlayOpen() {
        return Boolean(document.querySelector('.modal.open, .mobile-nav.open'));
    }

    function initSmooth() {
        const isTouch = matchMedia('(pointer: coarse)').matches;
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        const heroInner = document.querySelector('.hero-inner');
        const ghostNodes = Array.from(document.querySelectorAll('.ghost'));
        const railLinks = document.querySelectorAll('.rail a');
        const railProgress = document.getElementById('rail-progress');
        const glow = document.getElementById('glow');

        let vh = window.innerHeight || 1;
        let maxScroll = Math.max(0, document.documentElement.scrollHeight - vh);
        let ghostOffsets = [];
        let kickFn = null;

        function measureLayout() {
            vh = window.innerHeight || 1;
            maxScroll = Math.max(0, document.documentElement.scrollHeight - vh);
            const sy = window.scrollY;
            ghostOffsets = ghostNodes.map((el) => {
                const parent = el.parentElement;
                return parent ? parent.getBoundingClientRect().top + sy : 0;
            });
            let clamped = false;
            if (smooth.target > maxScroll) {
                smooth.target = maxScroll;
                clamped = true;
            }
            if (smooth.current > maxScroll) {
                smooth.current = maxScroll;
                clamped = true;
            }
            if (clamped && kickFn) kickFn();
        }

        if ('ResizeObserver' in window) {
            const ro = new ResizeObserver(() => measureLayout());
            ro.observe(document.documentElement);
            const mainEl = document.querySelector('main');
            if (mainEl) ro.observe(mainEl);
        }
        window.addEventListener('resize', measureLayout, { passive: true });
        measureLayout();

        if (railLinks.length) {
            const secObserver = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    if (!entry.isIntersecting) return;
                    railLinks.forEach((a) => a.classList.toggle('active', a.dataset.sec === entry.target.id));
                });
            }, { rootMargin: '-40% 0px -40% 0px' });
            document.querySelectorAll('.section[id]').forEach((s) => secObserver.observe(s));
        }

        if (reduced || isTouch) {
            let touchRaf = null;
            const upd = () => {
                touchRaf = null;
                const y = window.scrollY;
                const rH = Math.max(0, vh * 0.64 - 40);
                if (railProgress) railProgress.style.transform = `translate3d(0, ${rH * (maxScroll > 0 ? Math.min(1, y / maxScroll) : 0)}px, 0)`;
            };
            kickFn = upd;
            window.addEventListener('scroll', () => {
                if (!touchRaf) touchRaf = requestAnimationFrame(upd);
            }, { passive: true });
            upd();
            return;
        }

        let raf = null;
        let lastFrameTs = 0;
        let lastProgrammaticY = Math.round(window.scrollY);
        let programmaticUntil = 0;

        function animate(now) {
            const dt = lastFrameTs ? Math.min(0.05, Math.max(0.001, (now - lastFrameTs) / 1000)) : (1 / 60);
            lastFrameTs = now;

            if (smooth.target > maxScroll) smooth.target = maxScroll;

            smooth.current = damp(smooth.current, smooth.target, 9.6, dt);
            if (Math.abs(smooth.target - smooth.current) < 0.15) smooth.current = smooth.target;

            lastProgrammaticY = Math.round(smooth.current);
            programmaticUntil = now + 90;
            window.scrollTo(0, smooth.current);

            smooth.hero = damp(smooth.hero, smooth.heroT, 8.2, dt);
            smooth.glow = damp(smooth.glow, smooth.glowT, 4.6, dt);
            smooth.rail = damp(smooth.rail, smooth.railT, 11.0, dt);

            const y = smooth.current;
            if (heroInner) {
                heroInner.style.transform = `translate3d(0, ${y * 0.28}px, 0)`;
                heroInner.style.opacity = String(Math.max(0, 1 - y / 660));
            }
            const p = maxScroll > 0 ? smooth.glow / maxScroll : 0;
            if (glow) glow.style.transform = `translate3d(0, ${(p - 0.5) * 52}vh, 0)`;
            if (railProgress) {
                const rSpan = Math.max(0, vh * 0.64 - 40);
                railProgress.style.transform = `translate3d(0, ${smooth.rail * rSpan}px, 0)`;
            }
            for (let i = 0; i < ghostNodes.length; i++) {
                const relTop = ghostOffsets[i] - y;
                if (relTop > -vh * 1.5 && relTop < vh * 1.8) {
                    ghostNodes[i].style.transform = `translate3d(0, ${relTop * -0.1}px, 0)`;
                }
            }

            if (Math.abs(smooth.target - smooth.current) > 0.15 || Math.abs(smooth.glow - smooth.glowT) > 0.4) {
                raf = requestAnimationFrame(animate);
            } else {
                raf = null;
                lastFrameTs = 0;
            }
        }

        function kick() {
            if (smooth.target > maxScroll) smooth.target = maxScroll;
            smooth.heroT = smooth.target;
            smooth.glowT = smooth.target;
            smooth.railT = maxScroll > 0 ? smooth.target / maxScroll : 0;
            if (!raf) {
                lastFrameTs = 0;
                raf = requestAnimationFrame(animate);
            }
        }
        kickFn = kick;

        window.addEventListener('wheel', (e) => {
            if (e.ctrlKey || anyOverlayOpen()) return;
            const sc = e.target.closest && e.target.closest('.chat-body, .modal-box, [data-scroll]');
            if (sc && sc.scrollHeight > sc.clientHeight) return;
            e.preventDefault();
            const mult = e.deltaMode === 1 ? 28 : (e.deltaMode === 2 ? vh * 0.85 : 1);
            const delta = e.deltaY * mult;
            smooth.target = Math.max(0, Math.min(maxScroll, smooth.target + delta));
            kick();
        }, { passive: false });

        window.addEventListener('scroll', () => {
            const sy = window.scrollY;
            if (performance.now() < programmaticUntil && Math.abs(sy - lastProgrammaticY) <= 6) return;
            if (Math.abs(sy - smooth.current) > 4) {
                smooth.current = sy;
                smooth.target = sy;
                kick();
            }
        }, { passive: true });

        window.addEventListener('keydown', (e) => {
            if (anyOverlayOpen()) return;
            const tag = (e.target && e.target.tagName) || '';
            if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || e.target?.isContentEditable) return;
            const step = {
                PageDown: vh * 0.88,
                PageUp: -vh * 0.88,
                ArrowDown: 100,
                ArrowUp: -100,
                Home: -1e9,
                End: 1e9,
            }[e.key];
            if (step !== undefined) {
                smooth.target = Math.max(0, Math.min(maxScroll, smooth.target + step));
                e.preventDefault();
                kick();
            }
        });

        document.querySelectorAll('a[href^="#"]').forEach((a) => {
            a.addEventListener('click', (e) => {
                const href = a.getAttribute('href');
                if (!href || !href.startsWith('#')) return;
                if (href === '#' || href === '#top') {
                    e.preventDefault();
                    smooth.target = 0;
                    kick();
                    return;
                }
                let targetEl = null;
                try {
                    targetEl = document.getElementById(decodeURIComponent(href.slice(1)));
                } catch {}
                if (!targetEl) {
                    try { targetEl = document.querySelector(href); } catch {}
                }
                if (!targetEl) return;
                e.preventDefault();
                measureLayout();
                smooth.target = Math.max(0, Math.min(maxScroll, targetEl.getBoundingClientRect().top + window.scrollY - 36));
                kick();
            });
        });

        smooth.current = smooth.target = window.scrollY;
        kick();
    }

    /* ---------- модальные окна ---------- */
    function openModal(id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.classList.add('open');
        document.body.style.overflow = 'hidden';
    }

    function closeModal(id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.classList.remove('open');
        if (!document.querySelector('.modal.open')) {
            document.body.style.overflow = '';
        }
    }

    function bindModalDismiss(onClose) {
        document.querySelectorAll('[data-close]').forEach((btn) => {
            btn.addEventListener('click', () => {
                const id = btn.dataset.close;
                closeModal(id);
                if (onClose) onClose(id);
            });
        });
        document.querySelectorAll('.modal').forEach((modal) => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    closeModal(modal.id);
                    if (onClose) onClose(modal.id);
                }
            });
        });
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape') return;
            document.querySelectorAll('.modal.open').forEach((modal) => {
                closeModal(modal.id);
                if (onClose) onClose(modal.id);
            });
        });
    }

    /* ---------- единый менеджер админ-сессии ---------- */
    function createAdminController({ onChange } = {}) {
        let active = false;
        const lockBtn = document.getElementById('lock-btn');
        const logoutBtn = document.getElementById('admin-logout-btn');
        const loginBtn = document.getElementById('login-btn');
        const passInput = document.getElementById('admin-password-input');
        const errBox = document.getElementById('admin-login-error');
        const adminBar = document.getElementById('admin-bar');

        if (adminBar && !adminBar.querySelector('a[href="/admin"]') && location.pathname !== '/admin') {
            const dashLink = document.createElement('a');
            dashLink.href = '/admin';
            dashLink.className = 'btn-action';
            dashLink.textContent = 'панель управления ↗';
            const firstAction = adminBar.querySelector('a.btn-action, button.btn-action');
            if (firstAction) adminBar.insertBefore(dashLink, firstAction);
            else adminBar.appendChild(dashLink);
        }

        function apply(next) {
            const wasActive = active;
            active = Boolean(next);
            document.body.classList.toggle('is-admin', active);
            if (!active && wasActive) {
                ['modal-add', 'modal-game', 'modal-item', 'modal-wish'].forEach((id) => closeModal(id));
            }
            if (lockBtn) {
                lockBtn.classList.toggle('is-admin-active', active);
                const label = active ? 'Выйти из режима администратора' : 'Войти в режим администратора';
                lockBtn.title = label;
                lockBtn.setAttribute('aria-label', label);
            }
            if (onChange) onChange(active);
        }

        async function check() {
            try {
                const res = await fetch('/api/admin', {
                    headers: { Accept: 'application/json' },
                    credentials: 'same-origin',
                });
                const data = await res.json().catch(() => ({}));
                apply(Boolean(res.ok && data.ok));
            } catch {
                apply(false);
            }
        }

        async function login() {
            if (!passInput) return;
            const password = passInput.value.trim();
            if (!password) return;
            if (loginBtn) {
                loginBtn.disabled = true;
                loginBtn.textContent = 'Проверка...';
            }
            if (errBox) errBox.classList.add('hidden');
            try {
                const res = await fetch('/api/admin', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ password }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    if (errBox) {
                        errBox.textContent = data.error === 'admin_password_missing'
                            ? 'На сервере не задан ADMIN_PASSWORD (укажите его в .env).'
                            : 'Неверный пароль или сервер недоступен.';
                        errBox.classList.remove('hidden');
                    }
                } else {
                    apply(true);
                    closeModal('modal-admin');
                    passInput.value = '';
                    toast('Режим администратора включён');
                }
            } catch {
                if (errBox) {
                    errBox.textContent = 'Ошибка соединения с сервером.';
                    errBox.classList.remove('hidden');
                }
            } finally {
                if (loginBtn) {
                    loginBtn.disabled = false;
                    loginBtn.textContent = 'Войти';
                }
            }
        }

        async function logout() {
            await fetch('/api/admin', {
                method: 'DELETE',
                credentials: 'same-origin',
            }).catch(() => {});
            apply(false);
            toast('Вы вышли из режима администратора');
        }

        if (lockBtn) {
            lockBtn.addEventListener('click', () => {
                if (active) {
                    if (confirm('Выйти из режима администратора?')) logout();
                    return;
                }
                if (errBox) errBox.classList.add('hidden');
                openModal('modal-admin');
                setTimeout(() => passInput?.focus(), 100);
            });
        }
        if (logoutBtn) logoutBtn.addEventListener('click', logout);
        if (loginBtn) loginBtn.addEventListener('click', login);
        if (passInput) {
            passInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') login();
            });
        }

        return {
            isAdmin: () => active,
            setAdmin: apply,
            check,
            logout,
        };
    }

    function onReady(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn, { once: true });
        } else {
            fn();
        }
    }

    onReady(() => {
        initGrain();
        observeReveals();
        initRain();
        initDock();
        initMobileNav();
        initSmooth();
        initNavPrefetch();
    });

    window.KrafenUI = {
        onReady,
        escapeHTML,
        safeHttpUrl,
        proxyImgUrl,
        toast,
        observeReveals,
        smoothSwap,
        openModal,
        closeModal,
        bindModalDismiss,
        createAdminController,
    };
})();
