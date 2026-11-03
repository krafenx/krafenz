-- Krafen SQLite database seed (watchlist, gamelist, setup, wishlist, drive_vods)
-- Коллекции принадлежат профилям (owner): основной krafen и личный danonsha.
BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS collection_meta (
    owner TEXT NOT NULL DEFAULT 'krafen',
    name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (owner, name)
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    title_ru TEXT,
    year TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    poster TEXT,
    rating INTEGER NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS gamelist_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    igdb_id INTEGER,
    title TEXT NOT NULL,
    year TEXT NOT NULL,
    status TEXT NOT NULL,
    rating REAL NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT '',
    cover TEXT,
    genres_json TEXT NOT NULL DEFAULT '[]',
    platforms_json TEXT NOT NULL DEFAULT '[]',
    category INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS setup_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    item_group TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT 'other',
    note TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS wishlist_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 2,
    price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'RUB',
    note TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS drive_vods (
    id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    size_label TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    sort_key TEXT NOT NULL,
    folder_drive_id TEXT NOT NULL DEFAULT '',
    chat_drive_id TEXT NOT NULL DEFAULT '',
    thumb_drive_id TEXT NOT NULL DEFAULT '',
    meta_drive_id TEXT NOT NULL DEFAULT '',
    game TEXT NOT NULL DEFAULT '',
    duration_s INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT ''
);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('danonsha', 'gamelist', 0, 1793663760);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('danonsha', 'setup', 0, 1793663760);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('danonsha', 'watchlist', 0, 1793663760);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('danonsha', 'wishlist', 0, 1793663760);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('krafen', 'gamelist', 10, 1793659103);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('krafen', 'setup', 17, 1793659050);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('krafen', 'watchlist', 73, 1793659098);
INSERT OR REPLACE INTO "collection_meta" (owner, name, revision, updated_at) VALUES('krafen', 'wishlist', 16, 1793659108);

INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 0, 'tmdb_movie_388', 'Inside Man', 'Не пойман — не вор', '2006', 'movie', 'watched', 'https://image.tmdb.org/t/p/w342/8ZcIjP53vaFeW416SMNr0d5kTYY.jpg', 0, '', '[]', 0, 1792771680713, 1792771680713);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 1, 'sh_61990', 'Cyberpunk: Edgerunners 2', 'Киберпанк: Бегущие по краю 2', '2026', 'anime', 'planned', 'https://images.weserv.nl/?url=https://a.storyblok.com/f/178900/1064x1330/0422d014b6/cyberpunk-edgerunners-2-visual.jpg', 0, '', '[]', 0, 1792185453678, 1792185453678);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 2, 'tmdb_movie_145135', 'Dark Skies', 'Мрачные небеса', '2013', 'movie', 'dropped', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/akS37q4dSZEO4dcorVuXZY00alP.jpg', 0, '', '[]', 0, 1791738987413, 1793657297948);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 3, 'tmdb_movie_204922', 'Before I Go to Sleep', 'Прежде чем я усну', '2014', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BNjI0NTAwMTU1NV5BMl5BanBnXkFtZTgwNzkwNjQ0MjE@._V1_.jpg', 0, '', '[]', 0, 1791733724683, 1791733724683);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 4, 'tmdb_movie_854', 'The Mask', 'Маска', '1994', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BNGNmNjI0ZmMtMzI5MC00ZjUyLWFlZDEtYjUyMGZlN2E3N2E2XkEyXkFqcGc@._V1_FMjpg_UX1000_.jpg', 0, '', '[]', 0, 1791305839507, 1791305839507);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 5, 'tmdb_movie_390497', '터널', 'Тоннель', '2016', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMjE3ODYwODE2OV5BMl5BanBnXkFtZTgwMjM5ODA2OTE@._V1_.jpg', 0, '', '[]', 0, 1790535359343, 1790535359343);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 6, 'tmdb_movie_972533', 'Last Breath', 'Дыхание шторма', '2025', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BYmNjMDg1Y2EtNmZiOS00NGUzLThjZGYtNzU2OGI5M2VkMDFhXkEyXkFqcGc@._V1_.jpg', 0, '', '[]', 0, 1790439519819, 1790439519819);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 7, 'tmdb_movie_48340', 'Sanctum', 'Санктум', '2011', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMTM4MjY1NTEyNV5BMl5BanBnXkFtZTcwMTc3MjcxNA@@._V1_FMjpg_UX1000_.jpg', 0, '', '[]', 0, 1789846745870, 1789846745870);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 8, 'tmdb_movie_174733', 'Метро', NULL, '2013', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BODAzMjY2MzMtNThiMy00OTk0LWI4NWUtMjNlZjU2ZjIwMzZhXkEyXkFqcGc@._V1_FMjpg_UX1000_.jpg', 0, '', '[]', 0, 1789667574495, 1789667574495);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 9, 'tmdb_movie_14161', '2012', NULL, '2009', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMTY0MjEyODQzMF5BMl5BanBnXkFtZTcwMTczMjQ4Mg@@._V1_.jpg', 0, '', '[]', 0, 1789493243006, 1789493243006);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 10, 'tmdb_movie_11774', 'Lemony Snicket''s A Series of Unfortunate Events', 'Лемони Сникет: 33 несчастья', '2004', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMjE3MDM4NTg0NV5BMl5BanBnXkFtZTcwNjI4MTczMw@@._V1_.jpg', 0, '', '[]', 0, 1789303523158, 1789303523158);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 11, 'tmdb_movie_1738', 'Next', 'Пророк', '2007', 'movie', 'watched', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/ltFhhcsOXPDZcFt8zcaAFmg4DcI.jpg', 0, '', '[]', 0, 1788705977246, 1788705977246);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 12, 'tmdb_movie_60935', 'The Thing', 'Нечто', '2011', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMTMxMjI0MzUyNl5BMl5BanBnXkFtZTcwNjc1NzE5NQ@@._V1_.jpg', 0, '', '[]', 0, 1786294476266, 1786294476266);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 13, 'tmdb_movie_158011', 'The Call', 'Тревожный вызов', '2013', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMjExNDkzNjAwOV5BMl5BanBnXkFtZTcwMDMzMzQwOQ@@._V1_.jpg', 0, '', '[]', 0, 1786227837963, 1786227837963);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 14, 'tmdb_movie_7183', 'Perfect Stranger', 'Идеальный незнакомец', '2007', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMTY4ODUwMTk3OV5BMl5BanBnXkFtZTcwNTI1MTEzMw@@._V1_.jpg', 0, '', '[]', 0, 1786227828515, 1786227828515);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 15, 'tmdb_movie_1339713', 'Obsession', 'Обсессия', '2026', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BYzc1NWUwMDgtNGZlMS00ZmYzLWIzMzktNmMxMmY1MTUzNWExXkEyXkFqcGc@._V1_FMjpg_UX1000_.jpg', 0, '', '[]', 0, 1785852983546, 1785852983546);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 16, 'tmdb_movie_392044', 'Murder on the Orient Express', 'Убийство в «Восточном экспрессе»', '2017', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BNTlkYjBlZTctZjg2YS00NDMzLTlkMTMtNWExMGYyOTliMmEzXkEyXkFqcGc@._V1_FMjpg_UX1000_.jpg', 0, '', '[]', 0, 1785684013950, 1785684013950);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 17, 'tmdb_movie_774079', 'Happy Death Day to Us', 'Счастливого дня смерти 3', '-', 'movie', 'planned', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/ue5wV2XLP6KRsFLX7ak14DyqT87.jpg', 0, '', '[]', 0, 1784827435835, 1784827435835);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 18, 'tmdb_movie_512196', 'Happy Death Day 2U', 'Счастливого нового дня смерти', '2019', 'movie', 'watched', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/ZstlucYRCRfkZ74O2LUfbbNbyb.jpg', 0, '', '[]', 0, 1784827433356, 1785090665344);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 19, 'tmdb_movie_440021', 'Happy Death Day', 'Счастливого дня смерти', '2017', 'movie', 'watched', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/gxOqCjZ3YGxZpkKchQbHX3DeQ3V.jpg', 0, '', '[]', 0, 1784827431284, 1784827431284);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 20, 'sh_42310', 'Cyberpunk: Edgerunners', 'Киберпанк: Бегущие по краю', '2022', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/42310.jpg', 0, '', '[]', 0, 1783288715334, 1783288715334);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 21, 'sh_58567', 'Ore dake Level Up na Ken Season 2: Arise from the Shadow', 'Поднятие уровня в одиночку 2: Восстаньте из тени', '2025', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/58567.jpg', 0, '', '[]', 0, 1783014157106, 1783103592686);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 22, 'sh_52299', 'Ore dake Level Up na Ken', 'Поднятие уровня в одиночку', '2024', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/52299.jpg', 0, '', '[]', 0, 1779825110973, 1783014135122);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 23, 'sh_54595', 'Kage no Jitsuryokusha ni Naritakute! 2nd Season', 'Восхождение в тени! 2', '2023', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/54595.jpg', 0, '', '[]', 0, 1779824427687, 1780524285930);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 24, 'sh_56876', 'Otonari no Tenshi-sama ni Itsunomanika Dame Ningen ni Sareteita Ken 2', 'Ангел по соседству 2', '2026', 'anime', 'planned', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/56876.jpg', 0, '', '[]', 0, 1779616060781, 1779616060781);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 25, 'sh_50739', 'Otonari no Tenshi-sama ni Itsunomanika Dame Ningen ni Sareteita Ken', 'Ангел по соседству', '2023', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/50739.jpg', 0, '', '[]', 0, 1779616059333, 1779647502982);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 26, 'sh_54744', 'Tokidoki Bosotto Russia-go de Dereru Tonari no Alya-san', 'Аля иногда кокетничает со мной по-русски', '2024', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/54744.jpg', 0, '', '[]', 0, 1779381320920, 1779381320920);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 27, 'tmdb_movie_136797', 'Need for Speed', 'Need for Speed: Жажда Скорости', '2014', 'movie', 'watched', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/rc9Fd0WGaJXuQr5ryIpS3H88C5j.jpg', 0, '', '[]', 0, 1779227552597, 1779227552597);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 28, 'sh_48316', 'Kage no Jitsuryokusha ni Naritakute!', 'Восхождение в тени!', '2022', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/48316.jpg', 0, '', '[]', 0, 1779227428113, 1779824429958);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 29, 'tmdb_movie_336843', 'Maze Runner: The Death Cure', 'Бегущий в лабиринте: Лекарство от смерти', '2018', 'movie', 'watched', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/6ps20wVKQghAx0KBTQvPcLPdLlc.jpg', 0, '', '[]', 0, 1778340789215, 1787929354812);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 30, 'tmdb_movie_294254', 'Maze Runner: The Scorch Trials', 'Бегущий в лабиринте: Испытание огнём', '2015', 'movie', 'watched', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/tA3uIM8dnZqwJnFyR9A7zLE5Wd1.jpg', 0, '', '[]', 0, 1778340788423, 1787922593879);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 31, 'tmdb_movie_198663', 'The Maze Runner', 'Бегущий в лабиринте', '2014', 'movie', 'watched', 'https://images.weserv.nl/?url=image.tmdb.org/t/p/w342/fvy2LrxQzSCpUuGaB9jK5z1NeA6.jpg', 0, '', '[]', 0, 1778340785160, 1778340785160);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 32, 'sh_38000', 'Kimetsu no Yaiba', 'Клинок, рассекающий демонов', '2019', 'anime', 'watched', 'https://images.weserv.nl/?url=shikimori.one/system/animes/original/38000.jpg', 0, '', '[]', 0, 1777437058160, 1777437058160);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 33, 'tt27114092', 'Major Grom: The Game', NULL, '2024', 'movie', 'watched', 'https://m.media-amazon.com/images/M/MV5BMTFkYjNkYTYtYzI5My00ZTUzLWIyM2EtMWJkNzg4NTA1NDc4XkEyXkFqcGc@._V1_SX300.jpg', 0, '', '[]', 0, 1777374800267, 1777374800267);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 34, 'sh_39535', 'Mushoku Tensei: Isekai Ittara Honki Dasu', 'Реинкарнация безработного: История о приключениях в другом мире', '2021', 'anime', 'planned', 'https://m.media-amazon.com/images/M/MV5BYWQwNjk3MDItNDAxMS00YTQ2LWEyNDctMGYyZTE5OGQxNGQ1XkEyXkFqcGc@._V1_.jpg', 0, '', '[]', 0, 1777373134334, 1777373134334);
INSERT OR REPLACE INTO "watchlist_items" ("owner", "pos", "id", "title", "title_ru", "year", "type", "status", "poster", "rating", "review", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 35, 'sh_48736', 'Sono Bisque Doll wa Koi wo Suru', 'Эта фарфоровая кукла влюбилась', '2022', 'anime', 'watched', 'https://m.media-amazon.com/images/M/MV5BYmJhYjgzYTQtMzMxYi00NTkyLWI2MWItNTgxNjk1NzU0YmQzXkEyXkFqcGc@._V1_.jpg', 0, '', '[]', 0, 1777373119487, 1777490938914);

