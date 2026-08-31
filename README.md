# Kanami

Kanami — self-hosted Discord-бот для статистики голосовой и текстовой активности
небольшого или среднего сервера. Он учитывает время участников в Voice и
Stage-каналах, собирает суточные счётчики сообщений без их содержимого,
показывает рейтинги и может вести долговечный журнал изменений сервера. Один
экземпляр Kanami обслуживает один настроенный Discord-сервер.

## Возможности

- учёт голосовых сессий с восстановлением после reconnect/restart;
- профиль участника и рейтинги участников и каналов за today/7d/30d/all;
- статистика отдельного канала, всего сервера и совместного времени двух
  участников;
- повторяющиеся активные часы, день недели, самое тихое трёхчасовое окно и
  компактная недельно-часовая heatmap голосовой активности за 7/30/90 дней;
- суточные агрегаты сообщений и replies и рейтинг `/topmessages` за
  today/7d/30d/all;
- optional Audit Logging участников, voice-переходов, каналов, ролей и
  ban/unban в отдельный Discord-канал с сохранением событий в PostgreSQL;
- optional Autorole для новых участников;
- optional автоматические поздравления с годовщиной вступления в настроенный
  Discord-канал;
- optional приветствие ранее ушедших участников с durable snapshot их lifetime
  voice/text/achievement статистики;
- отдельный защищённый Web Admin со статусом PostgreSQL и
  paginated-списком текущих участников;
- шестнадцать guild-only slash-команд, включая PostgreSQL-backed `/rules` с
  persistent-кнопкой, пользовательский каталог достижений, встроенную справку
  `/help` и административные `/rules-status` и `/health`.

Kanami не читает и не хранит содержимое сообщений. Privileged intent
`MESSAGE_CONTENT` выключен; text runtime не обращается к `Message.content`,
`Message.attachments` или embeds. Поэтому live-сбор считает сообщения и replies,
но передаёт `attachment_count=0`: колонка остаётся в persistence schema для
возможного будущего явно включаемого режима. Presence используется только для
optional Game Tracking при `GAME_TRACKING_ENABLED=true`; typing и DM tracking
выключены.

## Системные требования

Первая документированная production-конфигурация — **Debian 13**, `systemd` и
локальный PostgreSQL с доступом в интернет к Discord и Python package index.
Нужен Discord Application с Bot user.

Ориентир для небольшого/среднего сервера, а не строгий benchmark:

| | vCPU | RAM | Disk |
| --- | ---: | ---: | ---: |
| Минимально | 1 | 1 GB | 16 GB |
| Рекомендуется | 2 | 2 GB | 32 GB |

Фактический расход диска зависит от активности сервера, retention settings и
объёма audit/statistics history.

## Быстрый старт

1. Создайте Discord Application и Bot, включите **Server Members Intent** и
   пригласите приложение на сервер. Подробности: [INSTALL.md](docs/INSTALL.md).
2. Клонируйте репозиторий и запустите installer из checkout:

   ```bash
   git clone https://github.com/lDiestrol/kanami.git
   cd discord-bot
   sudo ./scripts/install.sh
   ```

3. Заполните `DISCORD_TOKEN` и `DISCORD_GUILD_ID` в
   `/etc/kanami/kanami.env`:

   ```bash
   sudoedit /etc/kanami/kanami.env
   ```

4. Проверьте миграции, включите service и посмотрите журнал:

   ```bash
   sudo -u kanami env DATABASE_URL="$(sudo sed -n 's/^DATABASE_URL=//p' /etc/kanami/kanami.env)" \
     /opt/kanami/.venv/bin/alembic -c /opt/kanami/alembic.ini current
   sudo systemctl enable --now kanami
   systemctl status kanami --no-pager
   journalctl -u kanami -n 50 --no-pager
   ```

Installer создаёт локальную PostgreSQL role/database, применяет Alembic
migrations и устанавливает unit, но намеренно не запускает бота с пустыми
Discord credentials.

## Настройка Discord

Обязателен privileged **Server Members Intent**. Runtime использует intents
guilds, members, moderation, voice states и guild messages; `MESSAGE_CONTENT` не
требуется и остаётся выключенным. Privileged **Presence Intent** нужен только
при явно включённом Game Tracking и предварительно разрешается вручную в Discord
Developer Portal. При приглашении используйте scopes `bot` и
`applications.commands` и выдайте только необходимые права: бот должен видеть
отслеживаемые Voice/Stage-каналы и guild text channels, а для audit-канала — также
отправлять сообщения и embeds. Те же права нужны в каналах автоматических
годовщин и возвращений. `Administrator` не нужен. `Manage Roles`
требуется только при включённом Autorole; роль Kanami должна быть выше
выдаваемой роли.

