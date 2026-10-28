/* ==========================================================================
   Krafen — единое клиентское ядро (атмосфера, инерционный скролл, утилиты,
   админ-сессия и сохранение коллекций с защитой от конфликта ревизий).
   ========================================================================== */
(() => {
    const ATMO_KEY = 'krafen-atmo';
    const HEART_KEY = 'krafen-heart';

    function escapeHTML(value) {
        return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        }[ch]));
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
            revealObserver = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('visible');
                        revealObserver.unobserve(entry.target);
                    }
                });
            }, { threshold: 0.08, rootMargin: '0px 0px -6% 0px' });
        }
        nodes.forEach((el) => revealObserver.observe(el));
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
        const ctx = cv.getContext('2d');
        let W = 0, H = 0, drops = [];

        function spawn(any) {
            return {
                x: Math.random() * W,
                y: any ? Math.random() * H : -20,
                l: 10 + Math.random() * 18,
                v: 9 + Math.random() * 9,
                o: 0.06 + Math.random() * 0.2,
            };
        }

        function resize() {
            W = cv.width = window.innerWidth;
            H = cv.height = window.innerHeight;
            drops = Array.from({ length: Math.floor(W / 6) }, () => spawn(true));
        }

        window.addEventListener('resize', resize, { passive: true });
        resize();

        (function loop() {
            ctx.clearRect(0, 0, W, H);
            ctx.strokeStyle = '#9aa4b8';
            ctx.lineWidth = 1;
            for (const d of drops) {
                ctx.globalAlpha = d.o;
                ctx.beginPath();
                ctx.moveTo(d.x, d.y);
                ctx.lineTo(d.x - 1.5, d.y + d.l);
                ctx.stroke();
                d.y += d.v;
                d.x -= 0.6;
                if (d.y > H) Object.assign(d, spawn(false));
            }
            requestAnimationFrame(loop);
        })();

        const flash = document.getElementById('flash');
        function schedule() {
            setTimeout(lightning, 6000 + Math.random() * 14000);
        }
        function lightning() {
            if (!flash || document.body.classList.contains('calm')) return schedule();
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
        const heart = document.getElementById('heart');
        if (heart) {
            try {
                if (localStorage.getItem(HEART_KEY) === '1') heart.classList.add('loved');
            } catch {}
            heart.addEventListener('click', () => {
                const loved = heart.classList.toggle('loved');
                try { localStorage.setItem(HEART_KEY, loved ? '1' : '0'); } catch {}
            });
        }

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
            toggle.setAttribute('aria-expanded', 'false');
        };
        const open = () => {
            drawer.classList.add('open');
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
    }

    /* ---------- плавный инерционный скролл + параллакс ---------- */
    const lerp = (a, b, t) => a + (b - a) * t;
    const smooth = { target: 0, current: 0, glow: 0, glowT: 0, hero: 0, heroT: 0, rail: 0, railT: 0 };

    function anyOverlayOpen() {
        return Boolean(document.querySelector('.modal.open, .mobile-nav.open'));
    }

    function initSmooth() {
        const isTouch = matchMedia('(pointer: coarse)').matches;
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        const heroInner = document.querySelector('.hero-inner');
        const ghosts = document.querySelectorAll('.ghost');
        const railLinks = document.querySelectorAll('.rail a');
        const railProgress = document.getElementById('rail-progress');
        const glow = document.getElementById('glow');
        const railH = () => window.innerHeight * 0.64;

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
            const upd = () => {
                const y = window.scrollY;
                const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
                if (railProgress) railProgress.style.transform = `translateY(${railH() * (max > 0 ? y / max : 0)}px)`;
            };
            window.addEventListener('scroll', upd, { passive: true });
            upd();
            return;
        }

        let raf = null;
        function animate() {
            const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
            if (smooth.target > max) smooth.target = max;

            smooth.current = lerp(smooth.current, smooth.target, 0.11);
            if (Math.abs(smooth.target - smooth.current) < 0.4) smooth.current = smooth.target;
            window.scrollTo(0, smooth.current);

            smooth.hero = lerp(smooth.hero, smooth.heroT, 0.08);
            smooth.glow = lerp(smooth.glow, smooth.glowT, 0.05);
            smooth.rail = lerp(smooth.rail, smooth.railT, 0.12);

            const y = smooth.current;
            if (heroInner) {
                heroInner.style.transform = `translateY(${y * 0.3}px)`;
                heroInner.style.opacity = String(Math.max(0, 1 - y / 640));
            }
            const p = max > 0 ? y / max : 0;
            if (glow) glow.style.transform = `translateY(${(p - 0.5) * 55}vh)`;
            if (railProgress) railProgress.style.transform = `translateY(${smooth.rail * Math.max(0, railH() - 40)}px)`;
            ghosts.forEach((g) => {
                const r = g.parentElement.getBoundingClientRect();
                g.style.transform = `translateY(${r.top * -0.1}px)`;
            });

            if (smooth.current !== smooth.target || Math.abs(smooth.glow - smooth.glowT) > 0.001) {
                raf = requestAnimationFrame(animate);
            } else {
                raf = null;
            }
        }

        function kick() {
            const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
            if (smooth.target > max) smooth.target = max;
            smooth.heroT = smooth.target;
            smooth.glowT = smooth.target;
            smooth.railT = max > 0 ? smooth.target / max : 0;
            if (!raf) raf = requestAnimationFrame(animate);
        }

        let wheelAcc = 0, wheelRaf = null;
        window.addEventListener('wheel', (e) => {
            if (e.ctrlKey || anyOverlayOpen()) return;
            const sc = e.target.closest && e.target.closest('.chat-body, .modal-box, [data-scroll]');
            if (sc && sc.scrollHeight > sc.clientHeight) return;
            e.preventDefault();
            const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
            wheelAcc += e.deltaY * (e.deltaMode === 1 ? 16 : 1);
            if (!wheelRaf) {
                wheelRaf = requestAnimationFrame(() => {
                    smooth.target = Math.max(0, Math.min(max, smooth.target + wheelAcc));
                    wheelAcc = 0;
                    wheelRaf = null;
                    kick();
                });
            }
        }, { passive: false });

        window.addEventListener('scroll', () => {
            if (Math.abs(window.scrollY - smooth.current) > 2) {
                smooth.target = window.scrollY;
                kick();
            }
        }, { passive: true });

        window.addEventListener('keydown', (e) => {
            if (anyOverlayOpen()) return;
            const tag = (e.target && e.target.tagName) || '';
            if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || e.target?.isContentEditable) return;
            const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
            const step = {
                PageDown: window.innerHeight * 0.9,
                PageUp: -window.innerHeight * 0.9,
                ArrowDown: 90,
                ArrowUp: -90,
                Home: -1e9,
                End: 1e9,
            }[e.key];
            if (step !== undefined) {
                smooth.target = Math.max(0, Math.min(max, smooth.target + step));
                e.preventDefault();
                kick();
            }
        });

        document.querySelectorAll('a[href^="#"]').forEach((a) => {
            a.addEventListener('click', (e) => {
                const href = a.getAttribute('href');
                if (!href || href === '#') {
                    e.preventDefault();
                    smooth.target = 0;
                    kick();
                    return;
                }
                const targetEl = document.querySelector(href);
                if (!targetEl) return;
                e.preventDefault();
                const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
                smooth.target = Math.max(0, Math.min(max, targetEl.getBoundingClientRect().top + window.scrollY - 36));
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

        function apply(next) {
            active = Boolean(next);
            document.body.classList.toggle('is-admin', active);
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

    document.addEventListener('DOMContentLoaded', () => {
        observeReveals();
        initRain();
        initDock();
        initMobileNav();
        initSmooth();
    });

    window.KrafenUI = {
        escapeHTML,
        toast,
        observeReveals,
        openModal,
        closeModal,
        bindModalDismiss,
        createAdminController,
    };
})();
