# Архитектура

## Назначение документа

Этот документ хранит архитектурный контекст и долгосрочные технические решения проекта. Он обновляется при принятии решений, влияющих на структуру приложения, границы компонентов, хранение данных, конфигурацию, развёртывание или эксплуатацию.

## Текущий контекст

- Проект предназначен для self-hosted Discord-бота на Python.
- Первоначальный целевой масштаб — один Discord-сервер примерно на 50–100 пользователей.
- Основная задача — долговременная статистика активности с особым вниманием к голосовым каналам.
- Реализованы persistence и service/repository логика voice tracking, startup reconciliation, live voice-state adapter, периодический checkpoint, read-only voice statistics query layer, суточные агрегаты текстовой активности и durable Audit Logging.
- Discord Gateway runtime подключён для provisioning/reconciliation, live voice и text tracking, connected-only checkpoint, автоматических годовщин и шестнадцати guild-only slash-команд: `/help`, `/profile`, `/stats`, `/games`, `/top`, `/channels`, `/channelstats`, `/together`, `/serverstats`, `/activity`, `/topmessages`, `/achievements`, `/anniversaries`, `/rules`, `/rules-status` и `/health`; pagination ещё не реализована.

## Принятые архитектурные решения

Статус перечисленных ниже решений: принято. Дата фиксации: 2026-08-10.

### Python и управление проектом

- Используется Python 3.13.
- Проект и зависимости управляются через uv и `pyproject.toml`.
- `uv.lock` хранится в Git для воспроизводимости окружения.
- Используется виртуальное окружение проекта; оно является локальным и в Git не хранится.
- Асинхронное выполнение строится на asyncio.
- Зависимости описываются в `pyproject.toml` и обновляются только явно.
- После обновления `uv.lock` обязательны review и тесты.
- В deployment не используются плавающие `latest`-зависимости.

### Конфигурация

- Для конфигурации используется pydantic-settings.
- Приложение получает конфигурацию из environment variables.
- Локальная разработка может загружать значения из `.env`; настоящий `.env` не хранится в Git.
- Конфигурация полностью валидируется при запуске до начала основной работы приложения.
- Общая database-only модель конфигурации валидирует `DATABASE_URL` для приложения и Alembic; миграции не требуют Discord credentials.
- Секреты не имеют реальных default values.

Обязательные переменные без default values:

- `DISCORD_TOKEN`;
- `DISCORD_GUILD_ID`;
- `DATABASE_URL`.
- для отдельного Web Admin process: `WEB_ADMIN_DISCORD_CLIENT_ID`,
  `WEB_ADMIN_DISCORD_CLIENT_SECRET`, `WEB_ADMIN_DISCORD_REDIRECT_URI`.

Переменные с default values:

| Переменная | Default |
| --- | --- |
| `REPORT_TIMEZONE` | `UTC` |
| `RAW_MESSAGE_RETENTION_DAYS` | `90` |
| `SERVER_EVENT_RETENTION_DAYS` | `365` |
| `VOICE_MIN_SESSION_SECONDS` | `10` |
| `VOICE_CHECKPOINT_INTERVAL_SECONDS` | `60` |
| `GAME_TRACKING_ENABLED` | `false` |
| `GAME_CONFIRM_INTERVAL_SECONDS` | `60` |
| `MEMBER_RETURN_MIN_ABSENCE_SECONDS` | `86400` |
| `AUDIT_TRANSIENT_RETENTION_DAYS` | `90` |
| `WEB_ADMIN_HOST` | `127.0.0.1` |
| `WEB_ADMIN_ALLOW_PRIVATE_BIND` | `false` |
| `WEB_ADMIN_PORT` | `8000` |
| `WEB_ADMIN_COOKIE_SECURE` | `true` |
| `WEB_ADMIN_SESSION_LIFETIME_SECONDS` | `28800` |
| `WEB_ADMIN_ALLOWED_USER_IDS` | пустой deny-all allowlist |
| `DISCORD_BOT_CONTROL_ENABLED` | `false` |
| `DISCORD_BOT_CONTROL_HOST` | `127.0.0.1` |
| `DISCORD_BOT_CONTROL_PORT` | `8765` |
| `LOG_LEVEL` | `INFO` |

`DISCORD_AUDIT_LOG_CHANNEL_ID` — optional positive snowflake baseline. При
отсутствии DB override и значения общий Audit Logging выключен.

`DISCORD_AUTOROLE_ID` — optional positive snowflake baseline. При отсутствии
override и значения Autorole выключен; роль в коде не хардкодится.

`DISCORD_ANNIVERSARY_CHANNEL_ID` — optional positive snowflake baseline. При
отсутствии override и значения автоматическая доставка годовщин выключена;
`/anniversaries` остаётся доступна.

`DISCORD_RETURN_CHANNEL_ID` — optional positive snowflake baseline для сообщений
о возвращении. При отсутствии override и значения feature не создаёт
`member.returned`. `MEMBER_RETURN_MIN_ABSENCE_SECONDS` — positive integer с
default `86400`.

`DISCORD_GUEST_ROLE_ID`, `DISCORD_INITIATED_ROLE_ID`,
`DISCORD_GUARDIAN_ROLE_ID`, `DISCORD_PURPLE_ROLE_ID` и `DISCORD_GOLD_ROLE_ID` —
optional stable role IDs для Profile v1. Они не разрешаются по Discord display
name. Guest/Initiated/Guardian используются только для отображения текущего
уровня; Purple/Gold дополнительно дают право читать чужую пользовательскую
статистику. Отсутствующий privileged ID означает deny, а не wildcard.

Новые переменные конфигурации не добавляются без необходимости и обновления этого контракта.

`RuntimeSettings` содержит общие database/logging settings, configured
`DISCORD_GUILD_ID`, используемый обоими process параметр voice eligibility
`VOICE_MIN_SESSION_SECONDS` и общий `REPORT_TIMEZONE`. Поэтому Discord-команды и
Web Dashboard используют одинаковую локальную границу «сегодня». Независимые
sibling-классы `Settings` и `WebSettings` расширяют его соответственно
Discord-only и web-only полями: Discord process не имеет web/OAuth/allowlist
settings, а Web Admin не требует Discord Bot token. `WebSettings` валидирует
literal IP bind: loopback разрешён по умолчанию, а конкретный RFC1918 IPv4/IPv6
ULA требует `WEB_ADMIN_ALLOW_PRIVATE_BIND=true`; hostname, wildcard, link-local
и public IP запрещены. Также валидируются отдельный OAuth confidential client,
exact callback URI, нормализованный comma-separated
`WEB_ADMIN_ALLOWED_USER_IDS` и явное соответствие HTTPS/Secure cookie либо
loopback HTTP/explicit insecure development cookie. Allowlist получает raw
environment string через `pydantic-settings` `NoDecode`, поэтому публичный
формат не зависит от JSON decoding complex fields. Пустой или отсутствующий
allowlist означает deny-all. Bot control по умолчанию выключен; при включении
bot-side требует shared secret не короче 32 символов, а web-side принимает
только полностью заданную пару secret + canonical
`http://127.0.0.1:<port>` URL. Эти два sibling settings-класса предназначены для
раздельных production environment: Web Admin не должен видеть `DISCORD_TOKEN`.

### Web Admin foundation and scoped writes

Web Admin остаётся частью одного Python package, но имеет отдельный console entry point `kanami-web-admin` и отдельный Starlette/Uvicorn process. Discord client, Gateway lifecycle и background tasks из него не создаются. Starlette выбран как небольшой ASGI routing/lifespan слой, Uvicorn — как ASGI server; HTML строится на сервере без Node.js, frontend build и template dependency.

WUI-1 сохраняет этот server-rendered подход и выделяет общий presentation layer
`web/presentation.py`: он владеет Kanami Dark Neon design tokens, responsive app
shell, role-aware sidebar с active state, page header и едиными CSS patterns для
cards, badges, tables, forms и operational states. Domain renderers по-прежнему
формируют только содержимое своих существующих страниц и передают его в общий
shell; frontend framework, external assets/CDN и client-side runtime не
добавляются. Видимость OWNER navigation остаётся только presentation aid и не
заменяет route-level authorization.

Application factory создаёт SQLAlchemy resources в Starlette lifespan и гарантированно вызывает `engine.dispose()` при shutdown. Используется существующий `DATABASE_URL`, `DatabaseResources`, session factory и ORM metadata; модели и schema не дублируются. После появления Rules Admin web engine больше не устанавливает connection-wide `default_transaction_read_only=on`: bounded query services по-прежнему выполняют только `SELECT`, а прямые DB mutations разрешены единственному rules-management service и оборачиваются в явные короткие транзакции. Остальные write domains Web Admin по-прежнему используют узкий Bot Control boundary.

Stage 3C добавляет Discord OAuth2 Authorization Code authentication с минимальным scope `identify`, cryptographically random one-shot `state`, PKCE S256 и точным configured callback URI. Изолированный adapter использует lifespan-owned shared `aiohttp.ClientSession`, bounded timeout, TLS verification, disabled redirects и bounded validated JSON. Access/refresh tokens существуют только внутри callback и не сохраняются в cookie, session, БД или logs; Bot token не используется. Uvicorn access log выключен, поскольку callback query содержит authorization code и state.

Stage 3D отделяет authorization от OAuth identity. Discord-independent policy service сначала требует exact membership Discord user ID в `WEB_ADMIN_ALLOWED_USER_IDS`, затем через отдельный bounded SELECT-only repository проверяет current non-bot membership configured guild (`guild_members.left_at IS NULL`, `discord_users.is_bot IS false`). Repository переиспользует lifespan session factory, выполняет только SELECT и ограничивает lookup одной парой guild/user и `LIMIT 1`; Discord API не вызывается. Только положительное решение разрешает `WebSessionStore.create`. Отказ возвращает нейтральный HTTP 403, удаляет временную OAuth cookie и не создаёт session/cookie; DB lookup failure безопасно превращается в отсутствие подтверждённого membership. Role-based policy в Stage 3D не входит.

Stage 4 оставляет Discord credentials и Gateway ownership в основном bot process.
При явном включении этот process поднимает второй Uvicorn listener строго на
`127.0.0.1` и предоставляет только пять фиксированных bot-profile operations:
получить профиль, установить/сбросить nickname, установить/сбросить avatar.
Generic URL, Discord route, guild ID или target user из browser/request не
принимаются. Service всегда получает configured guild из bot settings, выбирает
`guild.me` и вызывает `discord.Member.edit`; сериализованный lock исключает
пересечение edits. Требуется `discord.py>=2.7`, где собственный guild avatar
поддерживается штатным `Member.edit(avatar=...)`.

Web Admin вызывает control interface lifespan-owned `aiohttp` client с bounded
timeouts/body/response, disabled redirects и Bearer shared secret, сравниваемым
bot-side через constant-time comparison. Actor Discord user ID берётся только из
server-side WebSession и передаётся отдельным audit header; browser не выбирает
actor, guild или Discord endpoint. Все web POST требуют session CSRF token.
Nickname trim/length/control-character validation выполняется с обеих сторон.
Avatar ограничен 8 MiB, допускает только PNG/JPEG и проверяется по MIME type и
magic signature как в web process, так и в bot process; binary/base64 и shared
secret не логируются и не сохраняются. Поскольку Starlette `max_part_size` не
ограничивает file parts, web endpoint дополнительно считает фактические ASGI body
chunks до multipart parser и прекращает чтение выше 8 MiB + 64 KiB bounded
overhead независимо от отсутствующего или ложного `Content-Length`; parser
закрывает временный spooled file при таком исключении. Reset оформлен отдельными explicit
operations. При disabled/misconfigured/unavailable control UI работает
fail-closed и не показывает формы изменения.

Production boundary требует отдельного Linux user для Web Admin (например
`kanami-web`) без доступа к `/etc/kanami/kanami.env`, а также отдельного web env
или systemd credentials с OAuth/control secrets. Автоматическая смена текущих
service users и provisioning этих файлов не входит в Stage 4 coding scope.

Успехи и отказы пишутся структурированно с operation, category и actor Discord
ID без payload. Реальный Discord member update по-прежнему проходит через
существующий Gateway audit pipeline (`member.nickname_updated` или
`member.guild_avatar_updated`), когда Audit Logging включён; отдельная прямая
запись из Web Admin намеренно не создаётся, чтобы не дублировать событие. Schema
и migrations Stage 4 не меняет.

### Web Admin Dashboard v1

Authenticated `GET /admin/` является стартовой административной панелью, а не
глобальным счётчиком таблиц. Route получает готовый `WebAdminDashboard` из
отдельного testable service и передаёт его server-rendered responsive renderer;
SQL и Voice semantics в HTML не размещаются. Навигация ведёт только на
существующие страницы. OWNER видит Administrators и Audit Log, ADMIN — общие
Dashboard, Members, Bot и Server Settings; backend authorization каждой страницы
остаётся обязательной независимо от видимости ссылки.

