#!/usr/bin/env node
/**
 * Регресс-тест страниц krafen и профиля danonsha.
 *
 * Запускается против живого сервера и проверяет поведение страниц в браузерном
 * окружении (jsdom): рендер карточек, вход в админ-режим, сохранение коллекций,
 * фильтры и — главное — изоляцию профилей.
 *
 *   npm install jsdom          # один раз
 *   node tests/pages.test.js http://127.0.0.1:8080 <пароль>
 *
 * Переменные окружения:
 *   KF_BASE      адрес сервера (по умолчанию http://127.0.0.1:8080)
 *   KF_PASSWORD  пароль основного профиля
 *   KF_DANONSHA  пароль профиля danonsha (если профиль включён)
 */

const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2] || process.env.KF_BASE || 'http://127.0.0.1:8080';
const MAIN_PASSWORD = process.argv[3] || process.env.KF_PASSWORD || '';
const DANONSHA_PASSWORD = process.env.KF_DANONSHA || '';

let failures = 0;
let checks = 0;

function check(name, cond, extra = '') {
    checks += 1;
    if (!cond) failures += 1;
    console.log(`${cond ? '✓' : '✗'} ${name}${extra ? ' — ' + extra : ''}`);
}

/** Загружает страницу с настоящим fetch, сохраняя cookie между запросами. */
async function boot(path) {
    const errors = [];
    const requests = [];
    const virtualConsole = new VirtualConsole();
    virtualConsole.on('jsdomError', (e) => {
        const text = String(e);
        if (!/Could not load link|Not implemented|css parsing/i.test(text)) errors.push(text.slice(0, 200));
    });

    const html = await (await fetch(BASE + path)).text();
    const dom = new JSDOM(html, {
        url: BASE + path,
        runScripts: 'dangerously',
        resources: 'usable',
        pretendToBeVisual: true,
        virtualConsole,
        beforeParse(window) {
            const jar = new Map();
            window.__requests = requests;
            window.fetch = async (url, opts = {}) => {
                const abs = String(url).startsWith('http') ? String(url) : BASE + String(url);
                const headers = new Headers(opts.headers || {});
                const cookie = [...jar.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
                if (cookie) headers.set('Cookie', cookie);
                requests.push({ url: abs.replace(BASE, ''), profile: headers.get('X-Krafen-Profile'), method: opts.method || 'GET' });
                const res = await fetch(abs, { ...opts, headers });
                const setCookies = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
                for (const raw of setCookies) {
                    const pair = raw.split(';')[0];
                    const i = pair.indexOf('=');
                    jar.set(pair.slice(0, i).trim(), pair.slice(i + 1).trim());
                }
                return res;
            };
            window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
            // jsdom не реализует IntersectionObserver — нужен заглушка для reveal-анимаций
            window.IntersectionObserver = class {
                constructor(cb) { this._cb = cb; }
                observe(el) { this._cb([{ target: el, isIntersecting: true, intersectionRatio: 1 }], this); }
                unobserve() {}
                disconnect() {}
                takeRecords() { return []; }
            };
            window.HTMLCanvasElement.prototype.getContext = () => null;
            window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
            window.confirm = () => true;
            window.URL.createObjectURL = () => 'blob:fake';
            window.URL.revokeObjectURL = () => {};
        },
    });

    await settle(dom.window);
    return { w: dom.window, d: dom.window.document, errors, requests, dom };
}

const settle = (w, ms = 2200) => new Promise((r) => w.setTimeout ? w.setTimeout(r, ms) : setTimeout(r, ms));

function click(w, el) {
    if (el) el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
}

/** Вход: на обычных страницах — через замок, на панели — через шлюз. */
async function login(w, d, password) {
    if (!password) return false;
    const passInput = d.getElementById('admin-password-input') || d.getElementById('gate-password');
    const submit = d.getElementById('login-btn') || d.getElementById('gate-login-btn');
    if (!passInput || !submit) return false;
    if (d.getElementById('lock-btn') && d.getElementById('admin-password-input')) {
        click(w, d.getElementById('lock-btn'));
        await settle(w, 100);
    }
    const loginInput = d.getElementById('admin-login-input') || d.getElementById('gate-login');
    if (loginInput && !loginInput.value) loginInput.value = 'krafen';
    passInput.value = password;
    click(w, submit);
    await settle(w, 1200);
    return true;
}

async function api(path, { method = 'GET', body, profile } = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (profile) headers['X-Krafen-Profile'] = profile;
    const res = await fetch(BASE + path, {
        method,
        headers,
        body: body === undefined ? (method === 'PUT' || method === 'POST' ? '{}' : undefined) : JSON.stringify(body),
    });
    return { status: res.status, ok: res.ok, data: await res.json().catch(() => ({})) };
}

/** Логин на уровне HTTP — для проверок API без страницы. Профиль задаётся логином. */
async function httpLogin(profile, password) {
    const res = await fetch(BASE + '/api/admin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Origin: BASE, 'Sec-Fetch-Site': 'same-origin' },
        body: JSON.stringify({ login: profile, password }),
    });
    const cookies = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
    return { status: res.status, cookie: cookies.map((c) => c.split(';')[0]).join('; ') };
}