Полный Discord setup и правила least privilege описаны в
[INSTALL.md](docs/INSTALL.md#подготовка-discord).

## Команды

В текущей версии зарегистрированы шестнадцать guild-only slash-команд:

| Команда | Назначение |
| --- | --- |
| `/help` | Краткая справка по командам Kanami |
| `/profile [user]` | Паспорт участника Kanami |
| `/stats [user] [period]` | Голосовой профиль участника |
| `/games [user] [period]` | Игровая активность участника за 7/30/90 дней или всё время |
| `/top [period]` | TOP-10 участников по голосовому времени |
| `/channels [period]` | TOP-10 Voice/Stage-каналов |
| `/channelstats <channel> [period]` | Статистика выбранного канала |
| `/together <user1> <user2>` | Совместное голосовое время двух участников |
| `/serverstats [period]` | Общая голосовая статистика сервера |
| `/activity [period]` | Когда сервер наиболее активен за 7/30/90 дней |
| `/topmessages [period]` | TOP-10 участников по количеству сообщений |
| `/achievements [user]` | Достижения свои или выбранного участника |
| `/anniversaries` | Ближайшие годовщины вступления участников на сервер |
| `/rules` | Текущая опубликованная версия правил и кнопка принятия |
| `/rules-status` | Приватные version/publication/acceptance metrics для Manage Server |
| `/health` | Приватная read-only диагностика для участников с Manage Server |

`period` поддерживает today, 7d, 30d и all. Команды синхронизируются только с
сервером из `DISCORD_GUILD_ID`, поэтому обычно появляются быстро.
`/health` и `/rules-status` дополнительно ограничены Discord permission
`manage_guild`; health-команда выполняет
PostgreSQL probe изолированно и не мешает показать остальные runtime-метрики при
недоступности БД.

## Какие данные хранит бот

PostgreSQL хранит Discord IDs и snapshot-метаданные guild/users/members/voice
channels, логические голосовые сессии и интервалы с UTC timestamps. Эти данные
нужны для долговременной статистики, включая историю удалённых участников и
каналов.

Optional Game Tracking сохраняет только Playing identity и подтверждённые UTC
границы в `game_sessions`: display game name, optional Discord application ID,
start, checkpoint и end. Raw Presence, status, platform, Custom Status, Spotify,
Rich Presence details/state/secrets не сохраняются.

Для Discord identity хранятся глобальные `username`/`global_name` и отдельный
для каждого guild `nickname`. Эти изменяемые snapshot-поля обновляются при
startup/reconnect provisioning и событиях участников; вычисляемое отображаемое
имя и аватары не сохраняются.

Текстовая активность хранится только в `daily_text_activity`: одна строка на
guild, участника, канал и локальную дату с `message_count`, `reply_count` и
`attachment_count`. Содержимое, `message_id` и другие per-message данные не
сохраняются; live `attachment_count` сейчас всегда равен нулю.

Если включён Audit Logging, хранятся нормализованные изменения участников,
ролей, каналов, timeout и ban/unban, сведения о доставке audit embeds, а также
некоторые имена/названия и avatar asset key/URL в JSON snapshots. Transient
audit events по умолчанию удаляются через 90 дней; важные события могут
храниться без автоматического срока удаления. Бинарные аватары не сохраняются.

При включённых автоматических годовщинах `audit_events` также служит durable
очередью доставки: уникальный ключ guild/user/дата годовщины предотвращает
повторную постановку, а успешная отправка и retry-состояние сохраняются в БД.
Канал задаётся через `DISCORD_ANNIVERSARY_CHANNEL_ID`.

При заданном `DISCORD_RETURN_CHANNEL_ID` permanent `member.left` history
позволяет отличить первый вход от возвращения. Возврат после отсутствия не менее
`MEMBER_RETURN_MIN_ABSENCE_SECONDS` (по умолчанию 86400) сохраняется как
идемпотентный `member.returned` со snapshot lifetime-статистики и доставляется
тем же retry runner в отдельный канал. `guild_members.joined_at` остаётся текущей
датой вступления Discord и продолжает использоваться годовщинами.

## Kanami Web Admin

Web Admin запускается отдельным процессом и не запускает Discord runtime. Stage
3C использует Discord OAuth2 Authorization Code flow с `state`, PKCE S256 и
локальной server-side session, а Stage 3D отдельно проверяет право доступа до
создания session. Same-host/default deployment сохраняет loopback bind; для
remote HTTPS reverse proxy разрешён только явно включённый private bind с
соответствующим firewall allow rule. Bot Control при любой схеме остаётся
unconditional loopback-only. Общий engine допускает узкие Rules/audit mutations,
а полностью read-only страницы
владеют собственными transaction boundaries; Server Analytics первым statement
устанавливает `REPEATABLE READ, READ ONLY`. OAuth identity использует отдельные
client ID/secret и scope `identify`; основной `DISCORD_TOKEN` Web Admin не
получает.

После настройки `DATABASE_URL`, существующего `DISCORD_GUILD_ID` и переменных
`WEB_ADMIN_DISCORD_*` и `WEB_ADMIN_ALLOWED_USER_IDS` из
[CONFIGURATION.md](docs/CONFIGURATION.md#web-admin)
зарегистрируйте точный callback URI в Discord Developer Portal и запустите:

```bash
uv run kanami-web-admin
```

По умолчанию сервер доступен только локально на `http://127.0.0.1:8000`:

- `GET /admin/login` — начало Discord OAuth2 login;
- `GET /admin/auth/discord/callback` — одноразовый OAuth callback;
- `POST /admin/logout` — CSRF-protected отзыв локальной session;
- `GET /admin/` — защищённый server-rendered обзор состояния БД и счётчики;
- `GET /admin/health` — JSON health probe, возвращающий `200` при успешном
  `SELECT 1` и `503` при недоступной PostgreSQL.
- `GET /admin/members?page=1&q=...&sort=name&order=asc` — read-only каталог
  текущих non-bot участников с поиском, global sorting и lifetime
  voice/text/achievement статистикой.
- `GET /admin/members/{discord_id}` — read-only профиль текущего или ранее
  ушедшего non-bot участника с identity, lifetime-статистикой, achievements и
  последними lifecycle events.
- `GET /admin/analytics?period=7d|30d` — OWNER/ADMIN Server Analytics по
  завершённым локальным календарным дням: KPI и сравнения, ежедневные Voice/Text
  charts, Voice heatmap и Top-5 без JavaScript.
- `GET /admin/settings/bot-profile` — Stage 4 форма просмотра и изменения
  guild-specific nickname/avatar самого бота; все изменения выполняются только
  через CSRF-protected POST.
- `GET /admin/administrators` — OWNER-only список двух постоянных env OWNER и
  active managed ADMIN с формами выдачи/отзыва доступа.
- `GET /admin/audit` — OWNER-only read-only журнал последних 100 событий выдачи
  и отзыва managed ADMIN в configured guild.
- `POST /admin/administrators/grant` и `/revoke` — OWNER-only mutations через
  loopback Bot Control; сами эти administrator operations не выполняют прямую
  PostgreSQL mutation из Web Admin.

Страница участников показывает по 50 записей. Числовой запрос ищет точный
Discord ID, текстовый — без учёта регистра по nickname, global name и username.
Имя выбирается при чтении как nickname → global name → username → Discord ID;
`joined_at` показывается в UTC.

Обе страницы участников ограничены сервером из `DISCORD_GUILD_ID`. Detail page
выполняет один запрос profile/aggregates/achievements и один bounded запрос
последних 20 `member.joined`/`member.left`/`member.returned`; departed membership
остаётся доступной по прямой ссылке.

Server Analytics использует один database-enforced read-only snapshot на HTTP
request. Активным считается non-bot пользователь хотя бы с одним persisted
message или eligible non-AFK Voice activity; Games не входят. Earliest-recorded
для Voice/Text означает только самую раннюю найденную записанную активность, а
не запуск collector или гарантию полноты. Поэтому current и previous KPI
сохраняют отдельные source-aware предупреждения о potentially partial history.
Периоды 7/30 исключают сегодня и не являются rolling windows.

Для Stage 4 Discord process включает узкий control interface через
`DISCORD_BOT_CONTROL_ENABLED=true`, loopback `127.0.0.1` и отдельный shared
secret. Web Admin получает только соответствующие
`WEB_ADMIN_BOT_CONTROL_URL`/`WEB_ADMIN_BOT_CONTROL_SHARED_SECRET` и по-прежнему
не получает `DISCORD_TOKEN`. Интерфейс не является generic Discord proxy: он
содержит только фиксированные операции чтения, установки и явного сброса
nickname/avatar собственного bot member и managed ADMIN grant/revoke в
`DISCORD_GUILD_ID`. Avatar ограничен
8 MiB и проверяется как PNG/JPEG по MIME type и сигнатуре содержимого.

В production процессы должны использовать раздельные env-файлы или systemd
credentials/drop-ins: bot-side конфигурация содержит Bot token, web-side — OAuth
и control secret без Bot token. В отдельном web env нужно явно повторить
безопасный runtime context: `DISCORD_GUILD_ID`, `REPORT_TIMEZONE`,
`VOICE_CHECKPOINT_INTERVAL_SECONDS`, `GAME_TRACKING_ENABLED` и
`GAME_CONFIRM_INTERVAL_SECONDS`. Tracking settings нужны
Web Admin только для честного отображения `/admin/system`; tracker запускает
только bot service. Текущий installer отдельный Web Admin unit и это разделение
автоматически не создаёт. Рекомендуемый отдельный Linux user `kanami-web` не
должен иметь доступа к `/etc/kanami/kanami.env`. Для Git metadata этому user
нужно разрешить только `safe.directory=/opt/kanami`; точные команды приведены в
[deployment guide](docs/WEB_ADMIN_DEPLOYMENT.md#git-metadata-при-раздельных-service-users).

Параметры `WEB_ADMIN_HOST` и `WEB_ADMIN_PORT` имеют defaults `127.0.0.1` и
`8000`. Для central reverse proxy разрешён только конкретный RFC1918/IPv6 ULA
адрес с `WEB_ADMIN_ALLOW_PRIVATE_BIND=true`; wildcard, hostname и public IP
отклоняются. Cookie содержит только
случайный opaque session ID, а identity/expiry/CSRF хранятся в bounded memory
store. Discord access/refresh tokens не сохраняются; restart процесса завершает
все sessions. `GET /admin/health` намеренно остаётся публичным loopback probe,
остальные будущие `/admin/...` закрываются middleware по умолчанию.

Discord OAuth `identify` отвечает только на вопрос «кто вошёл?» и сам по себе не
разрешает доступ. Discord ID из `WEB_ADMIN_ALLOWED_USER_IDS` имеют постоянную
роль OWNER; active `web_admin_access_grants` добавляют роль ADMIN и не могут
понизить OWNER. Обе роли допускаются только при текущем non-bot membership в
configured `DISCORD_GUILD_ID` по данным PostgreSQL. Публичный Web Admin
поддерживается только за HTTPS reverse proxy;
три схемы, firewall, OAuth и systemd hardening описаны в
[WEB_ADMIN_DEPLOYMENT.md](docs/WEB_ADMIN_DEPLOYMENT.md).

Переменные `RAW_MESSAGE_RETENTION_DAYS` и `SERVER_EVENT_RETENTION_DAYS`
зарезервированы конфигурационным контрактом, но raw per-message и отдельный
server-event collectors не реализованы. `RAW_MESSAGE_RETENTION_DAYS` не
применяется к постоянным суточным агрегатам.

## Обновление

Для экземпляра, установленного installer-ом:

```bash
sudo /opt/kanami/scripts/update.sh
```

Скрипт останавливается при локальных изменениях, выполняет только
`git pull --ff-only`, синхронизирует locked dependencies, применяет миграции,
обновляет systemd unit и перезапускает service. Он не использует forced checkout
или `git reset --hard`. Installer сохраняет branch и `origin` исходного
checkout: для публичного repository рекомендуется обычный HTTPS remote и
поддерживаемая production branch (`main` либо отдельная release branch).
Private repository/fork потребует deploy key или другой Git credential mechanism,
доступный системному пользователю `kanami`; credentials в scripts/env examples
не встраиваются.

## Безопасность

Не коммитьте `.env` и `/etc/kanami/kanami.env`, не публикуйте
`DISCORD_TOKEN` и не вставляйте его в команды shell. При утечке немедленно
regenerate token в Discord Developer Portal. Installer хранит production env с
правами `0640` (`root:kanami`), а unit не содержит секретов.

## Документация

- [INSTALL.md](docs/INSTALL.md) — установка Debian 13 + systemd + PostgreSQL;
- [CONFIGURATION.md](docs/CONFIGURATION.md) — все environment variables;
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — developer quick-start и проверки;
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — технические решения;
- [WEB_ADMIN_DEPLOYMENT.md](docs/WEB_ADMIN_DEPLOYMENT.md) — Caddy/Nginx,
  firewall, OAuth и systemd для публичной панели;
- [STATUS.md](docs/STATUS.md) — текущее состояние и следующие шаги.

## Лицензия

Проект распространяется по лицензии MIT. Подробности см. в [LICENSE](LICENSE).