PostgreSQL read side ограничен configured guild. Один set-based overview SELECT
возвращает persisted guild name, current non-bot member count, distinct non-AFK
users с открытым interval и открытые logical Voice sessions. Voice today и 30d
вычисляются существующим `VoiceStatisticsService` и
`SqlAlchemyVoiceStatisticsRepository`: eligible whole-session threshold,
exact/estimated recovery, AFK/bot exclusions и `confirmed_through_at` остаются
такими же, как в Discord statistics commands. Все чтения server snapshot идут в
одной короткой `REPEATABLE READ` session; «сегодня» использует общий
`REPORT_TIMEZONE`, а 30d означает rolling interval `[as_of - 30 days, as_of]`.
Python не загружает все sessions/intervals для агрегации.

Database probe и Bot Control profile probe являются независимыми источниками.
Недоступный control API даёт честный Unknown/Unavailable и не скрывает
PostgreSQL overview. Успешный existing profile operation подтверждает готового
guild bot member; bot-not-ready/guild-unavailable отображаются как Not ready.
Dashboard не заявляет Gateway latency, uptime, исторический heartbeat или иной
monitoring, для которого у Web Admin нет надёжного источника. Новых таблиц,
migration, write path, frontend framework и external chart library нет.

### Server Analytics A1 read foundation

Server Analytics выделен в Discord/Web-independent feature boundary
`features/server_analytics`. A1 не добавляет route или presentation, а
предоставляет immutable `ServerAnalyticsReport` для будущего orchestration
layer. Периоды 7d/30d означают завершённые local calendar days в
`REPORT_TIMEZONE`: current `[midnight D-N, midnight D)`, today исключён, previous
непосредственно предшествует current. Все UTC/date boundaries вычисляются один
раз из явно переданного `as_of`.

Read repository возвращает один combined current+previous Voice result,
ограниченный внешним 14/60-day окном, один grouped Text result и отдельный
earliest-recorded metadata result; query-per-KPI и query-per-day отсутствуют.
Voice read вызывает общий eligibility builder существующей statistics
persistence: bots и AFK исключены, Stage остаётся, minimum применяется к exact
duration whole logical session, eligible estimated parts учитываются, а open
interval ограничен `confirmed_through_at`. Whole-session eligibility CTE при
этом может рассматривать более раннюю guild history; candidate-session
оптимизация отложена до production `EXPLAIN (ANALYZE, BUFFERS)`. Daily Voice
series делит effective intervals по local midnight, а weekday/hour pattern
переиспользует существующий `/activity` aggregator с явными calendar bounds и
прежней DST/exposure normalization.

Voice top members сохраняют общий ranking contract команд: `total DESC`, затем
`exact DESC`, затем `user_id ASC`. Active member определяется как non-bot user с persisted message либо
положительным eligible Voice overlap; current membership state и games в это
определение не входят. Coverage DTO сообщает только earliest recorded local
date и независимо отмечает, начинаются ли current и previous windows раньше
неё. Он намеренно не заявляет дату запуска collector или доказанную полноту наблюдений. Transaction ownership
и `REPEATABLE READ` принадлежат Web/unit-of-work layer; domain service сам не
открывает session и не вызывает clock повторно. Schema,
migrations, background jobs и persisted analytics не добавлены.

### Server Analytics A2.1 Web integration foundation