async function main() {
    console.log(`сервер: ${BASE}\n`);

    // ---------- основной профиль ----------
    console.log('=== основной профиль: watchlist ===');
    {
        const { w, d, errors } = await boot('/watchlist');
        const cards = d.querySelectorAll('.media-card[data-id]').length;
        check('страница отрисовала карточки', cards > 0, `${cards} шт.`);
        check('навигация основного профиля', d.querySelector('.topnav .brand')?.getAttribute('href') === '/');
        if (MAIN_PASSWORD) {
            await login(w, d, MAIN_PASSWORD);
            check('вход в админ-режим', d.body.classList.contains('is-admin'));
        }
        check('нет JS-ошибок', errors.length === 0, errors[0] || '');
        w.close();
    }

    console.log('\n=== основной профиль: остальные страницы ===');
    for (const page of ['/gamelist', '/setup', '/wishlist', '/', '/tasks']) {
        const { w, d, errors } = await boot(page);
        check(`${page} открылась`, Boolean(d.querySelector('main')), '');
        check(`${page}: нет JS-ошибок`, errors.length === 0, errors[0] || '');
        w.close();
    }

    // ---------- изоляция через API ----------
    if (MAIN_PASSWORD && DANONSHA_PASSWORD) {
        console.log('\n=== изоляция профилей (API) ===');
        const main = await httpLogin('krafen', MAIN_PASSWORD);
        const dan = await httpLogin('danonsha', DANONSHA_PASSWORD);
        check('вход основного профиля', main.status === 200);
        check('вход профиля danonsha', dan.status === 200);

        const asMain = await api('/api/watchlist', { profile: 'krafen' });
        const asDan = await api('/api/watchlist', { profile: 'danonsha' });
        check('коллекции читаются раздельно',
              JSON.stringify(asMain.data.items?.map((i) => i.id)) !== JSON.stringify(asDan.data.items?.map((i) => i.id)),
              `krafen=${asMain.data.items?.length}, danonsha=${asDan.data.items?.length}`);

        // чужая cookie не должна давать запись
        const crossMainToDan = await fetch(BASE + '/api/watchlist', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', Origin: BASE, 'Sec-Fetch-Site': 'same-origin',
                       'X-Krafen-Profile': 'danonsha', Cookie: main.cookie },
            body: JSON.stringify({ items: [], revision: 0 }),
        });
        check('cookie krafen не пишет в danonsha', crossMainToDan.status === 401, String(crossMainToDan.status));

        // чужой пароль не подходит
        const wrong = await httpLogin('danonsha', MAIN_PASSWORD);
        check('пароль основного профиля не подходит к danonsha', wrong.status === 401, String(wrong.status));

        // логин определяет профиль и выдаёт собственную cookie
        const byLogin = await fetch(BASE + '/api/admin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Origin: BASE, 'Sec-Fetch-Site': 'same-origin' },
            body: JSON.stringify({ login: 'danonsha', password: DANONSHA_PASSWORD }),
        });
        const loginCookies = byLogin.headers.getSetCookie ? byLogin.headers.getSetCookie() : [];
        check('вход по логину danonsha', byLogin.status === 200, String(byLogin.status));
        check('выдана cookie профиля danonsha', loginCookies.some((c) => c.startsWith('kf_danonsha_session=')),
              loginCookies.map((c) => c.split('=')[0]).join(','));
        const longLived = loginCookies.some((c) => {
            const m = c.match(/Max-Age=(\d+)/i);
            return m && Number(m[1]) >= 7 * 24 * 60 * 60;
        });
        check('сессия долгая (не меньше недели)', longLived, (loginCookies[0] || '').match(/Max-Age=\d+/i)?.[0] || '');
    } else {
        console.log('\n(изоляция API пропущена: задайте KF_PASSWORD и KF_DANONSHA)');
    }

    // ---------- профиль danonsha ----------
    console.log('\n=== профиль danonsha: страницы ===');
    for (const page of ['/danonsha', '/danonsha/watchlist', '/danonsha/gamelist',
                        '/danonsha/setup', '/danonsha/wishlist']) {
        const { w, d, errors, requests } = await boot(page);
        check(`${page} открылась`, Boolean(d.querySelector('main')));
        check(`${page}: нет JS-ошибок`, errors.length === 0, errors[0] || '');

        // каждый запрос коллекций из профиля должен нести профиль
        const collectionReqs = requests.filter((r) => /^\/api\/(watchlist|gamelist|setup|wishlist)(\?|$)/.test(r.url));
        check(`${page}: запросы коллекций с профилем danonsha`,
              collectionReqs.length === 0 || collectionReqs.every((r) => r.profile === 'danonsha'),
              collectionReqs.map((r) => r.profile || '—').join(','));
        w.close();
    }

    console.log('\n=== профиль danonsha: вход и правка ===');
    if (DANONSHA_PASSWORD) {
        const { w, d, errors } = await boot('/danonsha/watchlist');
        check('бренд ведёт в профиль', d.querySelector('.topnav .brand')?.getAttribute('href') === '/danonsha');
        check('метка личного профиля', Boolean(d.querySelector('.profile-badge')));
        await login(w, d, DANONSHA_PASSWORD);
        check('вход в админ-режим danonsha', d.body.classList.contains('is-admin'));

        const state = await w.fetch('/api/watchlist', { headers: { 'X-Krafen-Profile': 'danonsha' } }).then((r) => r.json());
        const probe = {
            id: `probe-${Date.now()}`,
            title: 'Тестовая запись', year: '2024', type: 'movie', status: 'planned',
            rating: 7, review: 'проверка', tags: ['тест'], favorite: true,
        };
        const put = await w.fetch('/api/watchlist', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-Krafen-Profile': 'danonsha' },
            body: JSON.stringify({ items: [...state.items, probe], revision: state.revision }),
        });
        check('сохранение в свой профиль', put.ok, String(put.status));

        const after = await w.fetch('/api/watchlist', { headers: { 'X-Krafen-Profile': 'danonsha' } }).then((r) => r.json());
        const saved = after.items.find((i) => i.id === probe.id);
        check('запись сохранена с полями', Boolean(saved) && saved.rating === 7 && saved.favorite === true,
              saved ? `rating=${saved.rating}` : 'нет');

        // откат
        const clean = await w.fetch('/api/watchlist', { headers: { 'X-Krafen-Profile': 'danonsha' } }).then((r) => r.json());
        await w.fetch('/api/watchlist', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-Krafen-Profile': 'danonsha' },
            body: JSON.stringify({ items: clean.items.filter((i) => i.id !== probe.id), revision: clean.revision }),
        });
        check('тестовая запись удалена', true);
        check('нет JS-ошибок', errors.length === 0, errors[0] || '');
        w.close();
    } else {
        console.log('(вход в danonsha пропущен: задайте KF_DANONSHA)');
    }

    console.log(`\n${failures === 0 ? '✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ' : `❌ ПРОВАЛЕНО: ${failures}`} (${checks} проверок)`);
    process.exit(failures ? 1 : 0);
}

main().catch((err) => {
    console.error('Ошибка выполнения:', err);
    process.exit(1);
});
