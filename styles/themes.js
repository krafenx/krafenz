const themes = [
    { id: "nocturne", name: "Ноктюрн", swatch: ["#050505", "#7c0a0a"] },
    { id: "crimson", name: "Багровый", swatch: ["#050505", "#a30f0f"] },
    { id: "silver", name: "Серебро", swatch: ["#050505", "#8f8f99"] },
];
const THEME_KEY = "krafen.theme";
const ATMO_KEY = "krafen.atmo";

const validThemes = new Set(themes.map((theme) => theme.id));

function readTheme() {
    try {
        const value = localStorage.getItem(THEME_KEY);
        return value && validThemes.has(value) ? value : themes[0].id;
    } catch {
        return themes[0].id;
    }
}

function applyTheme(themeId) {
    const nextTheme = validThemes.has(themeId) ? themeId : themes[0].id;
    if (document.documentElement.dataset.theme !== nextTheme) {
        document.documentElement.dataset.theme = nextTheme;
    }
    try {
        localStorage.setItem(THEME_KEY, nextTheme);
    } catch { }
}

function createThemeSwitcher() {
    const host = document.createElement("div");
    host.className = "theme-switcher";
    host.innerHTML = `
        <button type="button" class="theme-switcher__trigger" aria-haspopup="menu" aria-expanded="false">
            <span aria-hidden="true">◐</span>
            <span class="theme-switcher__label">тема</span>
        </button>
        <div class="theme-switcher__panel" role="menu" hidden></div>
    `;

    const trigger = host.querySelector(".theme-switcher__trigger");
    const label = host.querySelector(".theme-switcher__label");
    const panel = host.querySelector(".theme-switcher__panel");

    function closePanel() {
        panel.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        document.removeEventListener("click", handleOutsideClick, true);
    }

    function openPanel() {
        panel.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        document.addEventListener("click", handleOutsideClick, true);
    }

    function handleOutsideClick(event) {
        if (!host.contains(event.target)) {
            closePanel();
        }
    }

    function renderOptions() {
        const active = readTheme();
        label.textContent = themes.find((theme) => theme.id === active)?.name || "тема";
        panel.innerHTML = "";
        themes.forEach((theme) => {
            const option = document.createElement("button");
            option.type = "button";
            option.className = "theme-switcher__option";
            option.setAttribute("role", "menuitemradio");
            option.setAttribute("aria-checked", String(theme.id === active));

            const swatch = document.createElement("span");
            swatch.className = "theme-switcher__swatch";
            swatch.style.background = `linear-gradient(135deg, ${theme.swatch[0]} 0 45%, ${theme.swatch[1]} 55% 100%)`;
            swatch.setAttribute("aria-hidden", "true");

            const name = document.createElement("span");
            name.className = "theme-switcher__option-name";
            name.textContent = theme.name;

            option.append(swatch, name);
            if (theme.id === active) {
                const check = document.createElement("span");
                check.className = "theme-switcher__option-check";
                check.textContent = "✓";
                option.appendChild(check);
            }

            option.addEventListener("click", () => {
                applyTheme(theme.id);
                renderOptions();
                closePanel();
            });

            panel.appendChild(option);
        });
    }

    trigger.addEventListener("click", () => {
        const isOpen = !panel.hidden;
        if (isOpen) {
            closePanel();
        } else {
            renderOptions();
            openPanel();
        }
    });

    return host;
}

/* ---------- атмосфера: дождь, туман, молнии, виньетка, зерно ---------- */

function atmoOn() {
    try { return localStorage.getItem(ATMO_KEY) !== "0"; } catch { return true; }
}

function setAtmo(on) {
    document.body.classList.toggle("kf-calm", !on);
    try { localStorage.setItem(ATMO_KEY, on ? "1" : "0"); } catch { }
}

function injectAtmosphere() {
    // слои
    const layers = document.createElement("div");
    layers.innerHTML =
        '<canvas id="kf-rain"></canvas>' +
        '<div class="kf-fog"></div><div class="kf-fog f2"></div>' +
        '<div id="kf-flash"></div>' +
        '<div class="kf-vignette"></div><div class="kf-grain"></div>';
    document.body.prepend(layers);

    // дождь
    const cv = document.getElementById("kf-rain");
    const ctx = cv.getContext("2d");
    let W, H, drops = [];
    function spawn(any) {
        return {
            x: Math.random() * W, y: any ? Math.random() * H : -20,
            l: 10 + Math.random() * 18, v: 9 + Math.random() * 9, o: 0.06 + Math.random() * 0.2
        };
    }
    function resize() {
        W = cv.width = innerWidth; H = cv.height = innerHeight;
        drops = Array.from({ length: Math.floor(W / 6) }, () => spawn(true));
    }
    addEventListener("resize", resize); resize();
    (function loop() {
        ctx.clearRect(0, 0, W, H);
        ctx.strokeStyle = "#9aa4b8"; ctx.lineWidth = 1;
        for (const d of drops) {
            ctx.globalAlpha = d.o;
            ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(d.x - 1.5, d.y + d.l); ctx.stroke();
            d.y += d.v; d.x -= 0.6;
            if (d.y > H) Object.assign(d, spawn(false));
        }
        requestAnimationFrame(loop);
    })();

    // молнии
    const flash = document.getElementById("kf-flash");
    function lightning() {
        if (!document.body.classList.contains("kf-calm")) {
            const bursts = 1 + Math.floor(Math.random() * 3);
            let n = 0;
            (function flick() {
                flash.style.transition = "none";
                flash.style.opacity = 0.18 + Math.random() * 0.3;
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    flash.style.transition = "opacity .6s ease-out";
                    flash.style.opacity = 0;
                }));
                if (++n < bursts) setTimeout(flick, 90 + Math.random() * 140);
            })();
        }
        setTimeout(lightning, 6000 + Math.random() * 14000);
    }
    setTimeout(lightning, 2500);
}

function createAtmoToggle() {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-switcher__trigger";
    btn.style.marginRight = "0.5rem";
    btn.innerHTML = '<span aria-hidden="true">☂</span><span class="theme-switcher__label">атмосфера</span>';
    function sync() { btn.style.opacity = atmoOn() ? "1" : ".55"; }
    btn.addEventListener("click", () => { setAtmo(!atmoOn()); sync(); });
    sync();
    return btn;
}

document.addEventListener("DOMContentLoaded", () => {
    applyTheme(readTheme());
    document.body.classList.toggle("kf-calm", !atmoOn());
    injectAtmosphere();

    const switcher = createThemeSwitcher();
    switcher.querySelector(".theme-switcher__trigger").before(createAtmoToggle());
    document.body.appendChild(switcher);
});