Authenticated read-only `GET /admin/analytics` доступен OWNER и managed ADMIN и
принимает только фиксированный `period=7d|30d` с default 7d. Web orchestration
получает один `as_of`, открывает одну session и первым SQL statement выполняет
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`, после чего ровно
один раз вызывает `ServerAnalyticsService.get_report`; все секции страницы
потребляют один immutable report. Это request-local database-enforced read-only
snapshot и не меняет write-capable Web Admin engine. A1 по-прежнему не владеет
clock или transaction.

Top Voice/Text IDs объединяются и разрешаются одним set-based persisted lookup
в том же snapshot с общим precedence `nickname → global_name → username →
user_id`. Текущий Discord cache/API не вызывается, поэтому ушедший участник
сохраняет persisted fallback, а одинаковый ID в двух рейтингах не создаёт
повторный query. Ranking и KPI/delta calculations остаются в A1 без Web-копий.

Web adapter отдельно переносит current и previous coverage caveats. Voice hours
и unique Voice users зависят от Voice source, messages и unique authors — от
Text, active members — от обоих; `True` любого необходимого source означает
potential partial history, а `None` сохраняет empty-source uncertainty.
`NO_BASELINE` остаётся typed state и не превращается в synthetic percentage.
Минимальная server-rendered страница использует общий app shell, desktop/mobile
role-aware navigation и escaping, не добавляет JavaScript, charts, heatmap CSS,
dependencies или write endpoints. Controlled 400/503 не раскрывают SQL или
traceback. A2.1 остаётся integration boundary; финальная presentation построена
поверх него в A2.2 ниже без расширения transaction/query scope.

### Server Analytics A2.2 final presentation

A2.2 завершает локальную presentation Server Analytics v1, не меняя A1 report,
queries, transaction или authorization. Страница показывает пять готовых KPI с
typed `AVAILABLE`/`UNCHANGED_ZERO`/`NO_BASELINE`, human-readable Voice duration
и отдельной estimated contribution. Current и previous caveats берутся из
A2.1 source mapping и остаются рядом соответственно с текущим значением и его
comparison; математически корректный percentage при этом не скрывается.

`report.daily` server-side превращается в два независимых zero-safe HTML/CSS bar
chart: exact+estimated Voice и Messages. Каждая точка имеет видимые дату/value и
accessible label; estimated Voice не теряется. `report.voice_activity`
отображается без пересчёта semantics как semantic 8×7 table трёхчасовых окон,
где CSS intensity дополнена текстовыми glyph/labels, а `top_hours`,
`active_weekday` и `quietest_period` берутся непосредственно из DTO. 30-day
charts и heatmap используют только локальный controlled overflow; на 640/430 px
KPI/rankings складываются без global horizontal overflow.

Top Voice/Text используют уже разрешённые A2.1 names и существующий historical
member detail route, поддерживающий `left_at`; дополнительных lookup нет.
Страница остаётся server-rendered под общим Kanami shell и CSP `script-src
'none'`: JavaScript, client charts, dependencies, schema и background work не
добавлены. Server Analytics v1 реализован локально в feature branch, но Web
страница ещё не проходила production/browser smoke.

### Web Admin Operations / Health Dashboard W1.1–W1.2

Authenticated `GET /admin/system` доступен OWNER и managed ADMIN по той же
session policy, что Dashboard, и остаётся полностью read-only. Route получает
готовый `WebAdminSystemStatus` из operations service; service параллельно
объединяет безопасный Git adapter, узкий SQLAlchemy read repository и
существующий `BotProfileControl`, а server-rendered HTML не содержит SQL и не
использует polling. Навигация показывает страницу в группе «Система», не меняя
OWNER-only границы Administrators и Audit Log.

PostgreSQL repository сначала измеряет latency реального `SELECT 1`, затем
изолированными bounded SELECT читает `pg_database_size(current_database())`,
`alembic_version`, а также count/max `confirmed_through_at` открытых
`voice_sessions` и `game_sessions` configured guild. Ошибка probe помечает
PostgreSQL как Unavailable; ошибка отдельного дополнительного чтения оставляет
метрику недоступной и не превращается в HTTP 500. Alembic CLI из request path не
запускается. `VOICE_CHECKPOINT_INTERVAL_SECONDS`, `GAME_TRACKING_ENABLED` и
`GAME_CONFIRM_INTERVAL_SECONDS` являются общими несекретными полями
`RuntimeSettings`, чтобы bot и Web Admin показывали
один runtime contract; выключенная feature имеет нейтральный Disabled status.

Git commit и branch читаются bounded subprocess adapter с fallback `Unknown`,
если Git или repository недоступны; это metadata, а не выдуманный health signal.
При раздельных production users checkout остаётся `kanami:kanami`, а Git trust
для `kanami-web` задаётся user-scoped точным `safe.directory=/opt/kanami` при
deployment. Это не автоматизируется application code или bot-only installer:
они не должны менять ownership/write permissions, использовать wildcard trust
или управлять global config optional web user. Web unit фиксирует его
`HOME=/var/lib/kanami-web`, чтобы Git читал ожидаемый config.
Uptime относится только к текущему Web Admin process. Existing Bot Control
profile operation объективно различает Online, известный Not ready/Offline и
Unknown при недоступном control API; Gateway health/latency из этого сигнала не
выводятся. Общий статус строится только из доступности PostgreSQL, Bot Control и
известного online-состояния бота.

W1.2 вводит независимый от HTML enum `HealthStatus` со значениями
`HEALTHY`/`DEGRADED`/`UNAVAILABLE`/`NEUTRAL`, структурированные diagnostic
reasons и service-layer aggregation. Critical unavailable PostgreSQL, offline
bot, Voice или enabled Game Tracking дают overall `UNAVAILABLE`; любой
`DEGRADED` component либо недоступная integrity-диагностика при живом
PostgreSQL дают overall `DEGRADED`. `NEUTRAL` не ухудшает overall, поэтому
намеренно выключенный Game Tracking и неизвестная Git metadata остаются
информационными состояниями.

Voice считается stale только при наличии открытых sessions и возрасте самого
старого `confirmed_through_at > max(3 × VOICE_CHECKPOINT_INTERVAL_SECONDS,
180 seconds)`; ровно threshold ещё healthy, а ноль открытых sessions healthy
без freshness requirement. Для включённого Game
Tracking threshold равен
`max(GAME_CONFIRM_INTERVAL_SECONDS * 3, 180 seconds)`; disabled feature имеет
`NEUTRAL`. Все расчёты используют один внедряемый timezone-aware `now`,
нормализованный в UTC, а renderer получает уже вычисленные statuses/reasons.

Один set-based SELECT на каждую таблицу `voice_sessions`/`game_sessions`
возвращает open count, max/min confirmation, число открытых строк без
confirmation, число `(guild_id,user_id)` groups с несколькими открытыми строками
и число нарушений существующих temporal
invariants: `started_at <= confirmed_through_at`, а для закрытой строки также
`confirmed_through_at <= ended_at` и `started_at <= ended_at`. Partial unique
indexes и CHECK constraints остаются основной защитой записи; read-only checks
являются defense-in-depth и формируют отдельный блок «Целостность данных».
Schema/migrations, heartbeat, polling, history, alerts, auto-healing и
start/stop/restart W1.2 не добавляет.

W1.3 добавляет отдельную низкочастотную durable историю наблюдений, а не
внешнюю monitoring platform. Только основной Discord process запускает единый
`OperationalHealthObservationRunner` после успешного startup reconciliation
Voice и, если feature включена, Game: раз в 60 секунд он
классифицирует Discord Gateway, PostgreSQL diagnostic query, Voice freshness и
опциональный Game freshness и сохраняет одну компактную строку в
`operational_health_observations`. Game при выключенной feature имеет
`neutral`; overall хранит только `healthy`/`degraded`/`unavailable`. Причина —
короткая фиксированная операторская строка без exception text, URL, token или
stack trace. Cadence привязан к fixed monotonic start-to-start schedule: обычное
время запросов вычитается из следующего sleep, а просроченные ticks пропускаются
без catch-up storm. Нового env setting для периода нет.

Bot-owned write transaction одновременно удаляет строки configured guild
старше восьми дней. Это bounded retention примерно 11 520 строк на guild при
штатной минутной частоте, а не high-frequency telemetry. Web Admin сохраняет
SELECT-only repository для bounded восьмидневной истории (семь дней nominal
window и небольшой pre-window lookback) и не запускает recorder, retention либо
repair. Общая web connection больше не имеет `default_transaction_read_only=on`
из-за scoped Rules writes; права на эту таблицу остаются только read grants.
Migration `f2a6c9d41b73` добавляет
identity PK, guild FK, UTC observation time, component statuses, безопасные
component/reason и индекс `(guild_id, observed_at)`.

24h/7d делятся на ожидаемые минутные sampling slots: 1440 и 10080. В каждом
slot учитывается максимум одна observation, а при дублях выбирается худший
status. Nominal slots до первой доступной history отмечаются `Not monitored`, а
не `Missing`: если pre-window lookback доказывает, что recorder уже существовал,
monitored period начинается с границы окна, иначе — с первого observation в
окне. Внутренние и хвостовые незаполненные slots monitored period образуют
отдельный `Missing`, не классифицируемый как отказ PostgreSQL или другого
component. Healthy/Degraded/Unavailable/Missing и coverage считаются только по
monitored samples; UI отдельно показывает nominal, monitored, covered,
Not monitored, Missing и `History available since`. Окно `Full` только когда
monitored period покрывает всю nominal duration без gaps; новая трёхдневная
history в 7d окне остаётся `Partial`, но предыдущие четыре дня не выглядят как
outage. Пустая либо временно недоступная history не ломает текущую диагностику.
Непрерывная
последовательность non-healthy observations образует один incident, первый
следующий healthy snapshot задаёт recovery; если внутри incident появился
`unavailable`, он сохраняется как его наивысшая severity и причина. При полном
отказе PostgreSQL запись в эту же БД физически невозможна:
runner повторно пытается сохранить безопасный `PostgreSQL — health query failed`
snapshot, но длительный outage честно остаётся разрывом наблюдений и не
подменяется выдуманной точностью. W1.3 не отправляет alerts, не выполняет
restart/self-healing и не добавляет Prometheus/Grafana/daemon.

### Web Admin managed administrators and audit

Stage 6A вводит durable историю управляемых администраторов в
`web_admin_access_grants`: активной считается запись без `revoked_at`, а partial
unique index допускает не более одного активного grant на пару guild/user и при
этом сохраняет историю revoke/re-grant. Repository и domain service не владеют
транзакцией; grant/revoke и создание important `web_admin.access_granted` либо
`web_admin.access_revoked` audit event выполняются атомарно bot-side control
service.

Для managed administrators Web Admin может инициировать только
две фиксированные authenticated loopback Bot Control операции с единственным
`user_id`; configured `guild_id` и actor из доверенного control header остаются
в основном Discord process. HTTP payload не может выбирать guild, произвольный
endpoint или содержимое audit event. Authorization использует два аддитивных
источника роли: каждый ID из `WEB_ADMIN_ALLOWED_USER_IDS` всегда OWNER, active DB
grant даёт ADMIN только пользователю вне OWNER set. Затем обе роли обязаны пройти
SELECT-only current non-bot configured-guild membership check; OWNER с DB grant
не понижается. OWNER-only `/admin/administrators` читает
OWNER identity и active grants только bounded SELECT, исключает OWNER из
removable ADMIN и не показывает ссылку ADMIN. Каждый grant/revoke требует
session, CSRF, fresh OWNER decision и rate limit; target/actor/guild не берутся из
browser, кроме единственного target `user_id`. Mutation выполняет существующий
loopback Bot Control client; наличие scoped Rules write path эту границу не меняет.

OWNER-only `GET /admin/audit` перед каждым чтением повторяет fresh authorization
по Discord ID из server-side session; роль session используется только для
навигации. Единственный bounded SELECT ограничен configured guild, категорией
`web_admin`, точным allowlist событий `web_admin.access_granted` и
`web_admin.access_revoked`, сортировкой `occurred_at DESC, id DESC` и `LIMIT
100`. Actor и target обогащаются только безопасной persisted identity с fallback
до Discord ID. Страница не читает и не показывает raw JSON, delivery/retry поля
или детали ошибок и не имеет mutation path; отказ authorization и ошибка чтения
обрабатываются fail-closed.

### Web Admin server settings

Stage 6B.1 добавляет guild-specific таблицу `guild_server_settings` с одной
строкой на guild и FK на `guilds.id`. Четыре независимых setting-а — autorole
role, общий audit channel, anniversary channel и return channel — представлены
парой `*_mode`/`*_id`. Режим `env` использует соответствующий
`DISCORD_*_ID` baseline, `value` требует положительный ID и имеет приоритет над
env, а `disabled` даёт effective `None`; CHECK constraints не позволяют хранить
ID вне `value`. Отсутствие строки разрешается как четыре `env`, поэтому пустая
таблица полностью сохраняет прежнее поведение.

Единый Discord-independent resolver возвращает effective ID и source
`env`/`db`/`disabled`. Bot process использует общий refreshable provider: первый
lookup читает одну строку, а успешная Bot Control mutation после commit
инвалидирует cache и будит delivery runner. Autorole, audit ingestion/delivery и
retention, anniversary worker и member-return handler существуют независимо от
startup baseline и проверяют provider в момент действия; disabled path не
обращается к Discord API. Это позволяет включать, менять и выключать setting без
restart, не вводя startup-only cache без invalidation.

Write-side доступен только как строгий `POST /control/v1/server-settings` с
allowlisted `setting`, режимом `env`/`value`/`disabled` и ID только для `value`.
Payload не принимает `guild_id`, actor приходит только из authenticated control
header. Discord process fail-closed проверяет readiness и configured guild;
autorole target обязан быть существующей non-default/non-managed ролью ниже
highest bot role при `Manage Roles`, channel target — text/news channel с View,
Send Messages и Embed Links. Repository не владеет транзакцией; control service
в одной transaction сохраняет override и important history-only
`web_admin.server_setting_changed`, а no-op не пишет audit. Event имеет
`subject_type=guild_setting`, actor ID и bounded key/source/value data без raw
request или env names.

Server Settings read service остаётся SELECT-only и
одним guild-scoped lookup возвращает для всех четырёх setting effective ID и
source; env baselines доступны через общий `RuntimeSettings`. Stage 6B.2
добавляет fresh-authorized OWNER/ADMIN `GET /admin/server-settings` и отдельную
strict CSRF-protected форму на каждый setting. Browser выбирает только
allowlisted setting и один из `env`/`value`/`disabled`; rate limit применяется
после fresh authorization. Actor берётся из server-side session, а `guild_id`,
actor, control URL и shared secret form не принимает. Успех использует PRG и
различает real change и штатный no-op; audit по-прежнему создаёт только bot
process.

Локальная schema не хранит current Discord roles или полный список text/news
channels: `voice_channels` содержит только Voice/Stage. Поэтому bearer-protected
read-only `GET /control/v1/server-settings/options` получает bounded targets из
cache configured guild работающего `discord.Client` и не принимает guild из
HTTP. Roles ограничены non-default/non-managed объектами ниже highest bot role
при `Manage Roles`; channels ограничены text/news с View Channel, Send Messages
и Embed Links. Web Admin запрашивает options только server-to-server через
loopback client; browser не обращается к Bot Control и не получает secret.
Current effective ID, отсутствующий в options, отображается без raw ID как
недоступный, а write-side повторно выполняет Discord validation для защиты от
гонки между GET и POST.

OAuth transactions и web sessions — bounded in-memory stores одного процесса с lazy expiry cleanup. Transaction живёт 5 минут, требует совпадения query state и callback-specific HttpOnly cookie и атомарно потребляется до code exchange. Session cookie содержит только opaque random 256-bit ID; server record хранит Discord user ID, created/absolute expiry и CSRF token. Default lifetime — 8 часов без sliding renewal. Logout доступен только как CSRF-protected POST, атомарно отзывает record и удаляет cookie. Restart намеренно инвалидирует sessions и незавершённые flows; несколько workers, PostgreSQL sessions и миграции не нужны.

Deny-by-default middleware оставляет публичными только exact `GET /admin/login`, `GET /admin/auth/discord/callback` и `GET /admin/health`. Все существующие и будущие остальные `/admin/...` требуют session; unauthenticated safe methods получают `303 /admin/login`, unsafe methods — `401` без запуска OAuth. Health endpoint возвращает `200 healthy` только после успешного round trip к PostgreSQL и `503 unhealthy` при исключении. Детали исключений не попадают в HTTP response.

Первый domain screen `GET /admin/members?page=1&q=...` показывает текущие (`left_at IS NULL`) non-bot memberships по 50 строк. Pagination выполняет один filtered `COUNT` и один set-based page `SELECT`; voice/text/achievement sources присоединяются к полному `filtered_admin_members` scope до global allowlisted ordering, а `LIMIT/OFFSET` применяется после aggregates. Число SQL statements не зависит от размера страницы и N+1 отсутствует. Voice aggregate переиспользует eligibility builder команд: effective end открытого interval ограничен `confirmed_through_at/as_of`, AFK исключён, minimum exact session threshold применяется ко всей session, exact и estimated lifetime суммируются. Text — сумма постоянных `daily_text_activity.message_count`, achievements — число persisted `user_achievements`. `joined_at` отображается в UTC. Отображаемое имя вычисляется запросом как `nickname → global_name → username → Discord ID`; числовой поиск означает точный Discord ID, текстовый использует case-insensitive поиск по трём исходным identity-полям. HTML экранирует полученное имя.

`GET /admin/members/{discord_id}` показывает active и departed non-bot membership только configured `DISCORD_GUILD_ID`. Первый set-based SELECT строит single-member scope, до achievement join сводит voice/text к одной строке и возвращает profile с одной строкой на persisted achievement; voice использует тот же scoped eligibility builder, что список и Discord statistics. Второй SELECT ограничен 20 lifecycle rows `member.joined`/`member.left`/`member.returned` с ordering `occurred_at DESC, id DESC`. Raw audit JSON не отображается: для return разрешены только проверенные non-negative `absence_seconds` и positive `return_number`. Not-found выполняет только первый SELECT, успешный profile — ровно два; DB failure становится безопасным HTTP 503.

### Web Admin Members & Profiles WUI-4A

WUI-4A сохраняет routes, authorization и persisted data contracts Members/Profile,
но заменяет технические таблицы специализированным server-rendered directory и
profile presentation. Username отображается отдельным `@username`, а precedence
основного имени остаётся `nickname → global_name → username → user_id`.

Directory принимает только allowlisted `sort=name|joined|voice|messages|achievements`
и `order=asc|desc`; invalid values возвращаются к `name/asc`. Search/current
non-bot scope строится до aggregates. Один filtered `COUNT` сохраняет pagination,
а второй set-based SELECT присоединяет lifetime Voice/Text/Achievement totals ко
всему отфильтрованному scope, применяет выбранный SQL ordering с обязательным
`user_id ASC` tie-break и только затем `LIMIT/OFFSET`. Поэтому aggregate sorting
является глобальным, а не сортировкой текущих 50 rows; N+1 и загрузки всего набора
в Python нет. Цена aggregate sort — вычисление totals для всех совпавших current
members перед pagination, что соответствует небольшому/среднему целевому guild и
не требует schema/index migration; дальнейшая оптимизация требует production
`EXPLAIN (ANALYZE, BUFFERS)`.

Profile продолжает использовать существующие два bounded SELECT и не меняет
lifetime Voice, achievement evaluation или lifecycle allowlist. Persisted identity
показывается block-level полями, membership — только как сохранённые `joined_at` и
`left_at` без утверждений о первом вступлении или общем стаже. Achievements
отображаются human-facing cards с archived fallback, lifecycle — vertical timeline
с русскими labels и безопасным fallback event type. Все persisted strings проходят
HTML escaping. Scoped responsive CSS перестраивает toolbar, directory rows, hero,
KPI, achievements и timeline на 640/430 px и сохраняет structural safety от 320 px;
JavaScript, external assets, Discord API, token access и CSP изменения отсутствуют.
Персональная Member Analytics 7d/30d намеренно остаётся отдельным WUI-4B.

### WUI-4A.1 Member Avatars

WUI-4A.1 расширяет существующую persisted Discord identity без отдельной avatar
подсистемы: `discord_users.avatar_hash` хранит nullable global Discord avatar
asset hash, а `guild_members.guild_avatar_hash` — nullable guild-specific member
avatar hash. Бинарные изображения, произвольные remote URL и история версий
аватаров не сохраняются. Оба поля добавлены линейной Alembic migration
`d4e8a1c7b962` без backfill; legacy `NULL` безопасно отображается monogram.

Full startup provisioning, targeted message/voice provisioning и существующие
`member_join`, `member_update`, configured-guild `user_update` обновляют avatar
source facts через те же set-based reference upserts. Полный `discord.Member`
может обновить либо очистить guild avatar; partial `discord.User` обновляет
global avatar, включая достоверное удаление в `NULL`, но не меняет persisted
guild avatar и `left_at`. `member_remove` в одной транзакции сначала сохраняет
последний полный global/guild snapshot и только затем выставляет `left_at`, поэтому
departed profile показывает last-known persisted avatar без identity-history table.

Web Admin получает оба hash в существующих directory/detail statements без N+1
и без Discord/Bot Control API. Чистый presentation helper принимает только
positive persisted guild/user IDs, allowlisted Discord hash form и bounded image
size, затем строит URL исключительно на `https://cdn.discordapp.com`. Precedence:
guild avatar → global avatar → существующий Unicode-safe CSS monogram. Directory
использует native lazy loading, оба экрана задают размеры и async decoding;
JavaScript, inline event handlers и CSP изменения отсутствуют.

App factory и отдельные service/query boundaries позволяют расширять read-only
экраны, включая реализованный `/admin/audit` и будущий `/admin/statistics`, без
расширения bot control interface.

OAuth `identify` остаётся только authentication («кто вошёл?»), а authorization отдельно отвечает «можно ли войти?» через OWNER env IDs либо managed ADMIN grant и обязательный current guild membership. Stage 5 разрешает публикацию только через HTTPS reverse proxy: same-host deployment сохраняет loopback bind, remote proxy использует один explicitly opted-in private bind и firewall allow rule. Bot control остаётся безусловно loopback-only.

### Web Admin public deployment security

Uvicorn запускается с `proxy_headers=False`: redirect URI, cookie Secure и access
policy не зависят от browser-controlled forwarded headers. Central ASGI middleware
добавляет ко всем `/admin` responses `nosniff`, `DENY` framing, CSP без scripts,
no-referrer, minimal Permissions Policy и no-store. HSTS принадлежит TLS reverse
proxy.

Публичная поверхность reverse proxy ограничена точным redirect `/` на
`/admin/` и proxy-маршрутом `/admin/*`; любой другой path завершается на proxy
ответом `404`. Это одинаковый контракт для официальных Nginx и Caddy examples и
не включает Bot Control либо произвольные application routes.

Public `/admin/health` выполняет PostgreSQL probe, но раскрывает только общий status.
Bot-profile POST после session и CSRF непосредственно перед control I/O повторяет
allowlist/current membership authorization. DB error и denial fail closed, revoke
session и не вызывают control API. Process-local sliding limiter допускает 10 writes
за 60 секунд на session и хранит максимум 1024 ключа. Network login/callback/write
limits остаются обязанностью reverse proxy.

