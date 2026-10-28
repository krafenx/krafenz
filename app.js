/* app.js */

// 1. Атмосфера (Дождь)
function initRain() {
    const canvas = document.createElement('canvas');
    canvas.style.cssText = 'position:fixed; inset:0; z-index:-1; pointer-events:none; opacity:0.3;';
    document.body.prepend(canvas);
    const ctx = canvas.getContext('2d');
    let W, H, drops = [];
    
    function resize() {
        W = canvas.width = window.innerWidth;
        H = canvas.height = window.innerHeight;
        drops = Array.from({ length: Math.floor(W / 10) }, () => ({
            x: Math.random() * W, y: Math.random() * H, l: 10 + Math.random() * 20, v: 5 + Math.random() * 10
        }));
    }
    
    window.addEventListener('resize', resize);
    resize();
    
    function loop() {
        ctx.clearRect(0, 0, W, H);
        ctx.strokeStyle = '#9aa4b8';
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.3;
        for (const d of drops) {
            ctx.beginPath();
            ctx.moveTo(d.x, d.y);
            ctx.lineTo(d.x - 1, d.y + d.l);
            ctx.stroke();
            d.y += d.v;
            d.x -= 0.5;
            if (d.y > H) { d.y = -20; d.x = Math.random() * W; }
        }
        requestAnimationFrame(loop);
    }
    loop();
}

// 2. Виджет Twitch
async function initTwitch(login) {
    const card = document.getElementById('twitch-card');
    const statusBadge = document.getElementById('twitch-status');
    if (!card) return;

    try {
        const res = await fetch(`/api/twitch?login=${login}`);
        const data = await res.json();
        
        if (data.live) {
            statusBadge.textContent = 'LIVE';
            statusBadge.style.color = '#22c55e';
            statusBadge.style.borderColor = '#22c55e';
            card.innerHTML = `
                <p style="font-size: 13px; font-weight: 500; color: #fff; margin-bottom: 10px;">${data.stream.title}</p>
                <p style="font-size: 11px; color: var(--silver);">${data.stream.gameName} · ${data.stream.viewerCount} зрителей</p>
                <a href="${data.url}" target="_blank" class="btn btn-primary" style="margin-top: 15px; width: 100%;">Смотреть</a>
            `;
        } else {
            statusBadge.textContent = 'OFFLINE';
            card.innerHTML = `<p style="font-size: 12px; color: var(--silver-dim);">Канал сейчас не в сети.</p>`;
        }
    } catch (e) {
        card.innerHTML = `<p style="font-size: 12px; color: #fca5a5;">Ошибка API</p>`;
    }
}

// 3. Виджет Музыки (Spotify/Last.fm)
async function initMusic() {
    const trackName = document.getElementById('track-name');
    const trackArtist = document.getElementById('track-artist');
    const artContainer = document.getElementById('music-art');
    if (!trackName) return;

    try {
        let res = await fetch('/api/spotify');
        let data = res.ok ? await res.json() : null;
        
        if (!data || !data.track) {
            res = await fetch('/api/lastfm');
            data = res.ok ? await res.json() : null;
        }

        if (data && data.track) {
            trackName.textContent = data.track.name;
            trackArtist.textContent = data.track.artist;
            if (data.track.image) {
                artContainer.innerHTML = `<img src="${data.track.image}" style="width:100%; height:100%; object-fit:cover; border-radius:12px;">`;
            }
        } else {
            trackName.textContent = 'Ничего не играет';
            trackArtist.textContent = 'Spotify / Last.fm';
        }
    } catch (e) {
        trackName.textContent = 'Ошибка загрузки';
    }
}

// 4. Инициализация
document.addEventListener('DOMContentLoaded', () => {
    initRain();
});