INSERT OR REPLACE INTO "gamelist_items" ("owner", "pos", "id", "igdb_id", "title", "year", "status", "rating", "review", "cover", "genres_json", "platforms_json", "category", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 0, 'igdb_52189', 52189, 'Grand Theft Auto VI', '2026', 'wishlist', 0.0, 'никогда не поиграю в это мой компьютер просто не запустит эту игру ахахах', 'https://images.igdb.com/igdb/image/upload/t_cover_big/co9rwo.jpg', '["Shooter","Racing","Adventure"]', '["Series X|S","PS5"]', 0, '[]', 0, 1779230469361, 1779230525513);
INSERT OR REPLACE INTO "gamelist_items" ("owner", "pos", "id", "igdb_id", "title", "year", "status", "rating", "review", "cover", "genres_json", "platforms_json", "category", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 1, 'igdb_119133', 119133, 'Elden Ring', '2022', 'wishlist', 0.1, '', 'https://images.igdb.com/igdb/image/upload/t_cover_big/co4jni.jpg', '["Role-playing (RPG)","Adventure"]', '["Series X|S","PS4","Switch 2","PC"]', 0, '[]', 0, 1778098755507, 1793657839340);
INSERT OR REPLACE INTO "gamelist_items" ("owner", "pos", "id", "igdb_id", "title", "year", "status", "rating", "review", "cover", "genres_json", "platforms_json", "category", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 2, 'igdb_110248', 110248, 'Stray', '2022', 'wishlist', 0.0, '', 'https://images.igdb.com/igdb/image/upload/t_cover_big/co4tt2.jpg', '["Platform","Adventure","Indie"]', '["Series X|S","PS4","PC","PS5"]', 0, '[]', 0, 1778073924640, 1778073927584);
INSERT OR REPLACE INTO "gamelist_items" ("owner", "pos", "id", "igdb_id", "title", "year", "status", "rating", "review", "cover", "genres_json", "platforms_json", "category", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 3, 'igdb_204350', 204350, 'The Last of Us Part I', '2022', 'wishlist', 0.0, '', 'https://images.igdb.com/igdb/image/upload/t_cover_big/coa1gq.jpg', '["Shooter","Adventure"]', '["PC","PS5"]', 0, '[]', 0, 1778073894073, 1778073896385);
INSERT OR REPLACE INTO "gamelist_items" ("owner", "pos", "id", "igdb_id", "title", "year", "status", "rating", "review", "cover", "genres_json", "platforms_json", "category", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 4, 'igdb_25076', 25076, 'Red Dead Redemption 2', '2018', 'wishlist', 0.0, '', 'https://images.igdb.com/igdb/image/upload/t_cover_big/co1q1f.jpg', '["Shooter","Role-playing (RPG)","Adventure"]', '["Stadia","PS4","PC","XONE"]', 0, '[]', 0, 1778073849818, 1778073852578);
INSERT OR REPLACE INTO "gamelist_items" ("owner", "pos", "id", "igdb_id", "title", "year", "status", "rating", "review", "cover", "genres_json", "platforms_json", "category", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 5, 'igdb_1877', 1877, 'Cyberpunk 2077', '2020', 'wishlist', 0.0, '', 'https://images.igdb.com/igdb/image/upload/t_cover_big/coaih8.jpg', '["Shooter","Role-playing (RPG)","Adventure"]', '["Stadia","Series X|S","PS4","Switch 2"]', 0, '[]', 0, 1778073825827, 1778073829811);
INSERT OR REPLACE INTO "gamelist_items" ("owner", "pos", "id", "igdb_id", "title", "year", "status", "rating", "review", "cover", "genres_json", "platforms_json", "category", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 6, 'igdb_277143', 277143, 'The Last of Us Part II Remastered', '2024', 'wishlist', 0.0, '', 'https://images.igdb.com/igdb/image/upload/t_cover_big/coa1gr.jpg', '["Shooter","Adventure"]', '["PC","PS5"]', 0, '[]', 0, 1778071754516, 1778071766211);

INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 0, 'cpu', 'pc', 'Процессор', 'AMD Ryzen 5 8400F', 'cpu', '', 'https://www.dns-shop.ru/product/7c16c17ff883d582/processor-amd-ryzen-5-8400f-oem/', 1, 1793210776104, 1789937587852);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 1, 'gpu', 'pc', 'Видеокарта', 'Gigabyte NVIDIA GeForce GTX 650', 'gpu', 'затычка, коплю на новую', '', 2, 1793210776104, 1793210776104);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 2, 'ram', 'pc', 'Оперативная память', 'Kingston FURY Beast Black RGB', 'ram', 'DDR5 2x8GB', 'https://www.dns-shop.ru/product/f2d2b90ff075ed20/operativnaa-pamat-kingston-fury-beast-black-rgb-kf552c40bba-8-8-gb/', 3, 1793210776104, 1789937729216);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 3, 'board', 'pc', 'Материнская плата', 'GIGABYTE A620M H', 'board', '(AM5, Micro-ATX)', 'https://www.dns-shop.ru/product/f1e541de40caed20/materinskaa-plata-gigabyte-a620m-h/', 4, 1793210776104, 1789937618198);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 4, 'ssd', 'pc', 'SSD диск', 'Apacer AS350 PANTHER 128 ГБ', 'ssd', 'срочно нужен новый SSD', 'https://www.dns-shop.ru/product/ee3bc22301143332/128-gb-25-sata-nakopitel-apacer-as350-panther-95db260p100c/', 5, 1793210776104, 1789937644747);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 5, 'hdd', 'pc', 'HDD диск', 'Toshiba P300 1 ТБ', 'hdd', '', 'https://www.dns-shop.ru/product/a47ecfb5e894ed20/1-tb-zestkij-disk-toshiba-p300-hdwd110uzsva/', 6, 1793210776104, 1789937756893);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 6, 'cooler', 'pc', 'Охлаждение процессора', 'Lovingcool LC-D400 White', 'cooler', '', 'https://www.ozon.ru/product/kuler-dlya-protsessora-loving-cool-lc-400b-1867950535/', 7, 1793210776104, 1789937824290);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 7, 'psu', 'pc', 'Блок питания', 'Azerty RPA-650BA2 650W', 'psu', '', 'https://www.dns-shop.ru/product/ce16affc4006eadb/blok-pitania-atx-azerty-rpa-650ba2-120-0943-krasnyj-cernyj/', 8, 1793210776104, 1789937743112);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 8, 'case', 'pc', 'Корпус', '1STPLAYER MIKU Mi2', 'case', 'черный', 'https://www.dns-shop.ru/product/ad9a27d693c17cf3/korpus-1stplayer-miku-mi2-mi2-bk-3f1-cernyj/', 9, 1793210776104, 1789937803527);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 9, 'iem', 'gear', 'In-Ear Monitor', 'MOONDROP CHU II', 'audio', '', 'https://www.dns-shop.ru/product/cfd0c2b6a2e8d21a/provodnye-nausniki-moondrop-chu-ii-cernyj-2023/', 1, 1793210776104, 1789937836298);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 10, 'monitor-main', 'gear', 'Монитор', 'BenQ GL2055 20" 75 Гц', 'monitor', '', 'https://www.nix.ru/autocatalog/lcd_benq/20-ZHK-monitor-BenQ-GL2055-Black-LCD-1600x900-D-Sub-DVI_140740.html', 2, 1793210776104, 1789937972244);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 11, 'monitor-second', 'gear', 'Второй монитор', 'BenQ GL2055 20" 75 Гц', 'monitor', '', 'https://www.nix.ru/autocatalog/lcd_benq/20-ZHK-monitor-BenQ-GL2055-Black-LCD-1600x900-D-Sub-DVI_140740.html', 3, 1793210776104, 1789937977249);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 12, 'keyboard', 'gear', 'Клавиатура', 'ARDOR GAMING Blade PRO Optical', 'keyboard', '', 'https://www.dns-shop.ru/product/ea9145252763ed20/klaviatura-provodnaa-ardor-gaming-blade-pro-optical-cvet-cernyj/', 4, 1793210776104, 1789937851185);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 13, 'mouse', 'gear', 'Мышка', 'Zaopin ZPW MAX', 'mouse', '', 'https://mechkeys.com/products/zaopin-zpw?variant=46926838366431', 5, 1793210776104, 1793210776104);
INSERT OR REPLACE INTO "setup_items" ("owner", "pos", "id", "item_group", "category", "name", "icon", "note", "link", "sort_order", "added_at", "updated_at") VALUES('krafen', 14, 'mic', 'gear', 'Микрофон', 'FIFINE AmpliGame AM8', 'mic', '', 'https://www.dns-shop.ru/product/ec269d5c2492d9cb/mikrofon-fifine-am8-belyj/', 6, 1793210776104, 1789937922311);