Поддерживаемые схемы и security boundary зафиксированы в
`docs/WEB_ADMIN_DEPLOYMENT.md`; примеры находятся в `deploy/caddy`, `deploy/nginx` и
`deploy/systemd`.

### Logging

- Используется стандартный модуль Python `logging`.
- Логи направляются в stdout/stderr для сбора service manager-ом (в основном
  `journald`).
- Локальные application log-файлы не используются.
- Уровень логирования задаётся через `LOG_LEVEL`.
- Discord token, database credentials и другие секреты не должны попадать в логи.

### Границы MVP

Целевые границы MVP включают:

- voice-статистику;
- текстовую активность без хранения содержимого сообщений;
- базовую статистику участников;
- slash-команды статистики.

Первый законченный этап Audit Logging включает нормализованную историю участников, voice-переходов, каналов, ролей и ban/unban. Message/reaction/presence/invite/thread logging, actor correlation и `/history` остаются вне текущего этапа.

### Приватность и границы сбора данных

- Хранятся статистические метаданные, а не содержимое сообщений.
- Запрещено хранить `content` сообщений, текст вложений, URL и содержимое embeds.
- `MESSAGE_CONTENT` не используется.
- Runtime не читает `Message.content`, `Message.attachments` или embeds;
  live-сбор `attachment_count` намеренно оставлен равным нулю.
- Presence используется только optional Game Tracking и только для минимальной
  Playing identity; status/platform/Custom/Spotify/Rich Presence payload не
  сохраняются. Typing tracking и DM tracking не используются.
- Боты исключаются из пользовательской статистики.
- Возможность opt-out и удаления пользовательских данных должна быть предусмотрена архитектурно для дальнейшей реализации. Конкретные правила и пользовательские сценарии ещё не определены.

### Модель voice-статистики

- Всё время подключения пользователя к voice учитывается независимо от `self_mute` и `self_deaf`.
- Состояния mute, deaf, stream и video не входят в первую business-схему и могут быть добавлены отдельной migration позже.
- Используется логическая voice-сессия, состоящая из атомарных интервалов.
- Каждый интервал имеет `started_at`, `ended_at`, `channel_id` и показатель качества (`quality`).
- `quality` различает значения `exact` и `estimated`; открытый интервал определяется через `ended_at IS NULL`, а не отдельным значением качества.
- При изменении channel текущий интервал закрывается и открывается новый.
- Переход между каналами не прерывает логическую сессию.
- Discord disconnect/leave закрывает текущий интервал и логическую сессию.
- Reconnect grace period в первой версии отсутствует: новое подключение после disconnect создаёт новую логическую сессию.
- Основная пользовательская статистика и leaderboard по умолчанию учитывают только exact-время; estimated-время рассчитывается и показывается отдельно.
- Поддерживаются как общее voice-время, так и статистика по отдельным каналам.
- AFK учитывается отдельно и не включается в основной voice leaderboard.
- Stage учитывается отдельно.
- Для основной статистики правило минимальных 10 секунд применяется к суммарной длительности exact-интервалов логической сессии, а не к отдельным интервалам.
- Estimated duration считается отдельно, не участвует в прохождении этого порога и не добавляется к обычному voice time.
- Короткие сессии физически сохраняются; минимальный порог применяется при статистических запросах.
- Длительность вычисляется по `started_at` и `ended_at`, а не накапливается таймером.
- Все timestamps сохраняются в UTC.

### Disconnect, перезапуск и качество данных

- При запуске выполняется reconciliation сохранённого состояния с текущими voice-состояниями Discord.
- Точные (`exact`) и оценочные (`estimated`) интервалы различаются в данных и отчётах.
- Если после восстановления пользователь находится в том же канале, простой может быть восстановлен как `estimated`.
- Если канал или момент перехода во время downtime неизвестен, система не создаёт вымышленное точное время перехода.
- Оценочные данные никогда не представляются пользователю как точные.

### Game Tracking backend v1

Game Tracking является opt-in bot-only feature. `GAME_TRACKING_ENABLED=false`
не создаёт handler/reconciler/checkpoint worker и оставляет privileged
`GUILD_PRESENCES` intent выключенным. При `true` intent добавляется в
централизованном `create_gateway_intents`; Presence Intent оператор заранее
включает вручную в Discord Developer Portal. Migration и feature flag независимы.

Discord adapter копирует из Presence только Activity type, display name и
optional `application_id`; application service допускает только `Playing`.
Online/idle/dnd, client platform, Custom Status, Listening/Spotify, Streaming,
Watching, Competing, details/state, party/join secrets и raw Presence payload не
попадают в persistence. Если `application_id` есть, identity равна ему; иначе
используется Unicode-normalized/casefold display name. Оригинальное очищенное имя
хранится отдельно. При нескольких Playing activities текущий persisted key имеет
приоритет, иначе выбор детерминирован; reorder не создаёт switch.

`game_sessions` хранит `id`, guild/member FK, derived `game_key`, display
`game_name`, nullable positive `application_id`, `started_at`,
`confirmed_through_at` и nullable `ended_at`. Все времена — `TIMESTAMPTZ`/UTC.
Partial unique index `(guild_id, user_id) WHERE ended_at IS NULL` является
последней защитой одной открытой игры; history читается индексами
`(guild_id, user_id, started_at)` и `(guild_id, started_at)`. Каталога игр,
денормализованных totals и retention в Stage 3 нет.

Live Presence transition работает в одной caller-owned transaction и блокирует
строку `guild_members FOR UPDATE`. Same key только продвигает confirmation и
может обновить cosmetic display name; stop закрывает session на event time;
switch атомарно закрывает старую и открывает новую. Stale/duplicate observations
идемпотентны. Wrong guild и bots отбрасываются до transaction; DB failure
логируется и не выходит в Discord event loop. Member leave закрывает текущую
session тем же service path.

Checkpoint раз в `GAME_CONFIRM_INTERVAL_SECONDS` использует только Gateway cache,
не вызывает Discord API и одним set-based `UPDATE` подтверждает persisted open
sessions, чей `game_key` всё ещё присутствует. Отсутствие игры в checkpoint само
по себе не выводит stop: это делает Presence event либо authoritative recovery.

Initial READY и recovery после фактического `on_disconnect` выполняются под
существующим serialized recovery lock и generation guard. Один locked SELECT
получает все open sessions configured guild, один batch UPDATE закрывает старые
ровно на их `confirmed_through_at`, затем current cached games открываются с
общего startup `R`. Даже та же игра после crash получает новую session, поэтому
неизвестный downtime не считается игровым временем. Повторный READY/RESUME без
новой disconnect generation не режет sessions; после disconnect используется
консервативное новое разбиение. Repeated equal-time reconciliation сохраняет уже
созданный open snapshot. Clean shutdown после остановки periodic task выполняет
один bounded финальный checkpoint.

Game statistics G1 добавляет Discord-independent read service поверх той же
`game_sessions` history. Repository выбирает только sessions пользователя,
пересекающие rolling UTC window `7d`/`30d`/`90d` либо all-time; open session
ограничивается `confirmed_through_at`. Raw `game_key`/`application_id` остаются
tracker identity и не переписываются, но read service не использует их для
пользовательской группировки: canonical key строится из trimmed/casefold
`game_name`, а известный exact trailing suffix ` with Medal` удаляется
case-insensitively как presentation integration normalization. Service
clipping'ует границы, агрегирует total и TOP-5 по canonical key, считает
уникальные игры, longest session, latest game и календарные игровые дни в
`REPORT_TIMEZONE`. Новых таблиц, денормализованных totals и migration нет.

G3B Server Game Analytics добавляет отдельный historical read path для
`GET /admin/games`, не расширяя semantics существующего `/admin/analytics`.
Отдельный period type строит DST-safe окна 7/30/90 завершённых локальных дней в
`REPORT_TIMEZONE`; сегодняшний незавершённый день исключён. Один bounded
set-based SELECT получает только пересекающие окно confirmed sessions
configured guild вместе с persisted display names, второй aggregate SELECT
возвращает earliest confirmed activity для честного coverage caveat. Оба чтения
выполняются в одном `REPEATABLE READ, READ ONLY` snapshot; N+1, Discord cache и
live Presence не используются.

Server-wide domain service clipping'ует открытые sessions на
`confirmed_through_at`, считает person-time, unique gamers/games, average,
zero-filled daily points и deterministic TOP-10 игр/игроков. Member G1/G3A и
G3B используют единый canonicalization helper, включая casefold, display-name
selection и suffix `with Medal`; фактическое поведение member statistics не
изменено. TOP player identity разрешается тем же set-based session query из
persisted `guild_members`/`discord_users`. Новых таблиц, materialized aggregates,
background jobs и migration G3B не добавляет.

### Текстовая активность

```text
Discord MESSAGE_CREATE
→ TextActivityEventHandler
→ targeted reference provisioning
→ TextActivityService
→ TextActivityRepository
→ daily_text_activity
```

Persistence foundation хранит только постоянные дневные агрегаты в
`daily_text_activity`: одна строка на `guild_id + user_id + channel_id +
activity_date`. Содержимое сообщения, `message_id`, имя/URL вложения и другие
per-message данные не сохраняются.

`message_count` считает сообщения, `reply_count` — сообщения-reply, а
`attachment_count` на уровне domain/persistence означает суммарное число Discord
attachments. Один PostgreSQL `INSERT .. ON CONFLICT DO UPDATE` атомарно
увеличивает все три счётчика; service получает timezone-aware event timestamp,
нормализует его в UTC и определяет `activity_date` по `REPORT_TIMEZONE`, не по
timezone машины. Live Gateway adapter намеренно передаёт `attachment_count=0` и
не читает `Message.attachments`: при выключенном privileged `MESSAGE_CONTENT`
поле attachments для обычных guild messages нельзя считать надёжным. Колонка и
domain input остаются зарезервированы для возможного будущего осознанного режима.

Агрегат ссылается составным FK на существующий `guild_members`. Существующая
reference-таблица `voice_channels` ограничена Voice/Stage и не подходит для
текстовых каналов; поэтому `channel_id` хранится как положительный Discord
snowflake без FK. Это не создаёт дублирующую channel entity и сохраняет историю
после удаления канала. Общая reference-модель всех Discord channels потребовала
бы отдельной миграции и согласованного рефакторинга voice persistence.

`on_message` принимает только guild messages типов `default` и `reply` от
non-bot, non-webhook авторов configured guild. DM, другой guild и системные
message types отбрасываются до DB session. Для thread сохраняется ID самого
thread как фактического `message.channel`; parent channel не подставляется.
Targeted guild/user/member provisioning и запись агрегата выполняются одной
caller-owned транзакцией, а persistence failure логируется только с
guild/user/channel ID и не выходит из Gateway handler.

Публичная `/topmessages [period]` использует choices `today`, `7d`, `30d`,
`all` с default `7d`. В отличие от rolling timestamp voice-окон text periods
являются включительными календарными датами `REPORT_TIMEZONE`: today — одна
дата, 7d — сегодня и 6 предыдущих дат, 30d — сегодня и 29 предыдущих, all —
вся история до текущей локальной даты. Один aggregate query исключает bots,
группирует по `user_id`, сортирует по `message_count DESC, user_id ASC` и
ограничивает результат TOP 10.

Редактирование и удаление сообщений не корректируют агрегаты: без хранения
per-message ID дедупликация повторно доставленных Gateway events и обратная
коррекция невозможны. Это остаётся осознанным MVP trade-off.

### Участники и отображение статистики

- Member tracking включает события join и leave.
- Для member tracking используется privileged intent `GUILD_MEMBERS`.
- Leaderboard может быть публичным внутри сервера.
- Персональная команда `/stats` всегда использует ephemeral-ответ и может показать вызвавшего или явно выбранного non-bot участника configured guild.
- Правила доступа к другим персональным отчётам и влияние будущего opt-out на leaderboard ещё не определены.

### Retention

Значения по умолчанию:

| Категория | Retention |
| --- | --- |
| Voice-сессии | Постоянно |
| Дневные текстовые агрегаты | Постоянно |
| Server events | 1 год |
| Transient audit events | 90 дней |
| Important audit events | Постоянно (`expires_at = NULL`) |

Retention остаётся настраиваемым архитектурно. `RAW_MESSAGE_RETENTION_DAYS`
пока остаётся зарезервированной legacy-настройкой и не применяется к
`daily_text_activity`; в конфигурационный контракт также входят
`SERVER_EVENT_RETENTION_DAYS` и `AUDIT_TRANSIENT_RETENTION_DAYS`. Audit cleanup
запускается при старте своего runner и затем примерно раз в сутки. Расписание
очистки ещё не реализованных server-event таблиц будет определено отдельно.

- Retention-очистка реализуется как отдельная application/background task.
- В MVP эта задача может запускаться периодически внутри процесса бота.
- Задача использует утверждённые настраиваемые retention-правила и не должна смешиваться с Discord handlers.

### Durable Audit Logging

Audit Logging реализован отдельным feature-first контуром: Discord Gateway adapter создаёт Discord-независимый `AuditEventDraft`, application service рассчитывает retention, а repository сохраняет нормализованные поля в `audit_events` внутри caller-owned transaction. Только после успешного commit ingestion будит delivery runner. Discord API никогда не ожидается внутри транзакции записи события.

