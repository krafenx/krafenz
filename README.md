# Krafen

**Krafen is my personal website and collection dashboard.** It brings together profile information, links, live service status, and a few personal lists in one place. It is not a reusable framework or a general-purpose website builder.

[Live site](https://krafen.me/)

## What is here

- `index.html` — personal homepage with Discord, Twitch, and music widgets.
- `watchlist.html` — anime, film, and series list, with TMDB search.
- `gamelist.html` — game list, with IGDB search.
- `setup.html` — PC parts and gear list.
- `wishlist.html` — shopping wishlist.
- `tasks.html` — task page.
- `danonsha.html` — a separate profile page.
- `worker.js` — Cloudflare Worker API for the lists, admin session, and external services.

The pages use plain HTML, CSS, and browser JavaScript. Tailwind CSS and JetBrains Mono are loaded from external CDNs. The backend uses Cloudflare Workers and a Workers KV namespace. There is no React or Next.js application, `package.json`, or `npm run dev` command.

## Services

The homepage reads public Discord and Twitch data. Music is read from Spotify when its Web API is configured; otherwise it falls back to Last.fm. A real playback timeline is only shown when Spotify provides the current track position and duration. Last.fm does not provide that playback progress, so the fallback intentionally has no timeline.

The list pages store data in Cloudflare KV. Editing is protected by the site's admin session. API credentials are configured as Cloudflare secrets and are not intended to be placed in browser code.

## Deployment notes

This site is configured for its existing Cloudflare Worker in `wrangler.jsonc`. Deployment depends on that Cloudflare setup, including its KV namespace, rate-limit bindings, and secrets. See:

- [Cloudflare Worker configuration](WATCHLIST_SETUP.md)
- [Spotify widget configuration](SPOTIFY_SETUP.md)

The configuration contains identifiers for the existing deployment. It is not a self-service template; deployment elsewhere would require separate Cloudflare resources and configuration.

## Rights and permissions

This is a personal project, not an open-source release. All rights to project materials owned by the author are reserved. No permission is granted to copy, modify, redistribute, or use those materials without prior written permission. Third-party materials remain subject to their respective terms.

To request permission, contact [@krafen on Telegram](https://t.me/krafen).

See [LICENSE](LICENSE). Public visibility on a code-hosting service may still allow viewing or forking through that service; repository visibility must be changed to private to restrict access there.

---

# Krafen (русский)

**Krafen — мой личный сайт и набор страниц для собственных списков.** Здесь собраны профиль, ссылки, статусы сервисов и несколько личных подборок. Это не универсальный конструктор сайтов и не переиспользуемый фреймворк.

[Открыть сайт](https://krafen.me/)

## Что входит в проект

- `index.html` — главная страница с виджетами Discord, Twitch и музыки.
- `watchlist.html` — список аниме, фильмов и сериалов с поиском через TMDB.
- `gamelist.html` — список игр с поиском через IGDB.
- `setup.html` — список комплектующих и техники.
- `wishlist.html` — список желаемых покупок.
- `tasks.html` — страница задач.
- `danonsha.html` — отдельная профильная страница.
- `worker.js` — API на Cloudflare Worker: списки, вход администратора и внешние сервисы.

Страницы написаны на обычных HTML, CSS и JavaScript. Tailwind CSS и шрифт JetBrains Mono загружаются с внешних CDN. Серверная часть работает на Cloudflare Workers и Workers KV. В проекте нет приложения на React или Next.js, файла `package.json` и команды `npm run dev`.

## Сервисы

Главная получает публичные данные Discord и Twitch. Для музыки используется Spotify, если настроен доступ к Web API; иначе виджет обращается к Last.fm. Настоящий таймлайн отображается только когда Spotify возвращает позицию и длительность текущего трека. Last.fm не отдаёт прогресс воспроизведения, поэтому в запасном режиме таймлайна нет.

Данные страниц со списками хранятся в Cloudflare KV. Редактирование доступно через защищённую сессию администратора. API-секреты задаются в Cloudflare и не должны помещаться в код браузера.

## Развёртывание

`wrangler.jsonc` настроен для существующего Cloudflare Worker. Для развёртывания нужны соответствующие ресурсы Cloudflare: KV namespace, rate limit bindings и секреты. Инструкции:

- [Настройка Cloudflare Worker](WATCHLIST_SETUP.md)
- [Настройка музыкального виджета Spotify](SPOTIFY_SETUP.md)

Конфигурация содержит идентификаторы существующего развёртывания. Это не готовый шаблон для самостоятельного форка: для другого окружения потребуются отдельные ресурсы Cloudflare и собственная конфигурация.

## Права и разрешения

Это личный проект, не выпускаемый как open source. Все права на материалы проекта, принадлежащие автору, защищены. Без предварительного письменного разрешения нельзя копировать, изменять, распространять или использовать эти материалы. На сторонние материалы распространяются условия их правообладателей.

Чтобы запросить разрешение, напиши [@krafen в Telegram](https://t.me/krafen).

Подробности — в файле [LICENSE](LICENSE). Если репозиторий публичный, правила хостинга всё ещё могут разрешать просмотр и форк. Чтобы ограничить к нему доступ, нужно сделать репозиторий приватным.