INSERT OR REPLACE INTO "wishlist_items" ("owner", "pos", "id", "title", "category", "status", "priority", "price", "currency", "note", "link", "image", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 0, 'wish_muacgd3h_wy1cmp', 'GIGABYTE GeForce RTX 5060 EAGLE ICE OC', 'hardware', 'want', 2, 52000.0, 'RUB', '', 'https://www.dns-shop.ru/product/6a1955092199d582/videokarta-gigabyte-geforce-rtx-5060-eagle-ice-oc-gv-n5060eagleoc-ice-8gd/', 'https://c.dns-shop.ru/thumb/st1/fit/wm/0/0/7c6e80025f340dd2ed7d0f85218f5810/3302542712a7786d42190b0164cce119a0751f2f3b8f6cd748b6a68910b2ef41.jpg.webp', '[]', 0, 1789940660813, 1791578217948);
INSERT OR REPLACE INTO "wishlist_items" ("owner", "pos", "id", "title", "category", "status", "priority", "price", "currency", "note", "link", "image", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 1, 'wish_muacdsrs_39m0mb', '1000 ГБ M.2 NVMe накопитель Kingston FURY Renegade', 'hardware', 'want', 2, 27000.0, 'RUB', '', 'https://www.dns-shop.ru/product/8bd59f9e81e4ed20/1000-gb-m2-nvme-nakopitel-kingston-fury-renegade-sfyrsk1000g/', 'https://c.dns-shop.ru/thumb/st1/fit/500/500/125bb3f7ebb70c3e55df05ee74b14c8f/6c07efc548daeef5e10c25348193c12ddc1d020ac29e9dec00a4d756a0e94aba.jpg.webp', '[]', 0, 1789940541160, 1791578229140);
INSERT OR REPLACE INTO "wishlist_items" ("owner", "pos", "id", "title", "category", "status", "priority", "price", "currency", "note", "link", "image", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 2, 'wish_muaazsqr_jjsxh1', 'Джерси', 'other', 'want', 2, 4200.0, 'RUB', '', 'https://torch-fff.com/shop/lagoda-jersey/', 'https://torch-fff.com/upload/iblock/935/9352d21e6e68e87d5651dfff23507fd5.jpg', '[]', 0, 1789938208323, 1789938208323);
INSERT OR REPLACE INTO "wishlist_items" ("owner", "pos", "id", "title", "category", "status", "priority", "price", "currency", "note", "link", "image", "tags_json", "favorite", "added_at", "updated_at") VALUES('krafen', 3, 'wish_muaaky3b_qu1aps', 'Футболка', 'other', 'want', 2, 3200.0, 'RUB', 'хочу крутую футболку', 'https://torch-fff.com/shop/sakura/', 'https://torch-fff.com/upload/iblock/9a8/9a816980638edbb93e39d2b38000358f.jpg', '[]', 0, 1789937515415, 1789937515415);

INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-09-03_17-56', '1_SRlYEQ9E0hF1dWmoS3segy5qSAV7rG3', 'мама в 16 (я) ｜ !конкурс ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '7.21 ГБ', '', '2026-09-03_17-56', '', '', '', '', '', 0, '');
INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-09-02_12-15', '1td-vOnVhyvsT6VjzROhhdIV7asxvJImR', 'ну допустим мяу ｜ !конкурс ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '12.32 ГБ', '', '2026-09-02_12-15', '', '', '', '', '', 0, '');
INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-08-31_13-44', '1u8ZrnJPIASjUEI5EeYS1v_X7C63YnDeJ', 'ну допустим мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '7.48 ГБ', '', '2026-08-31_13-44', '', '', '', '', '', 0, '');
INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-08-30_13-04', '1igadvmUfhI_XOjcmGsE_2rMaxYB7_4Be', 'мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '1.84 ГБ', '', '2026-08-30_13-04', '', '', '', '', '', 0, '');
INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-08-30_01-01', '1mLVCiVyC6yBVkX-E3YJafLY7EJhGeu-k', 'мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '3.65 ГБ', '', '2026-08-30_01-01', '', '', '', '', '', 0, '');
INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-08-27_17-23', '1KyokELlAV_-f703pnSjEtRpaKyWDH2mk', 'щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '7.5 ГБ', '', '2026-08-27_17-23', '', '', '', '', '', 0, '');
INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-08-25_14-52', '13maPLTBIWghOtGDDhdtG5iY9MUFdeXeE', 'щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '13.25 ГБ', 'эта запись лагающая — не исправить', '2026-08-25_14-52', '', '', '', '', '', 0, '');
INSERT OR REPLACE INTO "drive_vods" ("id", "drive_id", "title", "size_label", "note", "sort_key", "folder_drive_id", "chat_drive_id", "thumb_drive_id", "meta_drive_id", "game", "duration_s", "started_at") VALUES('2026-08-23_11-50', '1QPUp9NYaHEdvS8P-cndGpks1LivR-YkP', 'щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб', '17.27 ГБ', '', '2026-08-23_11-50', '', '', '', '', '', 0, '');
COMMIT;