PostgreSQL является source of truth для audit history; настроенный Discord-канал — presentation/delivery. Pending выбираются только для настроенного `guild_id`, oldest-first и ограниченным batch; partial index начинается с `guild_id`, затем содержит retry/order keys. Успешная отправка фиксирует `discord_message_id` и `delivered_at`; ошибка увеличивает `delivery_attempts`, сохраняет краткий `last_delivery_error` и назначает следующий retry по bounded backoff `5, 15, 30, 60, 120, 300` секунд. Гарантия at-least-once: crash между успешным Discord send и commit delivery state может дать duplicate, но закоммиченное событие не теряется из-за недоступности Discord. Внешний queue/Redis и distributed lock для одного экземпляра не используются.

Чистые `channel.updated` с единственным изменением `position` остаются отдельными строками `audit_events`, но presentation runner ждёт 1,5 секунды после последнего события одного parent/category scope и отправляет связанный reorder одним embed. Остальные audit events debounce не ожидают. После успешного Discord `send` все ID группы получают один `discord_message_id` и `delivered_at` одним SQL update внутри одной транзакции; при ошибке attempts/error/next retry так же обновляются для всей группы атомарно. Поэтому batch сохраняет общую at-least-once семантику: crash-gap может повторить весь embed, но не создаёт частично delivered группу и не удаляет исходную историю.

`audit_events` не имеет FK на удаляемые Discord channel/role/user current-state rows. `guild_id`, `event_type`, `category`, subject/actor/channel IDs и JSONB `before_data`/`after_data`/`details_data` сохраняют source data; presentation после restart строится из `AuditEventRecord`, а cache используется лишь как необязательное улучшение. `actor_user_id` подготовлен, но Audit Log actor enrichment намеренно не реализован. DB enums не используются, чтобы расширять event types без migration.

Voice tables остаются единственным source of truth для длительности: live voice transition сначала коммитит `voice_sessions`/`voice_intervals`, после чего audit enrichment находит interval, закрытый tracker-ом на точном `occurred_at`, и связанную logical session. Move закрывает только предыдущий interval и не завершает session. Для leave суммарное «Сегодня» рассчитывается существующим statistics aggregate с локальной полуночью `REPORT_TIMEZONE`, общим `VOICE_MIN_SESSION_SECONDS`, AFK и exact/estimated semantics. Полученные duration, session start и Gateway snapshot non-bot channel member count записываются в `audit_events.details_data` как immutable historical presentation snapshot; delivery/retry читает только persisted record и не пересчитывает значения по текущему Discord/DB state. Если enrichment недоступен или старое событие не содержит новых ключей, сохраняется и отображается базовое voice event без приблизительных метрик.

Voice audit enrichment добавляет только фиксированную стоимость на реальные channel transitions при включённом Audit Logging: join не выполняет дополнительный voice SQL, move использует один timing query, leave — timing query и уже существующий user statistics aggregate. Mute/deaf/stream/video updates не создают audit event и не запускают enrichment.

Transient voice/user-avatar/username/nickname/guild-avatar события получают `expires_at = occurred_at + AUDIT_TRANSIENT_RETENTION_DAYS`; important join/left/roles/timeout/channel/role/moderation события имеют `expires_at = NULL`. Cleanup выполняется при старте runner и затем примерно раз в сутки. Аватары хранятся только как asset key/URL, без бинарных данных. Voice statistics tables остаются отдельным долговременным источником аналитики и не зависят от audit retention.

Audit subsystem является secondary feature: ingestion, serialization, delivery, rendering и cleanup failures логируются и не останавливают Gateway, slash-команды, checkpoint или persistence lifecycle. В `on_voice_state_update` существующий critical voice tracking сохраняет прежнюю propagation semantics: его exception не проглатывается. Audit normalization выполняется в `finally`; обычная audit-ошибка логируется и не заменяет исходное critical exception. Cancellation не перехватывается как audit failure. Обычные audit messages используют `AllowedMentions.none()`; routed-поздравление разрешает mention только конкретного участника.

### Автоматические годовщины участников

Автоматическая проверка переиспользует `MemberAnniversaryService`, guild member cache и календарь `REPORT_TIMEZONE`; отдельной логики дат нет. Worker запускает одну проверку сразу после первого READY, затем каждый цикл заново вычисляет задержку до 00:05 следующей локальной даты. Поэтому фиксированный `sleep(86400)` и накопление дрейфа не используются. Повторные READY/RESUME не создают второй task, а `Client.close()` отменяет и ожидает существующий worker.

Найденные годовщины сохраняются в существующий `audit_events` durable outbox как important `member.anniversary`. Partial unique index по `(guild_id, subject_id, occurred_at)` только для этого event type, где `occurred_at` является UTC-представлением локальной полуночи годовщины, и PostgreSQL `ON CONFLICT DO NOTHING` обеспечивают одну очередь на пользователя и календарную годовщину после повторной проверки, reconnect или restart. Новая таблица не создаётся; migration добавляет только индекс.

Один общий delivery runner выбирает только включённые event types и маршрутизирует `member.anniversary` в `DISCORD_ANNIVERSARY_CHANNEL_ID`, независимо от включения Audit Logging. Успешный send фиксирует `discord_message_id`/`delivered_at`; Discord/API error сохраняет attempts/error/next retry и не мешает доставке остальных участников. Стабильный outbox ID передаётся как Discord nonce. Как и любой send + DB mark без распределённой транзакции, механизм имеет честную at-least-once гарантию: обычные повторные проверки и перезапуски не дублируют доставленную запись, но авария строго после принятия сообщения Discord и до commit `delivered_at` остаётся теоретическим crash-gap.

### Возвращения участников

`guild_members` остаётся reference/current-state таблицей: provisioning обновляет `joined_at` фактической датой текущего Discord membership и не удаляет lifetime voice/text/achievement данные. Источником истории уходов служат permanent `member.left` в `audit_events`. Если return channel включён без общего audit channel, ingestor сохраняет только `member.joined`/`member.left`, не включая остальной Audit Logging; эти history-only строки сразу получают `delivered_at` без `discord_message_id`, поэтому последующее включение audit-канала не отправит накопленную историю.

На `on_member_join` отдельный handler пропускает bots и `joined_at=None`, находит последний `member.left` до фактического Discord `joined_at` и сравнивает точную длительность отсутствия с `MEMBER_RETURN_MIN_ABSENCE_SECONDS`. Для прошедшего порог возвращения он в одной caller-owned transaction читает существующие all-time voice aggregates с общим `VOICE_MIN_SESSION_SECONDS`, permanent daily message aggregates до локальной даты `REPORT_TIMEZONE`, число `user_achievements` и количество предыдущих leave events. Эти значения и обе временные границы сохраняются immutable snapshot в `details_data`.

`member.returned` является important outbox event. Partial unique index и PostgreSQL `ON CONFLICT DO NOTHING` имеют одинаковые `(guild_id, subject_id, occurred_at)` и `WHERE event_type = 'member.returned'`, где `occurred_at` равен текущему Discord `joined_at`. Delivery runner маршрутизирует событие только в `DISCORD_RETURN_CHANNEL_ID`, использует persisted user ID для mention и обычные retry/backoff/delivered semantics. Нового background worker нет: обработка событийная, а общий delivery worker уже принадлежит runtime lifecycle.

### Autorole

Autorole является отдельным optional feature без persistence. `AutoroleService` ограничивает обработку configured guild, исключает bots и уже назначенную роль; Discord adapter использует guild cache, отклоняет отсутствующую, default или managed роль, проверяет `Manage Roles` и положение highest role Kanami выше целевой роли, затем единожды вызывает `member.add_roles` с audit-friendly reason. `Forbidden`/`HTTPException` логируются без retry.

Существующий `on_member_join` последовательно и независимо запускает `member.joined` Audit Logging и Autorole. Ошибка одной secondary feature не блокирует другую и не выходит из callback. Autorole не создаёт `member.roles_updated` вручную: успешный Discord role update поступает через штатный Gateway `on_member_update` и нормализуется существующим Audit Logging.

### Achievements

Achievement domain остаётся Discord-независимым, а пользовательский adapter
подключён отдельной guild-only командой `/achievements [user]`.

```text
Discord command + metric adapter
→ AchievementMetricSnapshot
→ AchievementEvaluator
→ AchievementUnlockService
→ AchievementRepository
→ user_achievements
```

Immutable code catalog передаётся pure evaluator вместе с typed snapshot
`voice_seconds`/`server_age_days`/`message_count`; `None` означает недоступную
метрику. Evaluator не зависит от discord.py, voice repository, Text Activity
repository или audit repositories и возвращает удовлетворённые definitions в
порядке catalog.

