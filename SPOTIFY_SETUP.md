# Подключение Spotify на сервере `server.py`

Виджет музыки на главной странице обращается к `/api/spotify`, а при отсутствии подключения автоматически переключается на `/api/lastfm`. Когда подключён Spotify, на главной отображается не только трек и обложка, но и живая шкала времени.

## 1. Приложение в Spotify Developer Dashboard

1. Откройте [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) и создайте приложение.
2. Скопируйте **Client ID** и **Client Secret**.
3. В настройках приложения добавьте **Redirect URI**:
   - Для продакшена: `https://krafen.me/api/spotify/callback`
   - Для локальной проверки: `http://127.0.0.1:8080/api/spotify/callback`

*(Адрес формируется автоматически из переменной `PUBLIC_ORIGIN` или заголовка запроса `/api/spotify/callback`.)*

## 2. Переменные в `.env`

Добавьте в `/opt/krafen/.env` (или локальный `.env`):

```dotenv
PUBLIC_ORIGIN=https://krafen.me
SPOTIFY_CLIENT_ID=ваш_client_id
SPOTIFY_CLIENT_SECRET=ваш_client_secret
```

Перезапустите сервер (`sudo systemctl restart krafen.service`).

## 3. Однократная привязка аккаунта

1. Откройте `/watchlist`, нажмите на иконку замка в нижнем доке и войдите под паролем `ADMIN_PASSWORD`.
2. В верхней панели администратора нажмите кнопку **«подключить Spotify»** (переход на `/api/spotify/auth`).
3. Подтвердите доступ в окне Spotify — сервер сохранит `refresh_token` в `data/krafen.db` и перенаправит обратно на главную страницу с уведомлением `Spotify успешно подключён`.

После этого `server.py` будет сам обновлять краткоживущий `access_token` в памяти и отдавать текущий трек через `GET /api/spotify`.