Разблокировки хранят только `(guild_id, user_id, achievement_key, unlocked_at)`.
Composite primary key и PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` дают
атомарную идемпотентность в caller-owned transaction; `RETURNING` сообщает лишь
новые unlock текущего вызова. Titles/descriptions остаются в code catalog.
Unknown persisted key не удаляется и возвращается repository как есть для
presentation fallback. Команда в одной caller-owned транзакции переиспользует
`VoiceStatisticsService` для all-time `voice_seconds`, получает полные дни
членства из Discord `Member.joined_at`, идемпотентно сохраняет новые unlock и
читает итоговый список. Unknown persisted keys учитываются только как архивные и
не раскрываются пользователю как технические ключи. Embed ограничивает число и
общий объём полей согласно лимитам Discord. Notifications и автоматическая
выдача вне вызова команды не реализованы.

### Member Profile v1

`/profile [user]` использует тонкий Discord handler, отдельную reusable policy и
Discord-независимый `MemberProfileService`. Handler до обращения к persistence
проверяет configured guild, bot target и право просмотра: собственный профиль
доступен всегда, чужой — только участнику с configured Purple или Gold Role ID.
Сравнение с именами ролей и hardcoded production IDs запрещено; пустая
privileged-конфигурация работает fail-closed.

Service принимает primitive member snapshot (identity, `joined_at`, role IDs и
avatar URL), переиспользует один существующий voice aggregate для all-time/30d
и bounded `AchievementRepository.list_unlocked()`, затем возвращает единый DTO.
Два read выполняются в одном `REPEATABLE READ` session snapshot. Дата вступления
переводится в `REPORT_TIMEZONE`, длительность членства считается полными днями;
неизвестная дата или незаполненная role hierarchy не ломают профиль. Discord
renderer отвечает только за компактный ephemeral embed и общий формат voice
duration. Новых таблиц, migration, background worker, XP или автоматической
progression `Гость → Посвящённый → Страж` Profile v1 не добавляет.

### База данных и время

- Основная БД — PostgreSQL.
- Для доступа к БД используется SQLAlchemy в async-режиме.
- PostgreSQL driver — asyncpg.
- Все изменения схемы БД выполняются через Alembic.
- Изменения схемы без миграции запрещены.
- Alembic-миграции выполняются отдельным deployment step или отдельной командой.
- Обычный запуск приложения не выполняет миграции автоматически.
- Утверждённая MVP-схема, связи, основные ограничения и транзакционные границы описаны ниже; business models и migration revisions ещё не реализованы.
- Все timestamps в БД хранятся в UTC.
- Часовой пояс отчётов настраивается отдельно и не меняет способ хранения времени.

### Утверждённая MVP-схема persistence

Статус решения: принято. Дата фиксации: 2026-08-11.

Первая business-схема состоит из шести таблиц:

1. `guilds` — Discord guild/server;
2. `discord_users` — глобальная Discord-учётная запись;
3. `guild_members` — участие пользователя в конкретном guild;
4. `voice_channels` — voice или stage channel;
5. `voice_sessions` — одно логическое непрерывное подключение пользователя к voice;
6. `voice_intervals` — участок session, проведённый в одном конкретном канале.

Миграция text activity foundation добавляет седьмую business-таблицу
`daily_text_activity` с составным primary key
`(guild_id, user_id, channel_id, activity_date)`, FK на `guild_members`,
неотрицательными `BIGINT`-счётчиками и одним индексом
`(guild_id, activity_date, user_id)` для будущих period leaderboard запросов.

`discord_users` и `guild_members` остаются раздельными сущностями: пользователь является глобальным объектом Discord, а membership принадлежит паре guild/user. `discord_users.username`/`global_name` и `avatar_hash` хранят глобальную mutable identity, а `guild_members.nickname` и `guild_avatar_hash` — guild-specific mutable identity. Вычисляемый `display_name`, image bytes и avatar history не сохраняются. Это сохраняет нормальную модель данных и не препятствует будущей поддержке нескольких guild.

Линейная migration `a8d3e5f7b912` добавляет username/global name/nickname, а `d4e8a1c7b962` — два nullable avatar hash; обе не выполняют SQL backfill. Следующий startup/reconnect full reference provisioning заполняет существующие production rows из Discord cache; join/member update/user update, а также targeted message/voice provisioning обновляют identity. Remove атомарно сохраняет последний полный member snapshot и выставляет `left_at`, а следующий join/full provisioning сбрасывает `left_at` в `NULL`. Identity sync выполняется независимо от Audit Logging и failure-isolated от audit, member-return, autorole, voice и text paths.

Полный `discord.Member` распознаётся по наличию member-only атрибута `nick`: в таком snapshot `nickname=None` и `guild_avatar_hash=None` являются достоверными удалениями и напрямую записываются как SQL `NULL`. Snapshot из частичного `discord.User` помечается как не содержащий полной guild identity; его conflict update намеренно не включает `nickname`, `guild_avatar_hash` и `left_at`, поэтому отсутствующие поля не стирают сохранённую guild identity и не реактивируют membership. При этом глобальные `username`/`global_name`/`avatar_hash` обновляются напрямую без `COALESCE`, чтобы реальные удаления сохранялись как `NULL`.

#### Идентификаторы и время

- Discord snowflake ID хранятся как PostgreSQL `BIGINT` с проверкой положительного значения.
- Внешние Discord ID не генерируются БД.
- Внутренние `voice_sessions.id` и `voice_intervals.id` используют `BIGINT GENERATED BY DEFAULT AS IDENTITY`.
- Все timestamps используют PostgreSQL `TIMESTAMPTZ` и timezone-aware Python `datetime` в UTC.
- Duration отдельной колонкой не хранится и вычисляется по пересечению интервалов с запрошенным периодом.
- Для открытого exact-интервала запрос использует одно явно зафиксированное значение `as_of`, а не вычисляет различающиеся текущие времена для отдельных строк.
- PostgreSQL ENUM в первой версии не используется; ограниченные наборы значений задаются через `TEXT` и `CHECK`.

#### Минимальный состав таблиц

| Таблица | Поля первой версии |
| --- | --- |
| `guilds` | `id BIGINT` PK, `name TEXT NULL` |
| `discord_users` | `id BIGINT` PK, `is_bot BOOLEAN NOT NULL`, `username TEXT NULL`, `global_name TEXT NULL` |
| `guild_members` | `guild_id BIGINT`, `user_id BIGINT`, `joined_at TIMESTAMPTZ NULL`, `left_at TIMESTAMPTZ NULL`, `nickname TEXT NULL`, composite PK `(guild_id, user_id)` |
| `voice_channels` | `id BIGINT` PK, `guild_id BIGINT`, `name TEXT NULL`, `channel_kind TEXT`, `is_afk BOOLEAN NOT NULL` |
| `voice_sessions` | `id BIGINT` identity PK, `guild_id BIGINT`, `user_id BIGINT`, `started_at TIMESTAMPTZ`, `ended_at TIMESTAMPTZ NULL`, `confirmed_through_at TIMESTAMPTZ` |
| `voice_intervals` | `id BIGINT` identity PK, `session_id BIGINT`, `guild_id BIGINT`, `user_id BIGINT`, `channel_id BIGINT`, `started_at TIMESTAMPTZ`, `ended_at TIMESTAMPTZ NULL`, `quality TEXT`, `channel_kind TEXT`, `is_afk BOOLEAN NOT NULL` |

`channel_kind` и `is_afk` в `voice_intervals` являются снимками состояния канала. Они нужны, чтобы историческая AFK/Stage-статистика не менялась после перенастройки или удаления канала.

Mute/deaf/stream/video flags в первую версию `voice_intervals` не входят. Их добавление не должно менять базовую модель session/interval и оформляется отдельной migration.

#### Связи и ограничения

- `guild_members.guild_id` ссылается на `guilds.id`.
- `guild_members.user_id` ссылается на `discord_users.id`.
- `voice_channels.guild_id` ссылается на `guilds.id`.
- `voice_channels` имеет `UNIQUE (guild_id, id)` для составной ссылки из intervals.
- `voice_sessions.(guild_id, user_id)` ссылается на `guild_members.(guild_id, user_id)`.
- `voice_sessions` имеет `UNIQUE (id, guild_id, user_id)` для составной ссылки из intervals.
- `voice_intervals` связывается составным FK с той же session и парой guild/user.
- `voice_intervals.(guild_id, channel_id)` ссылается на канал того же guild.
- Для `joined_at`/`left_at`, `started_at`/`ended_at` и `confirmed_through_at` задаются `CHECK`-ограничения, запрещающие обратный порядок времени.
- `voice_intervals.quality` допускает только `exact` и `estimated`.
- `channel_kind` допускает только `voice` и `stage`.
- Estimated-интервал всегда закрыт; открытым может быть только exact-интервал.
- Partial unique index по `(guild_id, user_id) WHERE ended_at IS NULL` в `voice_sessions` обеспечивает не более одной открытой session пользователя в guild.
- Аналогичный partial unique index в `voice_intervals` обеспечивает не более одного открытого interval пользователя в guild.

Основные непроизводные индексы первой версии:

- `voice_sessions (guild_id, user_id, started_at)`;
- `voice_intervals (session_id, started_at)`;
- `voice_intervals (guild_id, user_id, started_at)`;
- `voice_intervals (guild_id, channel_id, started_at)`;
- `voice_intervals (guild_id, started_at)`.

Exclusion constraints для всех возможных пересечений закрытых интервалов, range types, партиционирование и PostgreSQL extensions в первую migration не добавляются. Корректность переходов обеспечивается транзакциями, сериализацией обработки участника и partial unique indexes.

#### Voice transitions и транзакционные границы

Операции для одной пары `(guild_id, user_id)` сериализуются внутри транзакции, например через `SELECT ... FOR UPDATE` строки `guild_members`. Partial unique indexes являются дополнительной защитой, а не заменой транзакционной логики.

- Не устаревший join без открытой session создаёт новую session и первый exact interval.
- Повторно полученное уже представленное текущее voice state является no-op.
- Изменение `channel_id`, `channel_kind` или `is_afk` атомарно закрывает текущий interval и создаёт новый exact interval с тем же timestamp в той же session. `channel_kind` и `is_afk` являются историческими snapshots, поэтому изменение классификации канала образует новую границу interval.
- Discord disconnect/leave атомарно закрывает текущий interval и session.
- При отсутствии открытой session последняя достоверная граница определяется как `MAX(voice_sessions.confirmed_through_at)` для guild/user: более старое событие игнорируется как stale, а равный timestamp разрешён.
- Не устаревший leave при отсутствии открытой session является идемпотентным no-op.
- Reconnect после закрытой session создаёт новую session; автоматического объединения коротких переподключений нет.

Reference provisioning является отдельной от `VoiceTrackingService` ответственностью. На каждом `on_ready` Discord adapter снимает доступный cache snapshot настроенного guild: guild, всех cached members и соответствующие global users, а также все cached voice/stage channels. Application service вызывает repository upsert в порядке внешних ключей `guild -> users -> members -> channels` внутри одной caller-owned транзакции. Upsert обновляет mutable snapshot-поля и переводит присутствующего cached member в текущее состояние `left_at = NULL`; отсутствующие в cache строки автоматически не удаляются и не деактивируются. Боты сохраняются как reference users с актуальным `is_bot`, но по принятому продуктовому правилу не передаются в voice reconciliation. `VoiceTrackingService` по-прежнему требует заранее созданные member/channel references и не создаёт их автоматически.

Live `on_voice_state_update` обрабатывается отдельным Gateway adapter после успешного startup provisioning/reconciliation. События ограничиваются настроенным guild, а Discord bots отбрасываются до открытия транзакции. Для каждого релевантного события timezone-aware UTC timestamp фиксируется один раз на входе в client callback, до возможного ожидания startup gate, и явно передаётся event handler-у. После полностью успешного reconciliation его единый timestamp `R` публикуется как startup baseline одновременно с открытием gate. Ожидавшие события с `observed_at <= R` отбрасываются как уже покрытые authoritative cache snapshot; события с `observed_at > R` применяются после открытия gate с исходным временем. `on_disconnect` немедленно сбрасывает gate/baseline. И полный `on_ready`, и успешный Gateway `on_resumed` вызывают один приватный recovery method под общим lock: reference provisioning, startup reconciliation, публикация нового baseline и открытие gate. Generation guard запрещает recovery, пересёкшемуся с более новым disconnect, открыть gate устаревшим результатом. При provisioning error, reconciliation exception или ненулевом `failed_count` baseline не публикуется и live gate остаётся закрытым. Для применяемого события открывается ровно одна caller-owned транзакция. В ней отдельный provisioning service минимально upsert-ит только текущие guild/user/member references и актуальный voice/stage channel, если пользователь подключён; затем существующий `VoiceTrackingService` получает `observe_connected` либо `observe_disconnected`. Полный guild cache на каждом событии не синхронизируется. Targeted provisioning и voice transition коммитятся или откатываются атомарно, при этом business semantics join/move/leave/stale/idempotency остаётся только в voice service. Ошибка события логируется с guild/user/channel context и не выходит из handler.

#### Restart и crash recovery

`voice_sessions.confirmed_through_at` хранит последнюю сохранённую границу достоверного наблюдения открытой session. Пока Gateway работает нормально, открытые sessions в дальнейшем будут периодически обновляться; начальный ориентир периода checkpoint — 60 секунд.

Периодический checkpoint реализован отдельным Discord adapter/orchestrator и не содержит собственной voice state machine. После успешного recovery и открытия live gate client поддерживает ровно один asyncio-loop с конфигурируемым периодом `VOICE_CHECKPOINT_INTERVAL_SECONDS`. Каждый цикл один раз фиксирует timezone-aware UTC timestamp и снимает из Gateway cache подключённых неботов VoiceChannel/StageChannel вместе с AFK metadata. Для каждого пользователя открывается отдельная caller-owned transaction и вызывается существующий `VoiceTrackingService.observe_connected`: совпадающий snapshot только продвигает `confirmed_through_at`, изменение snapshot использует штатную live move-семантику, отсутствие persisted state — штатную join-семантику, а более старое наблюдение отбрасывается stale-проверкой. Поэтому обычный неизменившийся checkpoint не создаёт новую logical session или interval.

Checkpoint намеренно не перечисляет persisted open users и не вызывает `observe_disconnected`: отсутствие пользователя в одном периодическом cache snapshot не является достаточным основанием для закрытия session и может пересечься с live Gateway event. Leave фиксируется live event-ом, а authoritative обработка отсутствующих пользователей остаётся за startup/reconnect reconciliation. Операции разных пользователей изолированы отдельными транзакциями и ошибками; один сбой логируется с `guild_id`/`user_id` и не останавливает остальные операции или loop.

На `on_disconnect`, в начале любого `on_ready`/`on_resumed` recovery и при `Client.close()` live gate закрывается, а checkpoint task отменяется и ожидается. Отмена распространяется в текущие per-user операции, чтобы их transaction contexts выполнили rollback. Только полностью успешный recovery публикует новый baseline, открывает gate и создаёт новый loop; проверка существующей task исключает дубли. Per-member `FOR UPDATE` и общая stale/idempotent semantics сериализуют checkpoint с live events и не позволяют более старому checkpoint откатить более новое наблюдение.

Reconciliation выполняется до допуска обычной обработки новых voice events и использует единое время запуска `R`:

- сохранённый `confirmed_through_at` открытой session обозначается `H`; время после `H` до нового наблюдения не считается точным;
- если пользователь остался в том же полном snapshot `(channel_id, channel_kind, is_afk)` и `R > H`, старый exact interval закрывается на `H`, gap `[H, R]` записывается закрытым interval с `quality = 'estimated'`, после чего с `R` открывается новый exact interval в той же session и `confirmed_through_at` становится равен `R`;
- при полном совпадении snapshot и `R == H` новые intervals не создаются, а при `R < H` reconciliation игнорируется как устаревшая;
- `estimated` означает только отсутствие непосредственного подтверждения непрерывного присутствия во время gap;
- если любой элемент полного snapshot отличается, неизвестный gap не приписывается ни одному каналу: старые exact interval и session закрываются на `H`, а с `R` начинается новая exact session в текущем snapshot;
- если пользователя больше нет в voice, interval и session закрываются на `H`, момент выхода не придумывается, а gap не учитывается;
- если открытой session раньше не было, но пользователь сейчас находится в voice, с `R` создаётся новая exact session;
- повторное представление уже сохранённого текущего состояния не создаёт новый interval.

Gateway startup adapter при каждом `on_ready` сериализует всю startup-операцию. Сначала он атомарно выполняет reference provisioning и ждёт успешного commit; при ошибке provisioning reconciliation не запускается. Затем adapter один раз фиксирует timezone-aware UTC `R`, снимает текущие voice/stage snapshots из cache Discord и получает из repository владельцев persisted открытых sessions. Для каждого connected пользователя он вызывает `reconcile_connected`, а для владельца открытой session, отсутствующего во всех текущих voice states, — `reconcile_disconnected`. Reconciliation разных пользователей выполняется конкурентно в отдельных caller-owned транзакциях; handler не создаёт intervals и не дублирует domain rules. Повторный `on_ready` или reconnect запускает те же идемпотентные provisioning/service операции и не обходит stale/equal-time проверки.

Основная пользовательская статистика и leaderboard по умолчанию суммируют только `quality = 'exact'`. Estimated duration вычисляется отдельной величиной и никогда незаметно не добавляется к обычному voice time.

#### Статистические запросы и производительность

- Общее время, время пользователя, время канала и статистика за период вычисляются SQL-запросами по `voice_intervals`.
- Короткие sessions сохраняются физически; `VOICE_MIN_SESSION_SECONDS` применяется к суммарной длительности exact-интервалов logical session при построении основной статистики.
- Aggregate/cache tables на текущем масштабе не используются.
- Daily voice aggregates, materialized views, partitioning и специальные cache tables добавляются только после измерения реальной необходимости.

Read-only voice statistics оформлена отдельной feature/service/repository ответственностью. Service вычисляет четыре границы из одного timezone-aware UTC `as_of`: локальную полночь `REPORT_TIMEZONE`, rolling `7 * 24h`, rolling `30 * 24h` и all-time без нижней границы. `ZoneInfo` используется непосредственно, поэтому DST и исторические изменения offset не заменяются фиксированным UTC-смещением.

PostgreSQL repository одним aggregate statement объединяет `voice_intervals` с `voice_sessions`, отбрасывает AFK и вычисляет эффективный конец: `min(ended_at, as_of)` для закрытого interval и `min(confirmed_through_at, as_of)` для открытого. Каждое окно получает только пересечение `[max(started_at, window_start), effective_end]` с защитой от отрицательной duration. Exact и estimated секунды агрегируются раздельно, DTO дополнительно предоставляет их сумму.

Утверждённый `VOICE_MIN_SESSION_SECONDS` применяется на query layer к суммарной подтверждённой non-AFK exact duration logical session до `as_of`, до агрегации окон. Estimated duration не помогает session пройти порог; если exact threshold пройден, estimated участки этой session включаются в total и остаются отдельно видимыми в DTO. Это сохраняет ранее принятую semantics и не требует удаления коротких sessions или новой schema.

`/stats` и `/top` переиспользуют общие repository builders effective intervals, eligible sessions, per-user quality totals и deterministic ordering; отдельной ranking state/time semantics нет. Leaderboard выбирает одно из тех же четырёх окон, после session eligibility группирует exact/estimated overlap по `user_id`, исключает `discord_users.is_bot`, отбрасывает нулевой total и выполняет deterministic TOP 10: `total DESC`, `exact DESC`, `user_id ASC`.

Персональные standings `/stats` строят wide per-user totals сразу для today/7d/30d/all из одного `VoiceStatisticsQuery`, разворачивают периоды в строки и после фильтра `total > 0` применяют `ROW_NUMBER()` с тем же полным ordering внутри каждого периода. Поэтому позиция является фактическим порядковым номером, включая значения за пределами TOP 10; `participant_count` является числом non-bot пользователей с ненулевым eligible total. Standings выполняются одним SQL query.

Channel analytics использует существующий `voice_intervals.channel_id`, то есть каждый атомарный интервал сохраняет фактический канал своего участка logical session. Shared effective/eligible/overlap builders теперь передают этот ID в channel aggregation: move закрывает интервал старого канала и открывает интервал нового, поэтому время не приписывается session целиком одному каналу. User all-time TOP 3 и server period TOP 10 группируют exact/estimated отдельно по persisted channel ID, исключают AFK и bots, фильтруют zero total и сортируют `total DESC, exact DESC, channel_id ASC`. Deleted channels не исключаются persistence-запросом; Discord adapter использует current cache name либо fallback ID.

`/channelstats` принимает выбранный текущий Voice/Stage channel, но источник attribution остаётся только persisted `voice_intervals.channel_id`. Один statement сначала строит effective intervals и whole-session eligibility без channel filter, поэтому короткий сегмент после move не превращает общий session threshold в per-channel threshold. Только после qualification применяется selected `channel_id` и period overlap, затем exact/estimated группируются по `user_id`. Из полного ненулевого per-user результата оконные `SUM` считают channel total, а `ROW_NUMBER` задаёт `total DESC, exact DESC, user_id ASC`; TOP 10 отсекается во внешнем SELECT, поэтому total включает пользователей вне TOP. Пустой набор преобразуется repository в zero total и empty entries без второго запроса.

`/stats` является профилем одного выбранного периода и использует два caller-owned read-only query. Core statement возвращает target exact/estimated total, полный non-bot rank/participant count, `COUNT(DISTINCT session_id)` для logical sessions с положительным period overlap, предыдущий равный период и TOP-1 persisted channel. Companion statement строит два набора через общий effective/eligible helper, соединяет target и non-bot intervals по `channel_id`, ограничивает положительное пересечение выбранным окном и группирует по companion `user_id`; self/AFK/bots исключены, ordering равен `total DESC, exact DESC, user_id ASC`, limit равен 3. Оба statement получают один `VoiceStatisticsQuery` и `VoiceProfileWindow` и выполняются в одной `REPEATABLE READ` transaction, поэтому видят один snapshot даже при checkpoint commit между ними; session close завершает read transaction rollback-ом без commit. Python-загрузка history, summary tables и migration не используются.

Для today core использует локальную полночь `REPORT_TIMEZONE`, а comparison — предыдущую локальную дату от 00:00 до того же wall-clock времени; 7d/30d сравниваются с непосредственно предшествующим rolling-окном той же длины, all-time comparison отсутствует. Session eligibility остаётся whole-session: threshold проверяется по всей подтверждённой non-AFK exact duration до `as_of`, а distinct session попадает в счётчик только при ненулевом overlap текущего окна. Поэтому несколько channel intervals после move дают одну session. Favorite channel выбирается по period total с tie-break `total DESC, exact DESC, channel_id ASC`; имя берётся из persisted `voice_channels`, чтобы cache miss/deletion не раскрывал технический ID.

`/together` переиспользует те же `_eligible_voice_intervals`, pair-overlap и exact/estimated helpers. Один statement строит отдельные effective/eligible CTE для обоих non-bot пользователей, агрегирует положительные пересечения одного `channel_id`, ранжирует общие каналы по `total DESC, exact DESC, channel_id ASC`, сохраняет общий pair total до применения TOP-3 и параллельно считает individual all-time eligible totals. Поэтому A/B и B/A симметричны по pair/channel totals, а denominator меняются местами. Handler устанавливает `REPEATABLE READ` до SELECT и не управляет commit/rollback внутри repository; schema и migration не меняются.

`/serverstats` строит один общий non-bot effective/eligible interval CTE и из него два bounded aggregates выбранного окна: per-user и per-channel. Положительные per-user totals определяют server exact/estimated sum, `active_users` и TOP-1 через общий `_ranking_order`; channel totals используют общий `_channel_ranking_order`. Server total является person-time: одновременный час пяти участников равен пяти часам активности участников, а не одному часу wall-clock occupancy. Summary и оба TOP-1 возвращаются одним statement; handler устанавливает `REPEATABLE READ` до SELECT, repository не управляет transaction и migration не требуется.

`/activity` переиспользует тот же effective/eligible interval contract: PostgreSQL одним read-only statement выбирает только non-AFK/non-bot интервалы configured guild, пересекающие bounded 7/30/90-day UTC window, ограничивает open rows через `confirmed_through_at` и применяет минимальную длительность к whole-session exact time. Discord-independent aggregation clipping'ует интервалы, переводит фактические границы в `REPORT_TIMEZONE`, распределяет user-seconds по local hour/weekday и нормализует recurring hours по доступному wall-clock exposure, а weekdays — по числу фактических календарных появлений. Top-3, active weekday и quietest 3-hour bucket имеют deterministic ordering; 8×7 numeric heatmap преобразуется чистой intensity-функцией в шкалу `· ░ ▒ ▓ █` и выводится компактным code block без дополнительного запроса. Существующий индекс `(guild_id, started_at)` используется без migration или secondary statistics storage.

#### Границы первой business migration

Первая business revision должна создать перечисленные шесть таблиц, их PK, FK, `UNIQUE`, `CHECK` и основные indexes. В неё намеренно не входят:

- message statistics и reactions;
- presence;
- role, nickname и membership history;
- mute/deaf/stream/video statistics;
- aggregate/cache tables;
- retention-задачи;
- реализация opt-out и удаления данных;
- audit event log;
- authenticated/public web dashboard and its domain pages (the separate
  loopback-only read-only foundation does not change this migration boundary).

### Область одного и нескольких guild

- Первая версия предназначена для одного Discord guild.
- Схема БД и бизнес-логика с самого начала используют `guild_id`.
- Модель не должна препятствовать будущей поддержке нескольких guild.

Это требование не означает, что multi-guild управление или конфигурация входят в MVP.

### Архитектура приложения

- Приложение строится как modular monolith с feature-first организацией.
- Discord handlers отвечают за адаптацию входных событий и команд, но не содержат основную бизнес-логику.
- Handlers вызывают application/services соответствующих feature-модулей.
- Сервисы взаимодействуют с БД через persistence/repository слой.
- Объекты discord.py не должны проникать глубоко в бизнес-логику.
- Сервисы статистики проектируются так, чтобы будущий Web API мог использовать их без дублирования логики.
- Структура может уточняться по мере реализации без ненужного усложнения.

Базовая структура проекта:

```text
src/discord_stats_bot/
├── main.py
├── config.py
├── discord/
├── features/
│   ├── voice/
│   ├── text_activity/
│   ├── members/
│   └── statistics/
├── persistence/
│   ├── models/
│   ├── repositories/
│   └── database.py
└── common/

tests/
migrations/
```

### Discord-интеграция

Rules v1 хранит версии в `rulesets`, а доказательство принятия точной версии —
в `rule_acceptances`. PostgreSQL является единственным source of truth;
optional Discord-роль — только восстанавливаемое следствие для access control.
Partial unique index разрешает не более одного `published` ruleset на guild,
уникальность `(guild_id, version)` сохраняет identity версии, а уникальность
`(guild_id, user_id, ruleset_id)` вместе с `ON CONFLICT DO NOTHING` делает
принятие конкурентно идемпотентным. Composite foreign key не позволяет связать
acceptance с ruleset другого guild. Application service не предоставляет API
изменения опубликованного content: новая редакция публикуется новой строкой, а
прежняя становится `archived`. Acceptance и документированный publish seed
берут одинаковую `guilds` row lock, чтобы current-version resolution не
пересекался с публикацией в середине транзакции.

`/rules` читает только текущую опубликованную версию из PostgreSQL и прикрепляет
persistent view с stable `custom_id=kanami:rules:accept:v1`, регистрируемую в
`setup_hook` до command sync. Callback заново разрешает current ruleset внутри
транзакции, поэтому старая Discord message не может записать acceptance как
новую версию. `/rules-status` использует существующую `manage_guild` policy и
показывает publication metadata и accepted count. Discord не допускает одному
application-command имени одновременно быть исполняемой командой и group,
поэтому admin status не оформлен как `/rules status`.

Rules Publication R2A расширяет существующую единственную строку
`guild_server_settings` nullable-полями `rules_publication_channel_id`,
`rules_publication_message_id` и `rules_publication_ruleset_id`. Отсутствие
channel ID означает opt-in feature disabled; message/ruleset cursor либо оба
отсутствуют, либо оба заданы, а composite foreign key удерживает reflected
ruleset в том же guild. Текст правил повторно не хранится. Один guild поэтому
имеет не более одного managed Rules message, а PostgreSQL переживает restart и
остаётся source of truth для delivery cursor.

Bot-owned `RulesPublicationService` сериализует startup, RESUME и Bot Control
sync общей `asyncio.Lock`. Первый sync создаёт message, новая published version
редактирует то же message, совпадающий cursor не вызывает `edit`, а Discord
`NotFound` для сохранённого message создаёт replacement и атомарно заменяет
cursor. Renderer `/rules` и тот же `RulesAcceptanceView` со stable custom ID
используются без второй acceptance business logic. Missing/unsupported channel,
`Forbidden`, `NotFound` и transient `HTTPException` возвращаются как
структурированный результат и не останавливают Gateway startup. Значимые
результаты логируются с guild/channel/message/ruleset/version; обычный
`already_current` не создаёт audit history.

Create/recreate использует узкую compensation для partial failure: если Discord
`send` успешен, но новый delivery cursor не сохранился, только что созданное
message best-effort удаляется, после чего исходная persistence exception
продолжает существующий runtime/Bot Control failure path. Ошибка rollback-delete
логируется с идентификаторами и не заменяет исходную ошибку. Для UPDATE уже
существующего managed message удаление никогда не выполняется. Поиск orphan
messages остаётся вне Rules Publication v1.

Принудительный sync доступен только bot process через bearer-authenticated
loopback `POST /control/v1/rules/publication/sync`. R2B добавляет fixed-purpose
`POST /control/v1/rules/publication/configure` и `/disable`; все три операции
требуют shared secret, а admin mutations также передают actor из server-side web
session. Для channel discovery `/admin/rules` переиспользует bounded
`/control/v1/server-settings/options`, поэтому Web Admin не получает
`DISCORD_TOKEN` и не обращается к Discord API напрямую.

Configure валидирует current guild text/news channel и effective permissions
`view_channel`, `send_messages`, `embed_links` в bot process. Same-channel
операция сохраняет delivery cursor. При change/disable старое managed message
best-effort удаляется до DB mutation; `NotFound` означает успешный cleanup, а
`Forbidden`/transient Discord failure сохраняют прежние channel/cursor и
возвращают structured failure. После cleanup channel upsert атомарно сохраняет
новое значение или `NULL` и очищает message/ruleset cursor. Если этот DB write
падает уже после удаления, прежняя DB configuration остаётся source of truth, а
READY/RESUME reconciliation восстанавливает отсутствующее старое message.

`/admin/rules` строит read model из current published ruleset и durable
publication cursor и показывает disabled/unpublished/current/stale независимо
от доступности Bot Control; имя channel дополняется существующими options, с
fallback на ID. CSRF-protected OWNER/ADMIN forms меняют channel, отключают и
запускают manual sync только через loopback control. Rules publish сначала
полностью commits PostgreSQL transaction, затем best-effort вызывает sync.
Discord/control failure не откатывает published version, показывается warning и
остаётся recoverable через manual sync либо READY/RESUME.

Rules Compliance R3A вычисляет состояние пользователя поверх immutable
publication/acceptance history и не создаёт synthetic acceptance rows. История
сортируется по `published_at`, затем по `ruleset.id`; version string не участвует
в ordering. Draft исключены, а archived rulesets остаются частью опубликованной
истории. Required checkpoint — последний опубликованный ruleset с
`requires_reacceptance=true` до current включительно либо первая опубликованная
версия, если mandatory checkpoint ещё не встречался. Acceptance квалифицируется,
если его ruleset находится между checkpoint и current в этом publication order.

Nullable `rulesets.reacceptance_grace_days` задаёт 1–365 дней только для ruleset
с `requires_reacceptance=true`; PostgreSQL CHECK защищает это состояние.
Deadline равен UTC `checkpoint.published_at + grace days`. Без qualifying
acceptance пользователь pending до deadline включительно и overdue после него;
при отсутствии deadline остаётся pending без автоматического перехода. Read-only
aggregate использует durable current-member scope `guild_members.left_at IS NULL`
и исключает `discord_users.is_bot=true`. `/admin/rules` показывает summary без
Discord API; ошибка summary изолирована warning-блоком и не скрывает Rules или
Publication. Scheduler, reminders, enforcement и new-member onboarding в R3A не
входят.

Migration создаёт только schema и не вставляет production guild/content.
Первичную v1.0 после миграции создают отдельным транзакционным initial-only
`scripts/publish_rules_v1.sql` с явным `guild_id`. Под guild row lock он требует
существующий guild и полное отсутствие его `rulesets`; любой повторный запуск
завершается ошибкой до `INSERT` без архивирования или замены данных. Версии 1.1+
публикует Web Admin Rules v1. OWNER/ADMIN создаёт новый draft как копию current
published, меняет только draft и может удалить только draft. Публикация под
`guilds` row lock в одной транзакции архивирует current, публикует выбранный
draft, устанавливает `published_at` и создаёт important `rules.published` audit
event; partial unique index остаётся последней защитой инварианта одного
published ruleset. Создание, изменение и удаление draft также пишут
`rules.draft_created`, `rules.draft_updated` и `rules.draft_deleted` в
существующий `audit_events`. Preview использует только form data и не обращается
к write service. Общий domain validator проверяет точный Discord embed contract
`/rules`: title не длиннее 256 символов, description не длиннее 4096, footer
`Версия {version}` не длиннее 2048 и сумма этих трёх частей не больше 6000.
Проверка выполняется для Preview, create/update draft и непосредственно перед
publish; initial published v1.0 остаётся immutable. Update/delete сохраняют
`status = 'draft'` в mutation predicate и трактуют zero-row `RETURNING` как
immutable conflict, поэтому конкурентный publish не даёт HTTP 500 или ложного
успеха/audit event. Только PostgreSQL constraint
`uq_rulesets_guild_version` отображается как duplicate version; остальные
`IntegrityError` не маскируются и откатывают транзакцию. Reminders, grace
periods, diff, rollback, scheduled publishing, role enforcement и forced
reacceptance workflow намеренно отложены.

- Discord Python-библиотека — discord.py.
- Discord-интеграция работает асинхронно через asyncio.
- Основной пользовательский интерфейс — application/slash commands.
- Для member tracking используется `GUILD_MEMBERS`.
- `MESSAGE_CONTENT`, typing и DM intents не используются. Presence intent
  включается только при `GAME_TRACKING_ENABLED=true`.
- Текущий Gateway client всегда включает guild, guild-message, member,
  voice-state и moderation intents. `on_ready` использует cache сначала для
  provisioning, затем для Voice и optional Game reconciliation;
  `on_voice_state_update`, `on_presence_update` и `on_message` поддерживают свои
  изолированные collectors. В Developer Portal всегда требуется Server Members
  Intent, а Presence Intent — только перед включением Game Tracking.
- Gateway adapter отвечает только за преобразование Discord cache в полный `(channel_id, channel_kind, is_afk)` snapshot, единый timestamp операции, транзакционные service-вызовы и компактное итоговое логирование; exact/estimated semantics остаётся в application service.
- `/profile`, `/stats`, `/games`, `/top`, `/topmessages`, `/channels`, `/channelstats`, `/together`, `/serverstats`, `/activity`, `/achievements`, `/anniversaries`, `/rules`, `/rules-status`, `/health` и `/help` добавлены в `app_commands.CommandTree` только для configured guild и синхронизируются вместе одним вызовом в одноразовом `Client.setup_hook()` до Gateway events; `on_ready`/`on_resumed` command sync не вызывают и сохраняют прежнюю voice recovery semantics.
- Slash handler `/stats [user] [period]` проверяет configured guild и invoking-user bot guard, отклоняет bot target, по умолчанию использует `interaction.user` и период 7d. Compact ephemeral embed показывает один согласованный профиль: total, полный rank, logical session count/average, любимый канал, period TOP 3 companions и finite-window trend; all-time trend отсутствует. Ошибка любого из двух query изолируется как единая операция без partial embed; все mentions подавлены через `AllowedMentions.none()`.
- `/top` имеет optional application-command choice `period` (`today`, `7d`, `30d`, `all`, default `7d`) с русскими названиями и возвращает публичный embed. Persistence ranking не зависит от Discord cache: adapter показывает cached member как кликабельный `<@user_id>`, оставляет fallback для отсутствующего member и подавляет уведомления пользователей, ролей и `@everyone` через `AllowedMentions.none()`.
- `/help` — статический ephemeral embed с актуальным списком команд; handler не зависит от persistence и не открывает DB session.
- `/anniversaries` использует текущий guild member cache и `Member.joined_at`, исключает bots и записи без даты вступления, публично показывает годовщины в inclusive-окне сегодня + 30 дней по календарю `REPORT_TIMEZONE`. В невисокосный год годовщина вступления 29 февраля считается 28 февраля. Команда остаётся read-only; durable persistence, worker и channel setting относятся только к optional автоматическим поздравлениям.
- `/health` — guild-only административная read-only команда с default и runtime
  проверками `manage_guild`. Runtime формирует актуальный snapshot Gateway,
  command sync и voice startup state, а handler выполняет изолированный
  `SELECT 1`, измеряет latency monotonic clock и при ошибке БД всё равно
  возвращает ephemeral embed без exception, connection data и secrets. Uptime
  отсчитывается от единственной monotonic отметки startup; новые settings,
  migrations и intents не требуются.
- `/channels` имеет тот же period contract и публично показывает persistence-ranked TOP 10; adapter разрешает только текущее escaped channel name, использует `Канал <ID>` при cache miss и запрещает mentions через `AllowedMentions.none()`.
- `/channelstats` ограничивает required channel option штатным union VoiceChannel/StageChannel и повторно валидирует тип/guild до query; публичный embed использует текущее escaped имя выбранного канала, escaped cache display names с fallback `Пользователь <ID>` и `AllowedMentions.none()`.
- `/together` принимает два required `discord.Member`, до query отклоняет invalid guild/DM, invoking bot, bot targets и одинаковые ID, затем возвращает ephemeral all-time pair embed с non-notifying member mentions, pair duration, individual percentages и TOP 3 common channels.
- `/serverstats` имеет optional period с default 7d и теми же четырьмя choices, что `/top`/`channels`; configured-guild validation выполняется до DB, а compact overview отвечает ephemeral с `AllowedMentions.none()`.
- `/activity` имеет optional period `7d`/`30d`/`90d` с default `30d`; configured-guild validation выполняется до DB, а compact public embed показывает Top-3 recurring hours, нормализованный active weekday, quietest 3-hour bucket и `REPORT_TIMEZONE` с `AllowedMentions.none()`.
- `/games [user] [period]` имеет default invoking user и `30d`, choices
  `7d`/`30d`/`90d`/`all` и private embed. Wrong guild и bots отклоняются до DB;
  выключенный bot-wide Game Tracking opt-in даёт понятный unavailable response.
  Handler не содержит SQL и подавляет mentions через `AllowedMentions.none()`.
- Для text tracking боту требуется доступ на просмотр соответствующих guild text
  channels; privileged Message Content Intent для этого не требуется.

### Deployment

- Первая официально документированная целевая среда — Debian 13, `systemd` и
  локальный PostgreSQL.
- Приложение устанавливается в `/opt/kanami`, запускается отдельным system user
  `kanami`, а production environment хранится в
  `/etc/kanami/kanami.env` с ограниченными правами.
- `/opt/kanami` остаётся чистым Git working tree; home/credential state
  пользователя вынесен в `/var/lib/kanami`, bootstrap `uv` — в
  `/opt/kanami-uv`, cache `uv` — в `/var/cache/kanami/uv`.
- Entry point systemd — console script
  `/opt/kanami/.venv/bin/discord-stats-bot`; Alembic остаётся отдельным
  deployment step и не запускается application unit-ом.
- Консервативный installer создаёт отдельные PostgreSQL database
  `discord_stats_prod` и role `kanami_app` только при отсутствии обоих; первый
  запуск с placeholder Discord credentials запрещён операционным flow.
- Update выполняется только из чистого Git tree через `git pull --ff-only`,
  locked dependency sync и migration до restart. Forced checkout/reset и
  автоматическое удаление данных не используются.
- Foundation будущего Kanami Manager — автономный Bash entrypoint
  `scripts/manager.sh`, рассчитанный на последующую установку как
  `/usr/local/bin/kanami`. D2.1 добавил `help`/`version`, а D2.2 — read-only
  `status`/`doctor`; manager не требует root, не читает environment-файлы и не
  вводит фиктивный semver. Git-команды выполняются без optional locks, systemd
  используется только через обычный `systemctl show`/`is-active`, а fake
  systemctl в hermetic tests подставляется только через `PATH`. Read-only пути
  имеют узкие test overrides, которые нельзя переносить в будущие write actions.
- Основной checkout, bot executable, uv bootstrap/cache, `kanami.service` и его
  active-state являются обязательными doctor checks. Web Admin остаётся
  отдельным optional deployment: отсутствие его executable/unit или inactive
  service даёт `WARN`/`SKIP`, но само по себе не делает результат `UNHEALTHY`.
  Недоступная из-за command/permissions systemd-проверка также остаётся
  `WARN`/`SKIP`; fatal считается только подтверждённое нарушение обязательной
  проверки. LoadState `masked`/`error`/`bad-setting` является fatal для
  обязательного bot unit и warning для optional Web Admin; status показывает
  abnormal LoadState явно и не подменяет его active-state.
- Секреты не хранятся в Git.
- Web-панель в MVP отсутствует.

Docker/container deployment не входит в первый официальный install flow и может
быть спроектирован отдельно. Полноценный production health monitoring и
backup-политика остаются открытыми эксплуатационными вопросами.

### Тестирование

- Тестовый фреймворк — pytest.
- Для асинхронных тестов используется pytest-asyncio.
- Бизнес-логика по возможности тестируется без реального подключения к Discord.
- Unit-тесты не требуют Discord или PostgreSQL.
- Unit-тесты отделяются от интеграционных тестов БД.
- DB integration tests используют отдельный временный PostgreSQL.

### Статические проверки

- Ruff используется одновременно как linter и formatter.
- Конкретный набор правил Ruff будет зафиксирован в конфигурации проекта при создании каркаса.

### Жизненный цикл приложения

- Приложение имеет единую точку запуска.
- Поддерживается graceful shutdown.
- При завершении корректно закрываются Discord client, SQLAlchemy engine и background tasks.
- Background tasks создаются, отслеживаются и останавливаются централизованно.
- Текущая точка запуска явно создаёт async engine/session factory, запускает `discord.Client.start()` и освобождает engine после закрытия client, не выполняя миграции и не создавая глобальные persistence resources.
- При штатном SIGINT `asyncio.run()` сначала отменяет главную task, позволяя async context Discord client и `finally` persistence завершить cleanup, а затем преобразует signal-originated cancellation в `KeyboardInterrupt`; только этот `KeyboardInterrupt` подавляется на синхронной границе entrypoint с успешным exit code. Произвольный `CancelledError` внутри application и неожиданные runtime exceptions не подавляются.

## Открытые архитектурные вопросы

Необходимо отдельно определить:

- границы application/services и repository-интерфейсов внутри feature-first структуры;
- actor enrichment через Discord Audit Log и будущий контракт `/history`;
- семантику opt-out и удаления данных, включая уже созданные агрегаты;
- правила доступа к статистике других пользователей и pagination;
- дополнительные bot permissions и OAuth scopes, если появятся новые функции;
- способ создания и удаления временного PostgreSQL для DB integration tests;
- backup-политику и полноценный production health monitoring на этапе deployment.
