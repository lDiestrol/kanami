# Статус проекта

## Текущее состояние проекта

Созданы Python-каркас, PostgreSQL persistence foundation, async Alembic
infrastructure, Discord Gateway runtime, voice statistics, суточная Text
Activity и durable Kanami Audit Logging. Runtime считает сообщения и replies без
message content, `/anniversaries` показывает ближайшие годовщины участников из
Discord member cache, optional worker автоматически поздравляет их в настроенном
канале, optional return feature узнаёт ранее ушедших участников по durable
`member.left` history и приветствует их со snapshot lifetime-статистики, а
optional Game Tracking backend сохраняет подтверждённую историю Playing
activities, а `/games [user] [period]` показывает private игровую статистику, и
`/profile` собирает паспорт участника из Discord identity, Voice и Achievements.
Rules v1 показывает PostgreSQL-backed правила через `/rules`, принимает точную
версию persistent-кнопкой и даёт администраторам `/rules-status`; вместе с
`/health` это шестнадцать guild-команд;
Rules Publication v1 добавляет opt-in managed message текущей published версии:
bot process создаёт, обновляет и восстанавливает его идемпотентно, а Web Admin
управляет channel/disable/manual sync без Discord token;
Rules Compliance R3A вычисляет compliant/pending/overdue из опубликованной
истории и реальных acceptance без synthetic rows и показывает read-only summary
на `/admin/rules`;
отдельный publish-ready Web Admin process предоставляет HTML/JSON
PostgreSQL health foundation, authenticated Dashboard v1, read-only W1.3
Operations page `/admin/system`, WUI-2 presentation для Dashboard/Operations и paginated список
текущих участников с persisted
Discord identity, защищённый Discord OAuth2 authentication, PKCE S256 и
server-side in-memory sessions, а Stage 3D разрешает создание session только
после allowlist и current non-bot guild membership проверок; Stage 4 добавляет
CSRF-protected управление guild-specific nickname/avatar самого бота через
отдельный authenticated loopback control interface без передачи Bot token в Web Admin;
OWNER-only `/admin/administrators` показывает постоянных env OWNER и active
managed ADMIN и делегирует grant/revoke только через Bot Control; OWNER-only
`/admin/audit` показывает bounded историю grant/revoke и изменений server
settings через bounded SELECT-only query service; Stage 6A и backend foundation Stage 6B.1 объединены с
`main`, развёрнуты на production-host и успешно прошли production smoke; production
smoke для text ingestion, `/achievements` и `/health` также выполнен. Stage 6B.2
Server Settings UI объединён с `main`, развёрнут на production-host и
production-validated. Stage 6B.3 UX/audit polish объединён с `main`, развёрнут
на production-host и production-validated. Подготовлены
человекочитаемая русская документация и первый официальный deployment flow для
Debian 13 + systemd + локальный PostgreSQL. Начат D2 — Installation & Lifecycle
v2: новая официальная установка добавляет `/usr/local/bin/kanami`, штатный
updater refresh-ит его после pull, а Kanami Manager поддерживает read-only
`help`, `version`, `status`, `doctor`, `logs`, lifecycle start/stop/restart и
trusted wrapper `update`.
Achievements
доступны пользователям через guild-only `/achievements` с актуализацией
voice/community метрик и идемпотентной выдачей. Pagination не реализована.

Текущее подтверждённое production-состояние работает на commit `1e73c8a`:
PostgreSQL Alembic `current = heads = d4e8a1c7b962`,
`kanami.service` и `kanami-web-admin.service` active. Bot Control работает только
на loopback `127.0.0.1:8765`, а Web Admin сохраняет существующую private-bind +
reverse-proxy архитектуру. Rules v1, Web Admin Rules v1, Rules Publication v1 и
Rules Compliance R3A развёрнуты на production-host и прошли соответствующие production
smoke. Server Analytics v1 также развёрнут в production и 29 августа 2026 года
прошёл authenticated production browser smoke. WUI-4B Member Analytics 7d/30d
также завершён, развёрнут и прошёл authenticated production browser smoke.
G3A Member Game Analytics объединён с `main` в commit `9c41204`, развёрнут на
production-host и 31.08.2026 прошёл authenticated production browser/server/responsive
smoke: существующий member profile получил read-only секцию «Игры» с независимым
периодом 7d/30d/90d поверх канонического `GameStatisticsService`.
G3B Server Game Analytics объединён с `main` в commit
`1e73c8a feat(web): add server game analytics`, развёрнут на production-host и
31.08.2026 прошёл authenticated production browser/server/database/responsive
smoke; G3B marked merged, deployed и production-smoke-verified.

## Что уже выполнено

- D2.9: добавлен root-only `sudo kanami update` как thin wrapper над canonical
  `/opt/kanami/scripts/update.sh`. До запуска Manager fixed `stat` проверяет
  checkout/scripts/updater bootstrap chain: ожидаемые типы, отсутствие symlink,
  UID/GID 0 и отсутствие group/other write. Updater запускается отдельным fixed
  `/usr/bin/bash`, output не скрывается, exit code передаётся caller-у; Git, uv,
  Alembic и systemd workflow остаётся только в `scripts/update.sh`. Direct CLI не
  требует confirmation; menu `9. Update` требует y/Y/yes/YES и после actual
  invocation завершает session из-за возможного self-refresh. Rollback не
  добавлен, поэтому runtime failure может оставить partial update.
- D2.8: production checkout `/opt/kanami`, `.git` и tracked source закреплены за
  `root:root` без write access для service user. Узким writable-исключением
  остаётся ignored `.venv` (`kanami:kanami`); runtime home и uv cache также
  остаются service-user writable. Installer больше не делает recursive chown
  checkout, updater выполняет Git operations от root и до pull валидирует новую
  ownership policy как defense-in-depth уже доверенной canonical installation.
  Legacy all-`kanami` installation требует manual migration/reinstall из trusted
  source; её checkout-local `update.sh` нельзя запускать с root для
  самомиграции, поскольку validation внутри уже запущенного script не создаёт
  trust boundary.
- D2.7: добавлены controlled root-only `kanami start`/`kanami stop` только для
  основного `kanami.service` через фиксированный `/usr/bin/systemctl`. Оба action
  проверяют `LoadState=loaded`; start не повторяет active service и подтверждает
  active state после запуска, stop принимает success только при точном конечном
  `inactive`. Menu добавляет `7. Start bot` без confirmation и `8. Stop bot` с
  y/Y/yes/YES confirmation. Auto-sudo, Web Admin mutation, daemon-reload,
  reset-failed и изменение enable/disable policy отсутствуют.
- D2.6: добавлен read-only `kanami logs` только для journal основного
  `kanami.service`: фиксированный `/usr/bin/journalctl`, default 100 записей,
  validated `--lines N` 1..1000 и обязательный `--no-pager`. Команда не требует
  root со стороны Manager и передаёт journalctl exit code прямому caller. Menu
  добавляет `6. Logs` без confirmation и не закрывается при failure. Follow,
  Web Admin logs, arbitrary units, auto-sudo и binary env override отсутствуют.
- D2.5: добавлена первая lifecycle-команда `kanami restart`. Она требует root,
  использует фиксированный `/usr/bin/systemctl`, проверяет `LoadState=loaded`,
  перезапускает только обязательный `kanami.service` и подтверждает успех лишь
  после post-restart active check. Menu сохраняет прежние пункты 1–4 и `0. Exit`,
  добавляет `5. Restart bot` с отдельным confirmation; cancel/EOF безопасны, а
  failure не закрывает menu. Auto-sudo и restart optional Web Admin отсутствуют.
- D2.4: штатный `scripts/update.sh` после успешного `git pull --ff-only` и до
  dependency sync refresh-ит regular `/usr/local/bin/kanami` только из
  `/opt/kanami/scripts/manager.sh` через `install -m 0755 -o root -g root`.
  Missing/unreadable/non-regular или symlink source останавливает update до uv,
  Alembic и restart; rollback checkout намеренно не добавлен.
- D2.3: installer идемпотентно копирует committed manager из `/opt/kanami` в
  `/usr/local/bin/kanami` как regular `root:root` file с mode `0755`. Добавлено
  read-only Bash menu только с Status/Doctor/Version/Help/Exit: no-args открывает
  его лишь при TTY stdin+stdout, non-TTY показывает help, invalid choice
  повторяет меню, EOF безопасно завершает его. Lifecycle commands и полноценный
  updater v2 ещё не реализованы.
- D2.2: добавлены read-only `status` и `doctor`. Диагностика проверяет checkout,
  Git repository/cleanliness/origin, bot и optional Web Admin executables, uv
  bootstrap/cache, наличие и active-state обоих systemd units. Обязательные
  ошибки дают `UNHEALTHY` и exit code 1; отсутствие или inactive-state
  отдельного optional Web Admin отражается через `WARN`/`SKIP` и не является
  fatal, а недоступные systemd probes не маскируются под подтверждённый FAIL.
  Abnormal LoadState обязательного bot unit является fatal, для optional Web
  Admin — warning. Hermetic tests используют только временные пути и fake
  systemctl через `PATH`, не читают env-файлы и проверяют отсутствие secrets в
  output; production override systemctl binary отсутствует.
- D2.1: добавлен автономный Bash entrypoint `scripts/manager.sh` с
  `set -Eeuo pipefail`, предсказуемыми `help`/`-h`/`--help`, безопасным
  `version` без фиктивного semver и ненулевым exit code для неизвестной команды.
  Linux/Bash pytest-покрытие явно пропускается на Windows; installer, updater и
  CI workflow не изменялись.

- Реализован G3A Member Game Analytics без нового route, migration и изменений
  Game Tracking collection: отдельный Web Admin service выполняет один
  configured-guild member read в `REPEATABLE READ, READ ONLY` snapshot и
  делегирует расчёт существующему `GameStatisticsService`. Member profile
  показывает подтверждённое игровое время, canonical unique games, игровые дни,
  TOP-5, последнюю игру и самую длинную подтверждённую сессию. `game_period`
  7d/30d/90d независим от Activity `period`; некорректные, пустые,
  дублированные и неподдерживаемые значения безопасно дают 30d. Empty и
  unavailable состояния изолированы на уровне секции, persisted game names
  HTML-экранируются, server-rendered responsive presentation не использует
  JavaScript и сохраняет текущий CSP.
- Production deployment G3A выполнен 31.08.2026 из fast-forward checkout
  `/opt/kanami` до `9c41204 feat(web): add member game analytics`. Новая migration
  не потребовалась; перезапущен только `kanami-web-admin.service`, основной
  `kanami.service` и его PID не менялись. Web Admin получил новый PID, оба
  сервиса остались `active`, а startup Web Admin прошёл без traceback, import и
  configuration errors. Неавторизованный `/admin/` корректно вернул `303` на
  `/admin/login`, `/admin/health` — HTTP `200`.
- Production database/journal smoke подтвердил, что после Web-only deployment
  Game Tracking продолжил работу: PostgreSQL уже содержит тысячи реальных
  игровых сессий, существовали активные open sessions, а
  `confirmed_through_at` продолжал обновляться. В проверенных Web Admin и bot
  journals не обнаружено deployment/Game Tracking errors.
- Authenticated browser smoke реального member profile подтвердил расположение
  секции «Игры» между Activity и Lifetime statistics, default `game_period=30d`,
  работу 7d/30d/90d и независимое сохранение Activity `period` и Games
  `game_period` в обе стороны. На реальных production game data отображаются
  подтверждённое время, canonical games, игровые дни, TOP игр, последняя игра и
  самая длинная сессия; presentation последней игры использует
  `REPORT_TIMEZONE`.
- Responsive authenticated smoke подтвердил narrow/mobile, промежуточную и
  широкую desktop/tablet раскладки. В compact режиме header/nav, Identity,
  Membership, Activity KPI/daily presentation и Game KPI/details остаются
  читаемыми; selector 7/30/90 доступен, наложений и horizontal overflow не
  обнаружено. На широкой раскладке сохраняется многоколоночное представление.
  No-data profile, искусственная database failure и malicious HTML game name
  покрыты regression tests, но отдельный production fault-injection/manual smoke
  для них не выполнялся; отдельная ручная сверка всех значений с `/games` также
  не заявляется.
- Реализован G3B Server Game Analytics как отдельная OWNER/ADMIN страница
  `GET /admin/games` и пункт «Игры» в группе навигации «Сервер». Периоды
  7d/30d/90d означают завершённые локальные календарные дни в
  `REPORT_TIMEZONE`, default равен 30d; invalid, empty и duplicate `period`
  безопасно приводятся к default без HTTP 500.
- G3B использует две set-based data queries в одном `REPEATABLE READ, READ ONLY`
  snapshot: bounded configured-guild session read одновременно получает
  persisted display names, отдельный aggregate read находит earliest confirmed
  activity для coverage. Per-member queries, Discord cache, live Presence,
  all-time session loading и writes отсутствуют. Общий canonicalization helper
  сохраняет G1/G3A semantics casefold/display selection/`with Medal`.
- Server-wide report считает confirmed game person-time, active gamers,
  canonical games, average per gamer, zero-filled daily time/unique gamers,
  deterministic TOP-10 игр с player count/share и TOP-10 игроков с unique games/
  gaming days. Open sessions ограничены `confirmed_through_at`; crash/reconnect
  downtime не приписывается. Persisted game/display names HTML-экранируются.
  Empty и unavailable states контролируемы; 90d chart использует существующий
  server-rendered horizontal-scroll pattern без JavaScript, external assets и
  CSP changes. Schema, migrations, collector/runtime и Discord commands не
  менялись.
- Production deployment G3B выполнен 31.08.2026 на production-host из commit
  `1e73c8a feat(web): add server game analytics`; перед deployment создан
  timestamped backup. Migration не требовалась и не выполнялась. Перезапущен
  только `kanami-web-admin.service`, а `kanami.service` не перезапускался. Оба
  сервиса остались `active`; Web Admin стартовал без traceback, import и
  configuration errors. Warning о configured private Web Admin listener ожидаем
  и ошибкой не является.
- Server/database smoke G3B подтвердил для `/admin/games`, `?period=7d`,
  `?period=30d` и `?period=90d` без авторизации redirect `303` на
  `/admin/login`; `/admin/health` вернул HTTP `200` и `{"status":"healthy"}`.
  В `game_sessions` присутствовала накопленная история, включая активные open
  sessions; earliest confirmed activity была обнаружена. После deployment
  checkpoint продолжал регулярно обновляться. В проверенных Web Admin и bot
  journals не обнаружено traceback, exception, error или failed.
- Authenticated production browser smoke G3B подтвердил 7d/30d/90d периоды в
  configured `REPORT_TIMEZONE`, coverage warning, KPI, zero-filled daily chart,
  TOP-10 игр и TOP-10 игроков. Для 7d warning о потенциально неполной истории
  корректно сохранился, потому что earliest activity была позже начала первого
  локального дня; это production-подтверждение exact-timestamp coverage fix. Для
  90d длинный chart оставался в собственном horizontal-scroll container и не
  ломал layout.
- Responsive smoke G3B на ширине около 400–500 px подтвердил KPI в одну колонку,
  вертикальное расположение TOP games и TOP players, отсутствие глобального
  horizontal overflow и сохранение 90d chart внутри scrollable container. В
  реальном TOP присутствовали Playing-приложения, не являющиеся играми; это не
  дефект G3B, classification/filtering остаётся будущим G4. Manual fault-injection,
  подтверждение authenticated requests через access
  log и независимая ручная сверка всех агрегатов с SQL или `/games` не
  выполнялись и не заявляются.
- Реализован backend/foundation Rules Publication R2A. Migration
  `e1a7c4d92b60` расширяет `guild_server_settings` nullable channel/message/
  ruleset cursor без изменения существующих rulesets и acceptances. Отсутствие
  channel configuration означает disabled; один guild имеет максимум один
  managed message.
- `RulesPublicationService` создаёт первую публикацию, не редактирует актуальную,
  обновляет её при новой published version и восстанавливает удалённую с новым
  persisted message ID. Startup/RESUME и authenticated loopback
  `POST /control/v1/rules/publication/sync` используют один сериализованный sync.
  Missing/unsupported channel, Forbidden, NotFound и transient Discord API
  failures дают structured result и не останавливают runtime.
- Публичное managed message переиспользует `build_rules_embed` и существующий
  `RulesAcceptanceView` с `kanami:rules:accept:v1`; второй acceptance flow не
  создан, semantics `requires_reacceptance` не изменена. Web Admin UI/config и
  automatic web publish sync реализованы в R2B.
- Partial failure `send succeeded -> save_delivery failed` компенсируется
  best-effort удалением только нового create/recreate message. Cleanup failure
  логируется, но не скрывает исходную persistence exception; UPDATE
  существующего managed message никогда не удаляется.
- Реализован Rules Publication R2B на `/admin/rules`: существующий Bot Control
  options endpoint предоставляет text/news channels и имена, а fixed-purpose
  configure/disable endpoints валидируют guild/type/permissions в bot process.
  Same channel не сбрасывает cursor; change/disable удаляет старое managed
  message, считает NotFound успехом и сохраняет прежнюю configuration при
  Forbidden/transient cleanup failure.
- Web Admin показывает disabled/unpublished/current/stale publication state из
  PostgreSQL даже при недоступном Bot Control, поддерживает CSRF-protected
  OWNER/ADMIN Save/Disable/Manual Sync и безопасные русские результаты ошибок.
  Publish вызывает sync только после DB commit; Discord/control failure не
  откатывает Rules version и оставляет drift для manual или READY/RESUME repair.
- Rules Publication v1 развёрнут на production-host с production commit `9e149eb` и
  Alembic head `e1a7c4d92b60`. Migration успешно применена: новые publication
  columns и constraints присутствуют, а начальное состояние после migration
  было disabled. Web Admin показывает publication section и получает
  отфильтрованные для configured guild text/news channels через существующий
  Bot Control без `DISCORD_TOKEN`; для доступного канала подтверждены требования
  `view_channel`, `send_messages` и `embed_links`.
- Production smoke подтвердил полный publication lifecycle: configure первого
  channel, создание managed message первым manual sync, сохранение
  channel/message/ruleset cursor, идемпотентный `already_current` без duplicate,
  восстановление удалённого message с `recreated` и сохранением нового message
  ID. Смена channel удалила прежнее managed message, очистила старый cursor и
  создала новую managed publication в новом channel. Disable удалил managed
  message и очистил channel/message/ruleset cursor; повторный configure после
  disable и новая публикация также прошли успешно.
- Полный restart `kanami.service` подтвердил self-healing: READY reconciliation
  нашёл существующее managed message с `already_current`, сохранил прежний
  message ID и не создал duplicate. Persistent acceptance View/button продолжил
  работать после restart. Уже принявший Rules 1.0 пользователь получил
  already-accepted response без duplicate acceptance; другой пользователь
  успешно принял текущую версию, после чего acceptance count обновился.
- После smoke publication намеренно оставлена включённой в закрытом тестовом
  Discord-канале. Managed publication отражает Rules version 1.0 и persisted
  ruleset ID 1, состояние — current/`already_current`. Production smoke не выявил
  `ERROR`, `Exception` или `Traceback`; failed systemd units — 0.
- Реализован Rules Compliance Foundation R3A. Required checkpoint определяется
  по `published_at + id`: последний опубликованный `requires_reacceptance=true`
  либо первая опубликованная версия; archived history учитывается, draft и
  version strings не влияют на ordering. Реальные acceptance после checkpoint и
  не позже current дают compliant; остальные пользователи pending либо overdue.
- Migration `a4f6c8d21e73` поверх `e1a7c4d92b60` добавляет nullable bounded
  `rulesets.reacceptance_grace_days`. Deadline вычисляется в UTC от publication
  checkpoint; без grace автоматического overdue нет. Aggregate считает только
  current non-bot members из PostgreSQL. `/admin/rules` показывает read-only
  current/checkpoint/deadline/compliant/pending/overdue/total и безопасный warning
  при ошибке summary. Acceptance и Rules Publication lifecycle не изменены.
- Rules Compliance R3A развёрнут на production commit `560703e`; Alembic rollout
  с `e1a7c4d92b60` до `a4f6c8d21e73` успешно добавил nullable PostgreSQL
  `smallint` `rulesets.reacceptance_grace_days` и constraint
  `ck_rulesets_reacceptance_grace_days`. Оба service остались active/running,
  Discord Gateway подключился, синхронизировано 16 application commands, а в
  startup/journal smoke не обнаружено новых `ERROR`, `Exception` или `Traceback`.
- Production Web Admin и прямой read-only PostgreSQL smoke дали одинаковый
  compliance summary для Rules 1.0: 4 compliant, 106 pending, 0 overdue, всего
  110 current non-bot members. Required checkpoint — первая опубликованная
  версия 1.0, deadline отсутствует; это подтверждает, что без grace непринявшие
  пользователи остаются pending. Managed Discord publication во время smoke была
  отключена и не влияла на persisted compliance calculation. Полный отчёт:
  [RULES_COMPLIANCE_PRODUCTION_SMOKE.md](RULES_COMPLIANCE_PRODUCTION_SMOKE.md).
- Production Rules v1 и Web Admin Rules v1 проверены end-to-end: опубликованные
  правила отображаются, acceptance сохраняется, повторный `/rules` видит ранее
  принятое соглашение, draft создаётся и удаляется, а Web Admin показывает
  current version и acceptance counts. Production migration применена до
  `b6e2c8f91a47`, совпадающего с Alembic `heads`.
- Production reverse proxy вручную исправлен и проверен по HTTPS: точный `/`
  возвращает `302 /admin/`, `/admin/` для неавторизованного клиента возвращает
  `303 /admin/login`, произвольный path и `/control` возвращают `404`.
- Унифицирован публичный routing contract официальных Nginx/Caddy templates:
  точный `/` возвращает `302` на `/admin/`, только `/admin/*` проксируется в Web
  Admin, остальные paths возвращают proxy-side `404`. Специальные Nginx limits и
  очистка forwarded headers сохранены, Bot Control не опубликован; добавлена
  offline regression-проверка шаблонов. Ruff lint/format и полный hermetic suite
  проходят: 1135 passed, 13 integration tests skipped без `TEST_DATABASE_URL`,
  34 существующих discord.py warnings.
- Реализован Web Admin Rules v1 на `/admin/rules`: OWNER/ADMIN видят current
  published, историю и acceptance counts, создают draft-копию current,
  редактируют/preview/publish/delete draft и просматривают persisted имена и
  timestamps принявших версию пользователей. Published/archived immutable.
- Publish использует общий Rules service/repository и caller-owned transaction:
  guild row блокируется, прежний published архивируется, draft публикуется и
  important audit event создаётся до единого commit. Добавлены события
  `rules.draft_created`, `rules.draft_updated`, `rules.draft_deleted` и
  `rules.published`; `requires_reacceptance` сохраняется без enforcement.
- Rules draft валидируется по точному embed contract `/rules`: title 256,
  description 4096, footer 2048 и общий размер 6000 символов. Preview и Save
  показывают конкретную понятную ошибку, publish повторяет domain-проверку;
  initial published v1.0 не изменялся.
- Update/delete draft используют zero-row `RETURNING` как immutable conflict при
  конкурентном publish; ложный delete и `rules.draft_deleted` audit исключены.
  В duplicate-version conflict преобразуется только constraint
  `uq_rulesets_guild_version`, остальные `IntegrityError` не маскируются и
  откатывают транзакцию.
- Добавлены regression-проверки fresh authorization существующей web session и
  HTML escaping version/title/content/change summary/persisted display name.
- Operations UI русифицирован без изменения расчётов: coverage явно относится к
  периоду с начала мониторинга, а pre-history различает «Доля до начала
  мониторинга» и «Минут до начала мониторинга»; подзаголовок описывает
  диагностику и доступность компонентов Kanami.
- Web Admin resources явно создаются с `read_only=False`, поэтому connection не
  получает `default_transaction_read_only`; deployment требует отдельную DB role
  только со scoped Rules/audit writes и необходимыми read grants.
- Итоговые проверки fixing pass: targeted suite — 225 passed, 2 skipped;
  полный hermetic suite — 1131 passed, 13 skipped без `TEST_DATABASE_URL`, 34
  существующих discord.py warnings. Ruff lint/format и `git diff --check`
  проходят.

- Реализован Rules v1: migration `b6e2c8f91a47` добавляет versioned `rulesets`
  и идемпотентные `rule_acceptances`. Partial unique published index исключает
  неоднозначный current ruleset, composite foreign keys удерживают guild scope,
  а опубликованный content не имеет mutation API.
- Discord-независимые repository/service операции получают current rules,
  проверяют и записывают acceptance точной версии и считают accepted count.
  Guild-only `/rules` показывает DB content с persistent stable-ID кнопкой;
  manager-only `/rules-status` показывает version/publication/count.
- Optional `RULES_ACCEPTED_ROLE_ID` выдаётся только как следствие принятия.
  PostgreSQL остаётся source of truth; missing role/permission не откатывает
  acceptance. Missing/grant failure возвращает пользователю отдельное
  предупреждение вместо ложного welcome; отсутствие настройки, уже имеющаяся
  или успешно выданная роль остаются нормальным успехом. Повторный callback не
  создаёт строки и может восстановить роль.
- Generic migration не содержит production data. Транзакционный
  `scripts/publish_rules_v1.sql` является strict initial-only bootstrap:
  принимает явный guild ID после deployment migration, требует существующий
  guild и отсутствие любых rulesets, а повторный запуск падает до `INSERT` без
  архивирования/замены. Для следующих версий используется Web Admin; reminders,
  grace/diff/rollback/scheduling/forced reacceptance отложены.

- Реализован Stage W1.1: защищённая OWNER/ADMIN read-only страница
  `/admin/system` в существующей тёмной server-rendered стилистике. Она
  показывает общий статус и время формирования, Git commit/branch, Alembic
  revision, uptime Web Admin process, PostgreSQL health/latency/размер, Bot и
  Bot Control status, а также count/last confirmation/freshness открытых Voice и
  Game sessions. Выключенный Game Tracking отображается как Disabled.
- Operations route получает единый DTO из отдельного service; Git adapter,
  специализированный SELECT-only PostgreSQL repository и existing Bot Control
  изолированы друг от друга. Недоступные Git, PostgreSQL, Bot Control или
  отдельная метрика дают честный Unknown/Недоступно без HTTP 500. Миграций,
  write actions, polling и frontend framework W1.1 не добавляет. Два Game
  settings перенесены без изменения env/default в общий `RuntimeSettings`,
  доступный обоим processes.
- W1.1 regression suite полностью offline проверяет OWNER/ADMIN access,
  authentication redirect, rendering/fallbacks, shared Game settings и bounded
  repository SQL и deployment env/unit contract. Полный hermetic suite: 1019
  passed, 13 skipped без
  `TEST_DATABASE_URL`, 34 существующих discord.py dependency warnings; Ruff
  format/lint проходят.
- Production smoke W1.1 выявил две deployment-зависимости без дефекта read
  model: отдельный web env не содержал Game runtime snapshot, а Git отклонил
  checkout другого владельца как dubious ownership. Deployment docs и examples
  теперь требуют явный безопасный shared subset в
  `/etc/kanami/kanami-web-admin.env` без `DISCORD_TOKEN`, а для `kanami-web` —
  только user-scoped `safe.directory=/opt/kanami` с отдельным home. Bot-only
  installer/updater намеренно не изменены; ownership и write permissions
  checkout остаются у `kanami`.
- Реализован Stage W1.2 без schema/config/write изменений: внутренние health
  results используют `HEALTHY`/`DEGRADED`/`UNAVAILABLE`/`NEUTRAL`, а overall
  status агрегирует critical availability и warnings без строковой логики HTML.
  Bot Control unavailable даёт warning, offline bot и PostgreSQL unavailable —
  critical problem, Git Unknown и disabled Game Tracking остаются neutral.
- Voice stale threshold вычисляется как
  `max(VOICE_CHECKPOINT_INTERVAL_SECONDS * 3, 180)`, Game — как
  `max(GAME_CONFIRM_INTERVAL_SECONDS * 3, 180)`.
  Ноль открытых Voice sessions или игр считается healthy и явно отображается
  без ложного stale. Один timezone-aware UTC check time используется всеми
  evaluation rules.
- Добавлен read-only блок «Целостность данных»: bounded aggregate SELECT для
  Voice и Games считает duplicate open-session groups и нарушения трёх
  существующих timestamp invariants. Проверки не читают отдельные rows, не
  принимают user input и не меняют данные; failure isolation W1.1 сохранена.
- W1.2 Operations suite содержит 37 deterministic тестов (на 23 больше W1.1),
  включая threshold boundaries, overall aggregation, integrity rendering,
  authorization и исключения всех источников. Полный hermetic suite: 1042
  passed, 13 skipped без `TEST_DATABASE_URL`, 34 существующих discord.py
  dependency warnings.
- Реализован W1.3: bot-owned минутный runner сохраняет компактные operational
  health observations для Gateway, PostgreSQL, Voice и опционального Game
  Tracking; migration `f2a6c9d41b73` добавляет отдельную таблицу с guild/time
  индексом, а каждая запись удаляет observations старше восьми дней. Operations
  read path остаётся SELECT-only; Web Admin не получает `DISCORD_TOKEN`.
- `/admin/system` теперь перед текущими W1.2 diagnostics показывает sampled
  availability за 24 часа/7 дней, число фактических проверок, последний Healthy,
  incident count, полноту окна и последние incidents с recovery. Пустая,
  частичная или временно недоступная история не вызывает HTTP 500 и не выдаётся
  за непрерывный uptime.
- W1.3 не является внешним monitoring, не отправляет alerts и не выполняет
  restart, repair или self-healing. Длительный PostgreSQL outage может оставить
  разрыв в PostgreSQL-owned history; UI честно показывает coverage вместо
  восстановления несуществующих samples.
- W1.3 и полный regression suite проходят: `1077 passed`, `13 skipped` без
  `TEST_DATABASE_URL`, 34 существующих discord.py dependency warnings; Ruff
  lint и format check проходят.
- После code review freshness всех открытых Voice/Game sessions использует
  oldest checkpoint и отдельный NULL count; один fresh checkpoint больше не
  скрывает stale/missing peer. Voice threshold следует configured
  `max(3 × VOICE_CHECKPOINT_INTERVAL_SECONDS, 180s)`, а observation runner
  запускается только после успешного startup reconciliation.
- Availability стала sampling-aware: 24h/7d имеют 1440/10080 ожидаемых минутных
  slots. До начала durable history они помечаются `Not monitored`, а `Missing`
  начинается только внутри monitored period; восьмидневный read-only lookback
  позволяет считать начальный gap 7d окна, если recorder существовал раньше.
  UI показывает nominal/monitored/covered, coverage, Missing, Not monitored,
  `History available since` и longest gap. Ненакопленная история, внутренний gap
  и missing tail дают `Partial`, но pre-history не изображается outage.
- Минутный observation loop использует fixed monotonic start-to-start cadence:
  штатное время выполнения не накапливает drift, а overrun пропускает
  просроченные ticks без catch-up storm; idempotent start и cancellation сохранены.

- Реализован opt-in Game Tracking backend v1: при
  `GAME_TRACKING_ENABLED=false` Presence Intent и все game runtime components
  выключены; при `true` `on_presence_update` сохраняет только Playing identity.
  Custom/Spotify/Streaming/Watching/Competing, status/platform и Rich Presence
  details/state/secrets не сохраняются.
- Discord-независимый selector предпочитает stable `application_id`, иначе
  normalized name, сохраняет текущую игру при reorder нескольких Playing
  activities и передаёт state machine не более одной candidate. Per-member
  transitions сериализуются `guild_members FOR UPDATE`; duplicate/same/switch/
  stop/member-leave выполняются идемпотентно в caller-owned transaction.
- Migration `c5b7e1d9a024` добавляет `game_sessions` с guild/member FK,
  display/name identity, UTC start/confirmation/end, history indexes и partial
  unique open-session constraint. Batched checkpoint использует один UPDATE;
  startup/reconnect закрывает старую session на последнем confirmation и
  открывает current game от нового observation, не засчитывая crash downtime.
  Повторный READY без disconnect generation session не режет; clean shutdown
  выполняет финальный bounded checkpoint. Команд, Profile/Web-полей, каталога
  игр, rankings и achievements этот этап не добавляет.
- Реализована guild-only private `/games [user] [period]` с default invoking
  user/`30d` и choices `7d`/`30d`/`90d`/`all`. Отдельный read service считает
  подтверждённое время, уникальные игры, игровые дни в `REPORT_TIMEZONE`, TOP-5,
  последнюю игру и longest session; open rows ограничены confirmation. Bot-wide
  opt-in и bot guards сохранены, schema/migration не изменялись.
- G1.1 сохраняет raw `game_key`/`application_id`, но read-side `/games`
  canonicalizes trimmed/casefold `game_name`; известный exact trailing suffix
  ` with Medal` удаляется case-insensitively. Поэтому разные Presence identities
  одной игры объединяются, а разные Medal games с общим application ID остаются
  раздельными без rewrite history, alias table или migration.
- После G1.1 targeted `GameStatisticsService` suite содержит 25 passed; полный
  hermetic suite — 1204 passed, 13 skipped без `TEST_DATABASE_URL`, 39
  dependency warnings. Ruff и `git diff --check` проходят.
- После G1 targeted game/command suite содержит 81 passed и 1 ожидаемо skipped
  PostgreSQL test; полный hermetic suite — 1194 passed, 13 skipped без
  `TEST_DATABASE_URL`, 39 существующих dependency warnings. Ruff проходит.
- Реализован Web Admin Dashboard v1 как authenticated стартовая `/admin/`:
  responsive server-rendered overview показывает configured guild name, current
  non-bot members, пользователей сейчас в non-AFK Voice, открытые logical Voice
  sessions, общее Voice-время сегодня и за rolling 30 дней. Message count,
  charts и неподтверждённые runtime metrics не добавлены.
- Новый testable Dashboard service отделяет route, read repositories, DTO и HTML.
  Current-state counts агрегируются одним bounded PostgreSQL statement, а Voice
  totals переиспользуют canonical `VoiceStatisticsService` в одной
  `REPEATABLE READ` session с общими threshold/exact/estimated/AFK/bot rules.
  `REPORT_TIMEZONE` перенесён в общий `RuntimeSettings`, чтобы Web Admin и bot
  process использовали одинаковую границу «сегодня».
- Bot profile/control availability проверяется независимо от PostgreSQL:
  недоступный Bot Control даёт Unknown/Unavailable, но не скрывает server data.
  OWNER-only Administrators/Audit links скрыты от ADMIN; существующая backend
  authorization не менялась. Новых таблиц, migration, write paths и frontend
  dependencies нет. Полный hermetic suite: 1005 passed, 13 skipped без
  `TEST_DATABASE_URL`, 34 существующих discord.py dependency warnings; Ruff и
  `git diff --check` проходят.
- Реализован Member Profile v1: guild-only ephemeral `/profile [user]` по
  умолчанию показывает вызывающего участника, отклоняет bot target и до чтения
  статистики применяет reusable policy. Собственный профиль доступен всем;
  чужой — только при совпадении stable configured Purple/Gold Role ID. Пустая
  privileged-конфигурация fail-closed, а display names ролей не участвуют в
  authorization.
- Discord-независимый `MemberProfileService` возвращает единый result DTO,
  переиспользует существующий aggregate voice query для all-time/30d и bounded
  achievement read. Compact embed показывает display name/avatar, определимый
  Kanami role, локальную дату вступления и полные дни, Voice и число достижений.
  Новых таблиц, migration, background worker, XP и progression engine нет.
- Добавлены optional typed `DISCORD_GUEST_ROLE_ID`,
  `DISCORD_INITIATED_ROLE_ID`, `DISCORD_GUARDIAN_ROLE_ID`,
  `DISCORD_PURPLE_ROLE_ID`, `DISCORD_GOLD_ROLE_ID`; обновлены `/help`, env example
  и configuration/architecture docs. Targeted suite: 119 passed. Полный
  hermetic suite: 950 passed, 11 skipped без `TEST_DATABASE_URL`, 34
  discord.py dependency warnings; Ruff проходит.
- Реализован Web Admin Stage 6A foundation для managed administrators: migration
  `8d44cacc791e` добавляет исторические `web_admin_access_grants` с единственным
  активным grant на guild/user; caller-owned repository и service обеспечивают
  идемпотентные grant/revoke и important audit events. Запись остаётся только в
  основном bot process через две фиксированные authenticated loopback Bot
  Control операции; `guild_id` не принимается из HTTP. Runtime wiring передаёт
  общую session factory в атомарный `WebAdminAccessControlService`.
- Managed authorization подключает active DB grants как дополнительную роль
  ADMIN, сохраняя все env IDs постоянными OWNER. OWNER имеет приоритет при
  пересечении источников; обе роли после resolution проходят прежнюю current
  non-bot configured-guild membership проверку. Новый web repository выполняет
  только bounded SELECT через read-only Web Admin resources; fresh authorization
  перед bot-profile writes сохранена.
- Targeted managed authorization suite содержит 176 passed и 2 ожидаемо skipped
  PostgreSQL tests. Полный hermetic suite содержит 818 passed, 10 skipped без
  `TEST_DATABASE_URL` и 29 существующих discord.py dependency warnings; Ruff
  lint проходит.
- Реализован OWNER-only Web Admin management UI: оба env OWNER отображаются
  постоянными и не имеют revoke controls; active non-OWNER grants показываются
  как ADMIN с grant metadata. ADMIN не видит navigation link и получает server-side
  403 на прямые GET/POST. Grant/revoke требуют strict form, CSRF, fresh OWNER
  authorization и rate limit; target OWNER, duplicate/inactive grant и stale/non-bot
  membership обрабатываются без небезопасной прямой DB mutation.
- Web Bot Control client расширен двумя фиксированными access URLs с прежними
  Bearer secret, actor header, bounded body, timeout и controlled parsing. JSON
  содержит только `user_id`; actor берётся из server-side session. Targeted suite:
  200 passed, 2 skipped. Полный suite: 835 passed, 10 skipped и 29 существующих
  discord.py dependency warnings.
- Добавлен OWNER-only `GET /admin/audit`: fresh authorization не доверяет роли
  session, а единственный SELECT ограничен configured guild, allowlist
  grant/revoke/server-setting events, newest-first ordering и 100 строками. Для
  access history UI показывает время в `REPORT_TIMEZONE`, действие и
  actor/target identity; setting history использует только persisted
  `setting_key`/`source` для semantic transition. Discord setting value IDs и
  delivery/retry internals не отображаются.
- Targeted Web Admin/audit regression suite содержит 236 passed и 2 ожидаемо
  skipped PostgreSQL tests. Полный hermetic suite содержит 841 passed, 10
  skipped без `TEST_DATABASE_URL` и 29 существующих discord.py dependency
  warnings; Ruff lint проходит.
- Реализован backend foundation Stage 6B.1 без Web UI: migration
  `3e7b9c2a6f41` добавляет одну guild-specific строку tri-state overrides для
  autorole/audit/anniversary/return. Env остаётся baseline; DB `value` имеет
  приоритет, а `disabled` явно выключает feature. Пустая таблица сохраняет
  прежнее production-поведение.
- Bot process использует общий refreshable effective-settings provider.
  Успешный строгий loopback Bot Control write инвалидирует cache, поэтому все
  четыре runtime path применяют значение без restart. Mutation и important
  history-only `web_admin.server_setting_changed` атомарны; no-op audit не
  создаёт. Web Admin read model выполняет только один SELECT через read-only
  connection и теперь используется Stage 6B.2 Server Settings UI.
- Финальная Stage 6B.1 validation на PostgreSQL 17: targeted PostgreSQL suite —
  66 passed; полный suite с реальным `TEST_DATABASE_URL` — 886 passed, 0
  skipped и 29 существующих discord.py dependency warnings. Migration
  `3e7b9c2a6f41` успешно применена локально до production, после test cleanup
  таблица `guild_server_settings` содержала 0 строк.
- Stage 6B.1 объединён с `main` на revision `20dbd6a`, GitHub CI прошёл, а
  production deployment на production-host применил migration `3e7b9c2a6f41`.
  Таблица `guild_server_settings` осталась пустой после migration/startup, все
  четыре configured env baseline сохранили effective поведение. Startup
  provisioning и Voice startup reconciliation прошли успешно. Bot Control
  no-op `audit_log_channel` в mode `env` вернул `changed=false` без DB row или
  audit event; оба service остались active. Полный результат зафиксирован в
  `docs/WEB_ADMIN_STAGE6B1_PRODUCTION_SMOKE.md`.
- Реализован Stage 6B.2 `GET/POST /admin/server-settings` для OWNER и managed
  ADMIN. Страница объединяет существующий SELECT-only effective/source read
  model с bounded options из bot runtime, поддерживает явные режимы
  `env`/`value`/`disabled`, безопасно показывает исчезнувший current target и
  использует PRG для changed/no-op результата. POST требует strict form, CSRF,
  fresh OWNER/ADMIN authorization и общий rate limiter; actor берётся только из
  session, а browser не передаёт `guild_id`, actor или control credentials.
- Добавлен bearer-protected read-only Bot Control
  `GET /control/v1/server-settings/options`: configured guild фиксирован bot
  process, допустимые роли и text/news channels фильтруются по hierarchy и
  effective permissions, сортируются deterministically и возвращаются без
  permission dump или Discord internals. Web Admin DB остаётся read-only, все
  mutations используют прежний loopback POST и bot-side повторную validation.
- Финальная Stage 6B.2 validation с PostgreSQL 17 и реальным
  `TEST_DATABASE_URL` содержит 922 passed, 0 skipped и 29 существующих
  discord.py dependency warnings. Stage объединён с `main` на revision
  `a585149`, GitHub CI прошёл и deployment выполнен fast-forward без dependency
  или migration changes.
- Production smoke подтвердил bearer-only Bot Control options contract, OWNER
  UI и сохранение OWNER-only navigation boundary, четыре runtime dropdown
  source, ENV no-op без DB/audit записи, реальный autorole `value` override с
  одной DB/audit записью и runtime apply без restart. Controlled leave/rejoin
  подтвердил фактическую выдачу выбранной роли. Финальный snapshot сохранял
  active DB autorole override; возврат к ENV не выполнялся. Подробности
  зафиксированы в `docs/WEB_ADMIN_STAGE6B2_PRODUCTION_SMOKE.md`.
- Stage 6B.3 UX/audit polish объединён с `main` на revision `599584a`, GitHub CI
  прошёл, deployment выполнен fast-forward без dependencies или migrations.
  Финальный suite с PostgreSQL 17 и реальным `TEST_DATABASE_URL` содержит
  932 passed, 0 skipped и 29 существующих discord.py dependency warnings.
  Server Settings dropdown
  явно выбирает совпадающий effective ENV/DB object и оставляет нейтральный
  placeholder для disabled/missing object. OWNER-only audit page показывает
  persisted `web_admin.server_setting_changed` как человекочитаемое изменение
  настройки и semantic mode transition без раскрытия raw Discord value ID.
- Production browser smoke подтвердил matching selected DB autorole, видимое ENV
  channel value, semantic setting transitions и сохранность access grant/revoke
  events.
  Read-only smoke сохранил одну settings row и пять setting-change events; active
  DB autorole override остался активным. Перезапущен только Web Admin, bot
  process не перезапускался. Подробности зафиксированы в
  `docs/WEB_ADMIN_STAGE6B3_PRODUCTION_SMOKE.md`.

- Реализован Web Admin Stage 5 public deployment hardening: safe default bind остаётся `127.0.0.1`, а explicit private RFC1918/IPv6 ULA bind требует `WEB_ADMIN_ALLOW_PRIVATE_BIND=true`; wildcard, hostname, link-local и public IP отклоняются. Uvicorn не доверяет proxy headers, а bot control по-прежнему принимает только `127.0.0.1`.
- Все Web Admin responses получают central CSP/nosniff/frame/referrer/permissions/no-store headers. Public health раскрывает только общий status. Bot-profile writes после CSRF повторяют allowlist/current membership authorization, fail closed с session revoke и защищены bounded process-local limiter. Добавлены Caddy, same-host/remote Nginx и hardened systemd examples, firewall/OAuth/secrets guide для трёх deployment-схем.
- Stage 5 targeted Web Admin/config/security regression suite содержит 210 passed и 2 ожидаемо skipped PostgreSQL tests. Полный hermetic suite содержит 788 passed, 10 skipped без `TEST_DATABASE_URL` и 29 существующих discord.py dependency warnings; `uv lock --check`, Ruff lint/format и `git diff --check` проходят.
- Добавлена публичная guild-only `/activity [period]` с default `30d` и choices `7d`/`30d`/`90d`: bounded read-only query переиспользует canonical eligible voice intervals, а чистая timezone-aware агрегация вычисляет user-time Top-3 часов, нормализованный weekday, quietest 3-hour bucket и 8×7 heatmap `· ░ ▒ ▓ █` без новой schema/migration или второго запроса.
- После реализации `/activity` targeted suite содержит 55 passed; полный набор — 666 passed и 10 ожидаемо skipped PostgreSQL integration tests без `TEST_DATABASE_URL` с 29 существующими dependency warnings.
- Добавлена guild-only `/anniversaries`: команда использует `Member.joined_at`, календарь `REPORT_TIMEZONE`, исключает bots, сортирует годовщины в inclusive-окне 30 дней и корректно переносит 29 февраля на 28 февраля в невисокосный год; сама read-only команда не требует persistence.
- Реализованы optional автоматические поздравления через `DISCORD_ANNIVERSARY_CHANNEL_ID`: ежедневный local-time worker переиспользует anniversary feature, сохраняет `member.anniversary` в общий `audit_events` outbox и будит существующий retry delivery runner. Migration `2f6a8c4d1e90` добавляет только partial unique idempotency index; новой таблицы нет.
- Реализованы optional уведомления о возвращении через `DISCORD_RETURN_CHANNEL_ID`: первый join отличается от повторного по permanent `member.left` history, порог задаётся `MEMBER_RETURN_MIN_ABSENCE_SECONDS=86400`, а `member.returned` хранит immutable snapshot all-time voice/messages/achievements и доставляется общим retry runner. Migration `7c2d9a4e6f10` добавляет только partial unique idempotency index; `guild_members.joined_at` сохраняет current-state семантику.
- Добавлен отдельный `kanami-web-admin` foundation на Starlette/Uvicorn: loopback-only server-rendered `GET /admin/`, PostgreSQL-backed JSON `GET /admin/health`, небольшие существующие table counters, lifespan-owned SQLAlchemy engine и enforced PostgreSQL read-only transactions. Discord process и DB schema не изменены; web authentication и actions отложены.
- Добавлен `GET /admin/members`: текущие non-bot memberships загружаются двумя bounded set-based SELECT без N+1, по 50 строк; числовой поиск использует точный Discord ID, текстовый — case-insensitive nickname/global name/username. Lifetime voice сохраняет eligibility-семантику команд, text суммирует `daily_text_activity`, achievements считает persisted unlocks, `joined_at` показывается в UTC, а display fallback равен nickname → global name → username → ID. Detail page не реализована.
- Alembic revision `a8d3e5f7b912` добавляет nullable `discord_users.username`, `discord_users.global_name` и guild-specific `guild_members.nickname` без SQL backfill и avatar данных. Full startup/reconnect provisioning автоматически заполняет legacy rows; join/member update/configured-guild user update/remove поддерживают mutable identity независимо от Audit Logging.
- Full Member snapshot напрямую обновляет nullable nickname, поэтому снятие nickname сохраняется как `NULL`; partial User snapshot имеет явный признак неполной guild identity и не включает nickname/left_at в `ON CONFLICT DO UPDATE`. Remove сохраняет последнюю identity и выставляет `left_at`, следующий full provisioning/rejoin возвращает membership в active state (`left_at=NULL`); исторический repair не выполняется.
- Добавлен read-only `GET /admin/members/{discord_id}` для active/departed non-bot membership configured guild: identity fallback, lifetime voice/messages, catalog-enriched persisted achievements и последние 20 lifecycle events загружаются двумя SELECT без N+1. `/admin/members` теперь также явно scoped по общему `RuntimeSettings.discord_guild_id`; Web Admin требует существующий `DISCORD_GUILD_ID`, но не Discord token.
- Реализован Web Admin Stage 3C authentication: отдельный `aiohttp` Discord OAuth2 Authorization Code adapter использует только `identify`, one-shot random state, PKCE S256 и временный access token для validated `/users/@me`; opaque sessions и OAuth transactions bounded и живут только в памяти процесса. Exact public allowlist оставляет открытыми login/callback/health, deny-by-default middleware закрывает остальные `/admin/...`, а POST logout требует CSRF и отзывает server record. Uvicorn access log отключён; client secret, code, verifier и Discord tokens не сохраняются и не выводятся.
- После Stage 3C targeted Web Admin/auth suite содержит 77 passed и 2 ожидаемо skipped PostgreSQL tests; полный набор содержит 687 passed, 10 skipped без configured `TEST_DATABASE_URL` и 29 существующих dependency warnings. Ruff lint/format проходят; OAuth tests полностью offline.
- Реализован Web Admin Stage 3D authorization: отдельный Discord-independent policy service требует `WEB_ADMIN_ALLOWED_USER_IDS` и current non-bot membership configured guild, проверяемый одним bounded SELECT через read-only session factory. OAuth callback создаёт WebSession только после положительного решения; отказ возвращает нейтральный 403, удаляет OAuth transaction cookie и не выдаёт session cookie. Allowlist нормализует whitespace/duplicates/empty items, валидирует positive decimal snowflakes и по умолчанию запрещает доступ всем. Discord API, bot runtime, schema и migrations не изменены.
- После исправления Stage 3D config parsing targeted Web Admin/auth suite содержит 104 passed и 2 ожидаемо skipped PostgreSQL tests; полный hermetic suite содержит 735 passed, 10 skipped без `TEST_DATABASE_URL` и 29 существующих dependency warnings. Ruff lint и format check проходят.
- Исправлена реальная загрузка `WEB_ADMIN_ALLOWED_USER_IDS` из environment: `pydantic-settings` больше не пытается JSON-decode complex `frozenset` до comma-separated validator, потому что поле явно использует `NoDecode`. Regression tests создают `WebSettings` через environment source и покрывают single/multiple IDs, whitespace, duplicates, empty/missing deny-all и malformed/zero/negative значения.
- Реализован Web Admin Stage 4 bot profile MVP: новая защищённая страница показывает профиль собственного bot member и через отдельные CSRF-protected POST позволяет установить либо явно сбросить guild-specific nickname/avatar. PNG/JPEG uploads ограничены 8 MiB и проверяются по MIME type и magic signature до отправки и повторно в bot process.
- Discord process при явном `DISCORD_BOT_CONTROL_ENABLED=true` поднимает authenticated control API только на `127.0.0.1`; API содержит пять фиксированных profile operations, всегда выбирает configured guild и `guild.me`, сериализует `Member.edit` и не принимает произвольный Discord route/target/guild от browser. Web Admin использует отдельный shared secret и не требует `DISCORD_TOKEN`; disabled/unavailable control работает fail-closed.
- Добавлены offline regression tests конфигурации, auth boundary, fixed operations, configured-guild/own-member scope, Discord error mapping, web client, CSRF, nickname/reset, PNG/JPEG upload, fake/oversized content, avatar reset и unavailable-control UX. Schema и migrations не изменены.
- После Stage 4 security hardening targeted control/profile suite содержит 37 passed; расширенный Web Admin/auth/audit regression suite до hardening — 267 passed и 2 ожидаемо skipped PostgreSQL tests. Полный hermetic suite содержит 772 passed, 10 skipped без `TEST_DATABASE_URL` и 29 существующих discord.py dependency warnings.
- Avatar endpoint больше не полагается на `Content-Length` или Starlette `max_part_size` для file parts: bounded ASGI receive прекращает передачу multipart parser выше общего лимита 8 MiB + 64 KiB overhead даже при отсутствующем/ложном header. Неожиданные parser failures получают безопасный structured exception log без form/upload/session payload, а control actor дополнительно ограничен максимальным uint64 Discord snowflake.
- Создано краткое описание проекта и его базовой структуры.
- Создан начальный архитектурный документ без преждевременно принятых решений.
- Зафиксированы правила работы для AI-агентов.
- Добавлен безопасный шаблон будущих переменных окружения.
- Добавлены исключения для секретов, Python-кэша, виртуальных окружений, IDE, логов, временных файлов, локальных данных и резервных копий.
- Настроены кроссплатформенные окончания строк через `.gitattributes`.
- Утверждены границы MVP: voice-статистика, текстовые агрегаты без содержимого сообщений, базовый member tracking и slash-команды статистики.
- Зафиксированы модель логических voice-сессий и сегментов, правила AFK/Stage, минимальная длительность и обработка exact/estimated интервалов.
- Зафиксированы требования к приватности текстовой статистики без хранения содержимого сообщений.
- Выбрана PostgreSQL с миграциями; хранение времени стандартизировано на UTC.
- Зафиксирована первоначальная ориентация на один guild с обязательным использованием `guild_id` для будущего расширения.
- Выбраны Python 3.13, uv, `pyproject.toml`, проектное виртуальное окружение и хранение `uv.lock` в Git.
- Выбраны discord.py, asyncio и application/slash commands без `MESSAGE_CONTENT`.
- Выбраны async SQLAlchemy, asyncpg и Alembic; изменения схемы без миграций запрещены.
- Утверждены modular monolith, feature-first структура и разделение Discord handlers, application/services и persistence/repositories.
- Уточнена voice-модель: логическая сессия состоит из атомарных exact/estimated интервалов, разделяемых при изменении канала; voice flags отложены за пределы первой business-схемы.
- Первоначальное container-направление пересмотрено: первый официальный deployment flow ориентирован на Debian 13, systemd и локальный PostgreSQL.
- Выбран pytest; unit- и DB integration-тесты разделяются.
- Retention оформляется отдельной background task, которая в MVP может выполняться внутри процесса бота.
- Выбраны pydantic-settings, environment variables и startup-валидация конфигурации; утверждён минимальный набор переменных.
- Утверждено логирование через стандартный Python `logging` в stdout/stderr без секретов и application-local log-файлов.
- Утверждена явная политика обновления зависимостей и review/test для `uv.lock`.
- Ruff выбран как linter и formatter; для async-тестов выбран pytest-asyncio.
- Alembic запускается отдельным deployment step/command, а не автоматически при старте приложения.
- Зафиксированы единая точка запуска, graceful shutdown и централизованное управление background tasks.
- Создан `pyproject.toml` с Python 3.13, src-layout, runtime/dev-зависимостями и минимальной конфигурацией Ruff и pytest.
- Штатными средствами uv создан версионируемый `uv.lock` и проектное окружение `.venv`.
- Создан пакет `discord_stats_bot` с Settings, безопасной настройкой logging и запуском через `python -m discord_stats_bot`.
- Добавлены unit-тесты импорта, defaults и ошибок валидации обязательных и положительных числовых настроек.
- Добавлена startup-валидация `DATABASE_URL` через SQLAlchemy URL parser с обязательным driver `postgresql+asyncpg` без создания engine.
- Добавлена проверка IANA timezone через `zoneinfo.ZoneInfo`; для воспроизводимости на Windows и в минимальном Linux container добавлен `tzdata`.
- Усилена проверка обязательных секретов: пустые и whitespace-only значения отклоняются без раскрытия содержимого.
- Добавлены Python build artifacts в `.gitignore` и регрессионные тесты env-загрузки, URL/timezone, `LOG_LEVEL`, маскирования секретов и безопасного вывода main.
- Успешно выполнены `uv sync`, `uv lock --check`, Ruff lint, Ruff format check, 40 unit-тестов и безопасные import/configuration smoke checks без сетевых подключений.
- Создан единый `Base` на SQLAlchemy `DeclarativeBase`; `Base.metadata` подготовлен для будущих моделей и Alembic, таблицы отсутствуют.
- Создан минимальный persistence API с явным созданием `AsyncEngine` и `async_sessionmaker[AsyncSession]` и асинхронным `dispose()` без глобальных ресурсов и auto-commit.
- Инициализирован async Alembic environment: URL берётся из общей database-only конфигурации, `target_metadata = Base.metadata`, credentials отсутствуют в `alembic.ini`; Discord credentials для миграций не требуются.
- Добавлены unit-тесты persistence/Alembic без PostgreSQL server, включая безопасный offline Alembic configuration check.
- Добавлен pytest marker `integration` и opt-in PostgreSQL smoke-test: без `TEST_DATABASE_URL` он пропускается, а при явном URL создаёт штатные persistence resources, выполняет `SELECT 1` через реальное async-соединение и гарантированно освобождает engine.
- Документированы ручной запуск integration smoke-test и проверка `alembic upgrade head` на той же внешней тестовой PostgreSQL без автоматического развёртывания БД.
- Утверждена MVP-схема persistence из `guilds`, `discord_users`, `guild_members`, `voice_channels`, `voice_sessions` и `voice_intervals`; зафиксированы ID/time types, связи, partial unique indexes, exact-only статистика по умолчанию, restart reconciliation через `confirmed_through_at` и границы первой business migration.
- Реализованы SQLAlchemy 2.x business models `Guild`, `DiscordUser`, `GuildMember`, `VoiceChannel`, `VoiceSession` и `VoiceInterval` на едином `Base` без ORM relationships и runtime-логики.
- Добавлены unit/metadata tests для состава таблиц и колонок, типов и nullability, PK/FK/UNIQUE/CHECK, identity, timezone-aware timestamps, обычных и PostgreSQL partial unique indexes без подключения к БД.
- Alembic environment загружает `Base` через пакет `persistence.models`, поэтому все шесть business-таблиц регистрируются в `target_metadata` без создания engine или соединения.
- Создана первая Alembic business revision `6f3d2a91b7c4` (`create initial persistence schema`) с шестью таблицами, утверждёнными constraints/indexes и полным downgrade в обратном порядке зависимостей.
- Добавлены offline migration tests для Alembic history, порядка upgrade/downgrade, соответствия ORM metadata, identity columns, composite foreign keys и PostgreSQL partial unique indexes без подключения к серверу.
- Успешно проверена генерация `alembic upgrade head --sql` только с `DATABASE_URL`, без Discord credentials; реальное применение upgrade/downgrade к PostgreSQL не выполнялось.
- Добавлены application service и SQLAlchemy repository для транзакционных live voice-переходов: join создаёт logical session и первый exact interval, channel snapshot change меняет interval внутри той же session, leave закрывает interval и session, а повторные состояния обрабатываются идемпотентно.
- Для сериализации обработки одной пары `(guild_id, user_id)` repository блокирует заранее созданную строку `guild_members` через `SELECT ... FOR UPDATE`; provisioning reference data остаётся отдельной ответственностью.
- Старые события явно возвращают `ignored_stale`: для открытой session используется её `confirmed_through_at`, а после закрытия — максимальная подтверждённая граница из истории пользователя. Timezone-aware timestamps нормализуются в UTC, а одинаковый timestamp не считается устаревшим.
- Repository работает с переданным `AsyncSession`, использует `flush()` для получения identity `VoiceSession.id`, не управляет engine lifecycle и не делает `commit()`; transaction scope и commit/rollback принадлежат вызывающей стороне.
- Live voice state machine и PostgreSQL statements покрыты unit/structural-тестами без PostgreSQL, включая join, duplicate, move, leave, stale/equal timestamps, exact-only intervals, сохранение session при move и наличие `FOR UPDATE`.
- Добавлены отдельные service-операции startup/crash reconciliation для текущего connected snapshot и отсутствия пользователя в voice; они используют единый timezone-aware UTC timestamp `R` и сохранённую подтверждённую границу `H = confirmed_through_at`.
- При совпадении полного snapshot `(channel_id, channel_kind, is_afk)` и `R > H` repository закрывает предыдущий exact interval на `H`, сохраняет estimated interval `[H, R]`, открывает exact interval с `R` в той же logical session и durable продвигает `confirmed_through_at` до `R`.
- При отличии snapshot старая session закрывается строго на `H` без estimated gap, а новый snapshot начинает новую exact session с `R`; при отсутствии пользователя старые interval/session также закрываются на `H` без придуманного момента выхода.
- Reconciliation с `R == H` не создаёт нулевой estimated interval, с `R < H` возвращает `ignored_stale` без мутаций, а повторный вызов с тем же `R` не создаёт дополнительные intervals.
- Reconciliation state machine и persistence-записи покрыты unit/structural-тестами без PostgreSQL, включая совпадающий и отличающийся полный snapshot, отсутствие в voice, `R > H`, `R == H`, `R < H`, повторный вызов, exact/estimated quality и сохранение/смену logical session.
- Repository получил минимальный read API для списка владельцев persisted открытых voice sessions конкретного guild; запрос ограничен `ended_at IS NULL` и не меняет schema.
- Добавлен Discord Gateway client с guild/member/voice-state intents и сериализованным `on_ready`: одна startup-операция фиксирует единый timezone-aware UTC `R`, снимает voice/stage snapshots из Gateway cache и вызывает существующие `reconcile_connected`/`reconcile_disconnected` без переноса exact/estimated правил в adapter.
- Connected и disconnected пользователи обрабатываются конкурентно в отдельных caller-owned транзакциях, поэтому одна долгая guild-wide транзакция не удерживается; ошибки отдельных пользователей логируются и учитываются в компактном итоговом summary.
- Повторные `on_ready`/reconnect проходят через ту же идемпотентную service-логику и сериализуются внутри client, не создавая параллельных startup reconciliation operations.
- `main.py` теперь явно создаёт database resources, запускает Discord client и гарантированно освобождает engine после закрытия Gateway; миграции при старте по-прежнему не выполняются.
- Startup wiring покрыт unit-тестами без Discord/PostgreSQL: same/changed connected snapshot, persisted absent user, повторный startup/on_ready, отсутствие открытых sessions, новый connected user, несколько пользователей, единый `R`, intents и lifecycle ресурсов.
- Добавлен отдельный reference-data feature с Discord-независимыми snapshots и application service; `VoiceTrackingService` не получил автоматического создания reference entities и сохраняет прежний precondition provisioning.
- Добавлен SQLAlchemy reference repository с PostgreSQL `ON CONFLICT DO UPDATE` для `guilds`, `discord_users`, `guild_members` и `voice_channels`; repository использует caller-owned session, не делает commit и не содержит destructive cleanup.
- Guild cache adapter синхронизирует всех доступных cached members/users и все voice/stage channels, сохраняет `is_bot`, `joined_at`, имена, `channel_kind` и `is_afk`; nullable `joined_at` из неполного cache не затирает ранее известное значение.
- `on_ready` сериализует полную последовательность `provisioning -> commit -> startup reconciliation`; ошибка provisioning логируется и прекращает startup operation до reconciliation.
- Добавлены unit/structural-тесты пустого provisioning, повторной идемпотентности, обновления snapshot-полей, нескольких members, нескольких voice/stage channels, PostgreSQL upsert SQL, отсутствия скрытого transaction control, порядка orchestration, short-circuit при ошибке и reconciliation двух одновременно подключённых пользователей после provisioning.
- Добавлен live `VoiceStateEventHandler`: он фильтрует bots/другие guild, классифицирует Voice/Stage/AFK, фиксирует один UTC timestamp и вызывает существующие `observe_connected`/`observe_disconnected` без дублирования state machine.
- Для live события выполняется targeted provisioning только текущего guild/user/member и актуального channel в той же caller-owned транзакции, что и voice transition; полный guild cache повторно не синхронизируется.
- Discord client допускает live events к persistence только после успешного startup provisioning/reconciliation и снова закрывает gate при Gateway disconnect.
- Timestamp live event фиксируется до ожидания startup gate. Полностью успешный reconciliation публикует `R` как baseline: события `<= R` не применяются повторно, а события `> R` сохраняют исходный `observed_at`; failed/partial startup не открывает live persistence.
- `on_ready` и `on_resumed` используют один сериализованный recovery method (`provisioning -> reconciliation -> baseline -> gate`); `on_disconnect` закрывает gate, а generation guard не позволяет пересёкшему disconnect recovery опубликовать устаревший baseline.
- Ошибка отдельного live transition откатывает транзакцию, логируется с безопасным guild/user/channel context и не останавливает Gateway handler.
- Live adapter покрыт unit-тестами join, Voice move, leave, Voice↔Stage, AFK, bot/other-guild filters, voice flags no-op, нескольких пользователей, одной транзакции на событие, targeted references, service integration и error isolation.
- Добавлен `VoiceCheckpointRunner`: один cache snapshot/UTC timestamp на цикл, Voice/Stage/AFK classification, исключение bots, per-user transactions и делегирование существующему `observe_connected` без отдельной state machine.
- Обычный checkpoint подтверждает только подключённых cache users; отсутствующих не закрывает. Неизменившийся snapshot продвигает только `confirmed_through_at`, а расхождение и stale observation обрабатываются существующими live semantics.
- `DiscordStatsClient` владеет единственным checkpoint loop: запускает его только после успешного ready/resumed recovery, отменяет и ожидает на disconnect, перед новым recovery и при close, не оставляя orphan tasks.
- Добавлена положительная настройка `VOICE_CHECKPOINT_INTERVAL_SECONDS` с default `60`; безопасный пример и конфигурационный контракт обновлены без изменения schema/Alembic.
- Checkpoint покрыт offline-тестами обновления confirmation без новых session/interval, нескольких пользователей, bots, Voice/Stage/AFK, общего timestamp, stale/live race, connected cache divergence, отсутствующего cache user, error isolation и ready/disconnect/resume/close lifecycle.
- После реализации checkpoint успешно выполнены `uv sync --locked`, `uv lock --check`, 153 offline/unit-теста и Ruff lint/format check; один opt-in PostgreSQL test ожидаемо пропущен без `TEST_DATABASE_URL`, import/config и Alembic history smoke checks прошли с явными фиктивными credentials без сетевых подключений.
- Добавлен feature `voice_statistics` с Discord-независимыми DTO/query/service: четыре периода строятся из одного UTC `as_of`, а локальная граница «Сегодня» вычисляется через `ZoneInfo(REPORT_TIMEZONE)`.
- Добавлен `SqlAlchemyVoiceStatisticsRepository`: один bounded aggregate SQL считает пересечения интервалов, отдельно exact/estimated и не загружает многолетнюю историю в Python; repository использует caller-owned `AsyncSession`, не делает commit и не создаёт engine.
- Открытый interval учитывается только до `min(voice_sessions.confirmed_through_at, as_of)`, закрытый — до `min(ended_at, as_of)`; отрицательные duration исключены.
- `VOICE_MIN_SESSION_SECONDS` реализован в утверждённой query-layer semantics: logical session допускается по суммарному подтверждённому non-AFK exact времени; estimated не проходит threshold, но входит в total допущенной session.
- Guild-only `/stats` имеет optional member-параметр: по умолчанию используется invoking user, bot target отклоняется, ответ всегда ephemeral, а другой guild/DM/bot invoker и query failure обрабатываются до выдачи данных.
- CommandTree синхронизирует `/stats` только для `DISCORD_GUILD_ID` один раз в `setup_hook`; READY/RESUME и checkpoint lifecycle не выполняют REST sync.
- Embed показывает «Сегодня», «7 дней», «30 дней», «Всё время» человекочитаемыми часами/минутами, отмечает наличие восстановленного estimated участка и сообщает о checkpoint freshness.
- После реализации stats layer выполнены `uv sync --locked`, `uv lock --check`, 182 offline/unit-теста, Ruff lint/format, `git diff --check`, import/config smoke и offline Alembic history; один opt-in PostgreSQL test ожидаемо пропущен без `TEST_DATABASE_URL`.
- Добавлены Discord-независимые `VoiceLeaderboard`/`VoiceLeaderboardEntry` и `VoiceStatisticsPeriod`; service валидирует period и строит те же четыре временные границы из одного `as_of`, что `/stats`.
- Stats и leaderboard repository statements используют общие effective/eligible CTE: closed/open cap, AFK exclusion, whole-session exact threshold и exact/estimated overlap определены в одном месте.
- Leaderboard одним PostgreSQL aggregate фильтрует bots через `discord_users`, группирует по user, исключает zero totals, сортирует `total DESC, exact DESC, user_id ASC` и применяет `LIMIT 10`.
- Добавлена публичная `/top` с русскими choices и default `7d`; presentation использует общий duration formatter, medals TOP 3, estimated marker/note, кликабельные cached-member mentions, neutral fallback и `AllowedMentions.none()`.
- `setup_hook` синхронизирует все восемь guild-only команд одним guild sync; READY/RESUME command sync не выполняют.
- Добавлен opt-in PostgreSQL execution test под отдельным `TEST_DATABASE_URL`: transaction-local temporary tables исполняют реальный leaderboard aggregate и откатываются; без URL тест безопасно пропускается.
- После реализации leaderboard выполнены `uv sync --locked`, `uv lock --check`, 209 offline/unit-тестов, Ruff lint/format, compileall, `git diff --check`, import smoke и offline Alembic history; два opt-in PostgreSQL tests ожидаемо пропущены без `TEST_DATABASE_URL`.
- Entrypoint корректно завершает штатный SIGINT с exit code 0 без traceback: Python 3.13 успевает отменить application task и выполнить существующий Discord/DB cleanup, после чего только преобразованный `KeyboardInterrupt` обрабатывается на синхронной границе `asyncio.run`; неожиданные runtime errors и произвольный `CancelledError` не подавляются.
- Lifecycle-тесты подтверждают normal exit, проброс настоящего `RuntimeError` и однократное закрытие Discord client/database resources при cancellation во время `client.start()`.
- После graceful shutdown fix выполнены `uv sync --locked`, `uv lock --check`, 212 offline/unit-тестов, Ruff lint/format, compileall, `git diff --check`, import smoke и offline Alembic history; два opt-in PostgreSQL tests ожидаемо пропущены без `TEST_DATABASE_URL`.
- Добавлены Discord-независимые `VoicePeriodStanding` и `VoiceUserStandings` с валидацией rank/participant count; service передаёт durations и standings один общий `VoiceStatisticsQuery` из одного UTC `as_of`.
- Standings одним PostgreSQL query считают четыре полных non-bot рейтинга через shared effective/eligible/overlap/totals semantics и `ROW_NUMBER` по `total DESC, exact DESC, user_id ASC`; `participant_count` считает ненулевых eligible пользователей, а TOP 10 limit применяется только к `/top`.
- `/stats` остаётся ephemeral и показывает для каждого периода duration плюс `Место: #N из M`; отсутствие места отображается как `Место: — из M`, а пустой рейтинг — как `В рейтинге пока нет`.
- Добавлен opt-in PostgreSQL execution test standings под `TEST_DATABASE_URL` для #1/#11, participant count, обоих tie-break, bot/zero/ineligible exclusion; без отдельной тестовой БД он безопасно пропускается.
- После реализации standings выполнены `uv sync --locked`, `uv lock --check`, 225 offline/unit-тестов, Ruff lint/format, compileall, `git diff --check`, import smoke и offline Alembic history; три opt-in PostgreSQL tests ожидаемо пропущены без `TEST_DATABASE_URL`.
- Все четыре SELECT `/stats` выполняются в одной caller-owned `REPEATABLE READ` transaction: isolation задаётся до первого чтения, поэтому periodic checkpoint не может изменить snapshot между частями отчёта; session закрывает read transaction rollback-ом, commit и глобальные engine/write isolation changes отсутствуют.
- Snapshot race fix покрыт отдельным isolation-order тестом и error-path тестами первого/второго SELECT; после исправления 226 offline/unit-тестов проходят, три opt-in PostgreSQL tests ожидаемо пропущены без `TEST_DATABASE_URL`.
- Подтверждено, что `voice_intervals.channel_id` уже хранит фактический канал каждого атомарного участка; schema/migration не потребовались, а move внутри logical session естественно разделяется по закрытому старому и открытому новому interval.
- Добавлены `VoiceChannelUsageEntry`, `VoiceUserTopChannels` и `VoiceChannelLeaderboard`, read-only user TOP 3 all-time и server TOP 10 period aggregates с общими effective/eligible/overlap semantics и ordering `total DESC, exact DESC, channel_id ASC`.
- `/stats` выполняет третий aggregate TOP 3 в той же `REPEATABLE READ` session/transaction, показывает cache channel names без mentions и fallback `Канал <ID>`; ошибка любого query не возвращает partial embed.
- Добавлена публичная guild-only `/channels` с default 7d, choices today/7d/30d/all, medals, estimated marker/note, deleted-channel fallback и одним общим command sync для трёх команд.
- Opt-in PostgreSQL aggregate coverage расширено проверкой move A→B, server sum across users, open confirmation cap и bot exclusion на TEMP tables; без `TEST_DATABASE_URL` тест безопасно пропускается.
- После channel analytics выполнены `uv sync --locked`, `uv lock --check`, 257 offline/unit-тестов, Ruff lint/format, compileall, `git diff --check`, import smoke и offline Alembic history; три opt-in PostgreSQL tests ожидаемо пропущены без `TEST_DATABASE_URL`.
- Добавлены Discord-независимый `VoiceChannelStatistics`, read-only `get_channel_statistics` и `/channelstats`: required option штатно ограничен Voice/Stage, четыре периода имеют default 7d, успешный ответ и DB error публичны, invalid context/channel отвергается ephemeral до query.
- Один channelstats statement вычисляет whole-session eligibility до selected-channel filter, затем агрегирует period exact/estimated по user, считает channel total оконными суммами до TOP-10 и ранжирует `total DESC, exact DESC, user_id ASC`; отдельный transaction isolation lifecycle не нужен, потому что выполняется один SELECT.
- Offline coverage проверяет move A→B и критический 1-second segment, threshold/estimated/AFK/bot/Stage/open cap, четыре периода и overlap, ranking/ties/TOP/full total, DTO/SQL/one-execute/empty result, Discord UX/validation/cache fallback/mentions и commands=7. Opt-in PostgreSQL TEMP-table test расширен реальным channelstats aggregate с A→B split, multiple users, bot exclusion и open cap.
- После реализации `/channelstats` проходят 287 offline/unit-тестов; три PostgreSQL integration tests ожидаемо пропущены без `TEST_DATABASE_URL`. `uv sync --locked`, lock check, Ruff lint/format, compileall, import smoke, offline Alembic history и `git diff --check` успешны.
- Пользовательская `/leaderboard` заменена на `/top` без изменения service/repository и default `7d`; cached участники выводятся кликабельными `<@user_id>` при полном подавлении уведомлений через `AllowedMentions.none()`, а cache miss сохраняет строку исторического рейтинга с fallback по ID.
- Добавлена guild-only `/help`: статический Kanami embed содержит только существующие команды, отвечает ephemeral и не использует PostgreSQL.
- После этапа `/top` и `/help` полный набор содержит 292 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint и format check проходят.
- Добавлены immutable `VoiceCompanionEntry`/`VoiceUserTopCompanions`, service/repository contract и all-time TOP 3 companion aggregate: два eligible interval CTE соединяются по одному каналу и положительному пересечению, exact сохраняется только для exact+exact, bots/AFK/self исключены, ordering deterministic.
- `/stats` получила optional `user`, target mention и companion field с cache-aware mentions/fallback; ответ остаётся ephemeral с `AllowedMentions.none()`, `/help` описывает просмотр выбранного участника.
- После этапа optional target и companions полный набор содержит 305 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format и `git diff --check` проходят.
- Companion-поле `/stats` сокращено до «Чаще всего вместе», сохранив all-time semantics, cache fallback, estimated marker и безопасные mentions.
- Добавлены `VoicePairStatistics`, single-statement pair/channel/denominator aggregate, `VoiceStatisticsService.get_pair_report()` и отдельный `VoiceTogetherCommandHandler`; `/together` private, guild-only, валидирует обоих members до БД и использует `REPEATABLE READ`.
- После этапа `/together` полный набор содержит 331 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format и `git diff --check` проходят.
- Добавлены `VoiceServerStatistics`, `VoiceStatisticsService.get_server_report()`, single-statement server summary/TOP user/TOP channel aggregate и отдельный private `VoiceServerStatisticsCommandHandler` с четырьмя периодами/default 7d.
- После этапа `/serverstats` полный набор содержит 356 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format и `git diff --check` проходят.
- Добавлены optional `DISCORD_AUDIT_LOG_CHANNEL_ID` и `AUDIT_TRANSIENT_RETENTION_DAYS=90`; Stage 6B.1 теперь трактует channel ID как env baseline, а всегда созданные dynamic adapters при effective disabled не выполняют audit ingestion/delivery/retention действий.
- Добавлена migration `91c4f28a6d3e` и модель `audit_events` с normalized category/event/source data, actor-ready schema, delivery/retry state, transient expiry и индексами history/pending/retention без FK на удаляемые Discord entities.
- Создан feature `audit_logging` с immutable DTO, стабильными event types, явными transient/important retention policies, caller-owned repository и commit-before-delivery ingestion.
- Реализованы member join/left, username/global avatar, nickname/guild avatar/roles/timeout, voice join/left/move, channel и role create/delete/update, ban/unban. Voice flags без channel transition и meaningless updates не создают события; другие guild игнорируются.
- Реализованы persisted-record-only русские embeds, `AllowedMentions.none()`, oldest-first batch delivery, wakeup, retry backoff `5/15/30/60/120/300s`, pending recovery после restart и ежедневный retention cleanup. Delivery честно имеет at-least-once семантику.
- Audit ошибки изолированы от Gateway/commands/checkpoint. В voice callback audit выполняется через безопасный `finally`, но exception critical tracking сохраняет прежнюю propagation semantics и не проглатывается; одновременная audit-ошибка только логируется и не заменяет critical exception.
- Pending delivery ограничен configured `guild_id` на уровне repository SQL и runner contract; partial index использует `(guild_id, next_delivery_attempt_at, occurred_at, id) WHERE delivered_at IS NULL`, поэтому событие другого guild не маршрутизируется в configured channel.
- После audit logging correction pass полный набор содержит 400 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format, offline Alembic history и `git diff --check` проходят.
- Production smoke Audit Logging, channel batching и voice enrichment успешно выполнен; текущий production HEAD — `5ca3e19`.
- Добавлен optional Autorole через `DISCORD_AUTOROLE_ID`: отдельные feature/service и Discord handler выдают одну cached стартовую роль новым non-bot участникам configured guild без БД, migration, retry task или новой slash-команды.
- Autorole проверяет отсутствие роли у member, существование role, `@everyone`/managed flags, permission `Manage Roles` и hierarchy Kanami; Discord `Forbidden`/`HTTPException` логируются и не выходят из callback.
- `member.joined` Audit Logging и Autorole failure-isolated; ручной `member.roles_updated` не создаётся, успешная выдача естественно наблюдается существующим `on_member_update`.
- После Autorole полный набор содержит 421 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format и `git diff --check` проходят.
- Channel update embeds используют channel mentions и семантические заголовки/поля без Guild ID, Channel ID и пустых `—`; чистые position updates одного reorder объединяются в Discord presentation через 1,5-секундный parent-scoped debounce, сохраняя отдельные `audit_events`.
- Batch delivery атомарно помечает все события группы delivered только после успешного Discord send; failure/retry state также обновляется одной транзакцией для всей группы, сохраняя at-least-once semantics.
- После улучшения channel audit UX полный набор содержит 429 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format проходят.
- Voice join/leave/move embeds используют user/channel mentions, локальное время `REPORT_TIMEZONE` и persisted enrichment snapshot: join сохраняет non-bot channel member count, leave — tracker interval duration и statistics-compatible today total, move — duration закрытого interval и непрерывной logical session.
- Voice duration enrichment читается только после commit tracker transition, не вычисляется из audit timestamps и сохраняется в `details_data`; delayed delivery/retry не пересчитывает исторические значения. Старые audit events безопасно отображаются без отсутствующих полей.
- Общий formatter voice duration поддерживает секунды, минуты, часы и дни без лишних нулевых компонентов.
- После улучшения voice audit UX полный набор содержит 440 passed и 4 ожидаемо skipped PostgreSQL integration tests; PostgreSQL integration tests без `TEST_DATABASE_URL` ожидаемо пропущены.
- Устранено двойное время в audit footer: одиночные embeds содержат только `Event ID`, batch — только `Event IDs`, а время всех audit events единообразно передаётся через `discord.Embed.timestamp` для локализации самим Discord.
- После унификации audit timestamp полный набор содержит 459 passed и 4 ожидаемо skipped PostgreSQL integration tests.
- `/stats` преобразована в выбранный period profile с choices today/7d/30d/all и default 7d: total, полный rank, distinct logical session count, average/session, любимый persisted канал, period TOP 3 companions и finite-window trend.
- Profile query layer использует два фиксированных SELECT в одной прежней `REPEATABLE READ` transaction: core aggregate и companions. Оба получают один UTC `as_of`/`VoiceProfileWindow`, переиспользуют effective interval, AFK, open confirmation и whole-session threshold semantics; migration не требуется.
- Today trend сравнивает локальный текущий день с предыдущей локальной датой до того же wall-clock времени; 7d/30d используют соседнее равное rolling-окно, all-time не выводит trend. Session count считает distinct eligible logical sessions с ненулевым overlap, поэтому channel move не увеличивает число сессий.
- После реализации period voice profile полный набор содержит 467 passed и 4 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format и `git diff --check` проходят.
- Добавлены `daily_text_activity`, Alembic revision и Discord-независимый `TextActivityService`: aggregate natural key состоит из guild/member/channel/local date, attachment counter считает вложения, а запись использует атомарный PostgreSQL upsert без message content или per-message строк.
- Добавлен минимальный read API для per-user message totals за inclusive date range и unit/opt-in PostgreSQL coverage service/repository/schema/timezone semantics; Discord runtime и команды намеренно не изменены.
- После text activity foundation полный набор содержит 480 passed и 5 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format, lock check, compileall, offline Alembic head и `git diff --check` проходят.
- `on_message` фильтрует DM, другой guild, bots, webhooks и системные message types; default/reply сообщения и threads используют фактический channel/thread ID, а targeted references и atomic daily upsert коммитятся одной транзакцией без доступа к `message.content`, `message.attachments` или embeds. Live adapter передаёт `attachment_count=0`, потому что `MESSAGE_CONTENT` намеренно выключен; поддержка счётчика остаётся в domain/persistence для возможного будущего режима.
- Добавлена публичная `/topmessages [period]` с календарными today/7d/30d/all по `REPORT_TIMEZONE`, одним non-bot aggregate query, ordering `message_count DESC, user_id ASC`, TOP-10, cache fallback и `AllowedMentions.none()`; `/help` обновлён, command sync содержит 8 команд.
- После подключения text runtime и `/topmessages` полный набор содержит 501 passed и 5 ожидаемо skipped PostgreSQL integration tests; Ruff lint/format, `uv lock --check` и `git diff --check` проходят.
- Production smoke подтвердил live text ingestion, reply counting, `/topmessages`
  и выключенный `MESSAGE_CONTENT`; live attachment collection остаётся
  намеренно выключенным.
- Подготовлены человекочитаемый README и отдельные
  INSTALL/CONFIGURATION/DEVELOPMENT документы, согласованные с восемью
  slash-командами и текущими Settings.
- Добавлены conservative Debian 13 installer/updater и systemd unit: отдельный
  system user/home, защищённый env, локальная PostgreSQL role/database, внешний
  по отношению к Git tree bootstrap/cache `uv`, locked dependency sync,
  отдельные Alembic migrations и запрет dirty/forced Git updates.
- После интеграции repository polish полный набор содержит 502 passed и 5
  ожидаемо skipped PostgreSQL integration tests; Ruff lint/format,
  `uv lock --check` и `git diff --check` проходят.
- Добавлены пять стартовых voice/community achievements, code-defined catalog,
  typed `AchievementMetricSnapshot` с future `MESSAGE_COUNT`, pure deterministic
  evaluator и Discord-независимый unlock service.
- Добавлена `user_achievements` с composite FK на `guild_members`, composite PK,
  timezone-aware first-unlock timestamp и атомарным PostgreSQL
  `ON CONFLICT DO NOTHING RETURNING`; definitions и отображаемые строки в БД не
  дублируются.
- Добавлена линейная Alembic migration после `daily_text_activity`, unit-тесты
  evaluator/service/schema/repository и opt-in PostgreSQL integration test
  идемпотентной выдачи.
- После интеграции Achievements Foundation полный набор содержит 525 passed и 6
  ожидаемо skipped PostgreSQL integration tests; Alembic имеет единственный head
  `4b9c1e7a2d63`, Ruff lint/format, `uv lock --check` и diff checks проходят.
- Добавлена ephemeral guild-only `/achievements [user]`: команда переиспользует
  all-time voice aggregate, вычисляет полные дни из `Member.joined_at`, в одной
  транзакции идемпотентно открывает достижения и показывает известный каталог с
  tier-маркерами и прогрессом; legacy keys безопасно учитываются как архивные.
- `/achievements` зарегистрирована девятой командой runtime и добавлена в
  `/help`; command/embed/runtime поведение покрыто отдельными unit-тестами.
- После интеграции команды полный набор содержит 532 passed и 6 ожидаемо
  skipped PostgreSQL integration tests; Ruff lint/format и `uv lock --check`
  проходят.
- Production smoke-test `/achievements` успешно выполнен на production-host с HEAD
  `b69a282` и Alembic `4b9c1e7a2d63` (head): `kanami.service` после restart
  остался `active/running`, а application command sync завершился с
  `commands=9`; `/help` реально показывает `/achievements`.
- `/achievements` проверена для вызывающего пользователя и явно выбранного
  участника. Embed корректно показывает пользователя, число открытых
  достижений, открытые и закрытые достижения, текущий прогресс, tier-маркеры и
  аватар.
- PostgreSQL `user_achievements` получил по три unlock-записи для каждого из
  двух протестированных пользователей (`voice_10_hours`, `voice_50_hours`,
  `server_age_30_days`), всего 6 строк. `voice_100_hours` и
  `server_age_365_days` корректно остались закрытыми. После smoke-test в journal
  отсутствуют `ERROR`, `Exception`, `Traceback` и `Achievement query failed`;
  startup voice reconciliation завершился с `connected=5`, `unchanged=5`,
  `failed=0`.
- Добавлен GitHub Actions workflow `CI` для push и pull request в `main`: один
  validation job на Python 3.13 устанавливает locked dependencies, запускает
  полный pytest, Ruff, lockfile и diff checks. PostgreSQL integration tests
  получают только `TEST_DATABASE_URL` временного disposable PostgreSQL 17;
  production secrets и deployment отсутствуют. Первый реальный run в Pull
  Request #1 успешно завершил job `checks`: полный pytest выполнил 538 тестов без
  skipped PostgreSQL integration tests (`538 passed`, 27 warnings), Ruff
  lint/format, `uv lock --check` и `git diff --check` прошли.

- Реализован WUI-2 Dashboard & Operations поверх общего WUI-1 presentation
  foundation. `/admin/` получил компактную server summary с независимыми
  статусами Kanami/PostgreSQL/Bot Control, выраженную сетку пяти KPI и
  вторичные role-aware quick actions. `/admin/system` сначала показывает
  overall health и четыре core component cards, затем компактные production
  metadata, sampling-aware availability 24h/7d, operational incident list и
  Data Integrity.
- Availability presentation разделяет основные Healthy/Degraded/Unavailable/
  Coverage/Missing/Not monitored/incident metrics и вторичные diagnostic
  details. Partial window явно объясняет поздний старт мониторинга, missing
  observations либо оба фактора; Missing и Not monitored не изображаются как
  uptime. Disabled Game Tracking остаётся neutral/inactive. Backend health,
  coverage, incident и integrity semantics, queries, permissions и schema не
  изменялись.
- WUI-2 развёрнут в production; desktop и основной mobile content/layout прошли
  browser smoke. Follow-up WUI-2.1 заменяет длинную always-expanded navigation
  на телефонах компактным native `<details>/<summary>` menu без JavaScript.
  Desktop sticky sidebar сохраняется, а полный role-aware navigation и
  CSRF-protected POST logout доступны внутри раскрываемого mobile menu. WUI-2.1
  также развёрнут в production и прошёл реальный mobile browser smoke.
- Второй узкий responsive hotfix навигации `fix/web-nav-900-compact-menu`
  объединён с `main` в commit `c7781e0` и развёрнут в production: breakpoint
  compact `<details>` menu синхронизирован с tablet shell на `<=900px`. Desktop
  navigation сохраняется на `>900px`, раскрытая navigation использует 3 колонки
  в диапазоне 641–900px, 2 колонки на `<=640px` и 1 колонку на `<=430px`.
  Narrow-only content/layout rules остались в исходных media queries; предыдущий
  full-width `member-profile-action` на `<=900px` сохранён. Hotfix не требовал
  migration и не менял JavaScript, schema, dependencies или runtime bot.
- WUI-3 добавляет responsive presentation для Members, Administrators и Audit:
  существующие semantic desktop tables сохраняются, а на `<=640px` заменяются
  компактными member/admin cards и вертикальным audit event list. Search,
  pagination, порядок, значения, OWNER-only visibility и POST/CSRF actions
  используют прежние контракты; backend/security semantics не менялись.
- Локально реализован WUI-4A Members & Profiles v2 без schema, migration,
  JavaScript или Discord API. `/admin/members` стал единым responsive directory
  с summary, search/sort toolbar, CSS monograms, отдельным `@username`, lifetime
  stats и русской pagination. Allowlisted global sorting поддерживает name,
  joined, Voice, Messages и Achievements в обоих направлениях; aggregates
  вычисляются set-based для всего filtered current non-bot scope до
  `LIMIT/OFFSET`, а `user_id ASC` сохраняет deterministic tie-break. Member-only
  compact layout включается при 1100px до общего mobile navigation breakpoint;
  Unicode monogram после uppercasing жёстко ограничен двумя code points.
- Web member profile получил monogram hero со статусом «На сервере»/«Покинул
  сервер», block-level identity и membership, трёхколоночные lifetime KPI,
  achievement cards и vertical lifecycle timeline с русскими labels. Existing
  two-query detail contract, lifetime Voice, achievement/lifecycle semantics,
  OWNER/ADMIN access, escaping и CSP `script-src 'none'` сохранены. Member
  Analytics 7d/30d не входила в WUI-4A и впоследствии реализована в WUI-4B.
- `WUI-4A.1 Member Avatars` ранее объединён с `main` и прошёл production smoke.
  Alembic revision `d4e8a1c7b962` добавляет nullable
  `discord_users.avatar_hash` и `guild_members.guild_avatar_hash` без backfill.
  Startup, targeted message/voice provisioning и существующие member/user identity
  events поддерживают эти source facts; remove сохраняет последний полный avatar
  snapshot до `left_at`. Web Admin читает hashes в прежних set-based statements и
  отображает guild avatar → global avatar → monogram только через allowlisted
  Discord CDN URL. Image bytes/history, live Discord API, JavaScript и CSP changes
  не добавлены. Production остаётся на Alembic `d4e8a1c7b962 (head)`; реальные
  Discord avatars и CSS monogram fallback подтверждены authenticated browser smoke.
- WUI-4B Member Analytics завершён: сначала с `main` объединён WUI-4B.1
  backend/domain foundation, затем WUI-4B.2 Web UI. Финальный production commit —
  `4c951ea654799b7a3dc26f97425a6f868e61c77d` (`4c951ea`),
  `feat(web): add member activity analytics`. Между Membership и Lifetime
  statistics в профиле участника добавлен раздел Activity с периодами 7d/30d по
  завершённым локальным календарным дням без сегодня и сравнением с
  непосредственно предшествующим периодом той же длины. KPI: Voice, Messages и
  Active days; daily history zero-filled ровно для 7 или 30 дат текущего периода.
  Active day означает положительный eligible Voice или хотя бы одно сохранённое
  сообщение; Voice сохраняет exact/estimated semantics.
- Coverage для Member Analytics member-specific и source-aware отдельно для
  Voice/Text; неизвестная полнота не представляется как partial history. SQL
  ограничен настроенными `guild_id + user_id` и не загружает server-wide activity
  для последующей фильтрации в Python. Успешная загрузка использует ровно три
  bounded read: Voice для объединённого previous/current range, Text для
  объединённого previous/current local-date range и member-specific earliest
  Voice/Text coverage. Web wrapper выполняет их в одной короткоживущей
  `REPEATABLE READ, READ ONLY` transaction. Ошибка аналитики деградирует только
  Activity; в остальном валидный профиль остаётся HTTP 200.
- WUI-4B не менял Discord `/profile`, member directory/sorting, schema, models,
  dependencies, JavaScript или CSP и не требовал migration. PR CI и main CI с
  PostgreSQL 17 / `TEST_DATABASE_URL` прошли; финальный full suite — 1334 passed,
  39 существующих warnings и 0 skipped. Ruff lint/format, uv lock check и
  `git diff --check` прошли.
- Production rollout WUI-4B обновил `/opt/kanami` fast-forward с `8dc0bb9` до
  `4c951ea`. Production Alembic остался `d4e8a1c7b962 (head)`. Перезапущен только
  `kanami-web-admin.service`; `kanami.service` не перезапускался, и его PID не
  изменился. Web Admin успешно перезапустился, оба service остались active.
  Startup journal не содержал traceback/application errors; наблюдался только
  ожидаемый warning о private non-loopback bind. Реальный OAuth login прошёл.
  Production HTTP smoke подтвердил `GET /admin/health` → HTTP 200 со
  `status=healthy`.
- Authenticated production browser smoke WUI-4B прошёл: member directory остался
  работоспособен, Activity отобразился для 7d и 30d. Для обоих периодов
  подтверждено ожидаемое число завершённых локальных дней; текущий день в local
  context корректно исключён.
  Отобразились Voice, Messages, Active days, previous-period comparison,
  estimated Voice note, daily history и coverage caveats. Визуально подтверждены
  source-aware unknown/partial distinction и `NO_BASELINE`; Lifetime statistics,
  achievements и остальной lifecycle/member profile content сохранились.
- Responsive production browser smoke WUI-4B с 30d как heavy case прошёл на всех
  проверенных ширинах без наблюдаемого global horizontal overflow: 1024px —
  desktop sidebar и три KPI; 900px — compact top navigation в закрытом и
  раскрытом состояниях с тремя колонками меню; 640px — одноколоночные profile
  sections, mobile Activity и daily history cards; 430px и 320px — usable narrow
  layout, включая period buttons и top navigation. После browser smoke журнал не
  содержал `ERROR`, `Exception`, `Traceback`, DB errors или Member Analytics
  failures; присутствовали успешные OAuth/authorization entries.
- Реализован Server Analytics A1 Domain & Read Foundation без schema и
  migrations. Typed report использует 7/30
  завершённых local calendar days, current/previous KPI, zero-filled daily
  Voice/Text series, top-5 rankings, существующую Voice eligibility и `/activity`
  semantics, а coverage независимо оценивает current/previous относительно
  earliest recorded dates без ложного утверждения о полном monitoring period.
- Реализован Server Analytics v1 (A1 + A2.1 + A2.2): OWNER/ADMIN
  `GET /admin/analytics` с фиксированным 7d/30d, одним `as_of`, одной
  database-enforced `REPEATABLE READ, READ ONLY` transaction и одним A1 report;
  Top Voice/Text display names разрешаются одним persisted batch lookup в том
  же snapshot. Финальная responsive presentation содержит пять KPI, отдельные
  daily Voice/Messages charts, semantic 8×7 Voice heatmap, activity summary,
  Top-5 и empty states. Она сохраняет typed `NO_BASELINE`, estimated Voice,
  source-aware current/previous caveats, CSP `script-src 'none'` и escaping.
- Server Analytics v1 production deployment выполнен 29 августа 2026 года:
  checkout `/opt/kanami` обновлён fast-forward с `8f5ed75` до `8b02fcc`. Новых
  migrations в feature нет, поэтому migrations не выполнялись.
  `kanami-web-admin.service` успешно перезапущен и остался active/running;
  основной `kanami.service` во время deploy не перезапускался и оставался
  active. Startup прошёл без traceback или application errors. Для deployment
  через reverse proxy сохранился ожидаемый warning о private non-loopback bind.
- Production HTTP smoke подтвердил `GET /admin/health` → `200 healthy`, а
  unauthenticated `GET /admin/analytics` и
  `GET /admin/analytics?period=30d` → `303 /admin/login`. Security headers
  сохранились, CSP по-прежнему содержит `script-src 'none'`.
- Authenticated production browser smoke под OWNER прошёл для desktop 7d,
  desktop 30d, 640px/30d и 430px/30d. Для 7d подтверждены корректные границы
  current period, earliest recorded Voice и Messages activity, а также
  согласованные ненулевые показатели active members, messages, unique Voice
  users и unique message authors. Voice value отображался человекочитаемо. Text
  previous-baseline partial warning появился у Messages,
  Unique message authors и Active members, но не у Voice-dependent KPI. Daily
  Voice/Messages charts, Voice heatmap/activity summary и оба Top-рейтинга
  отобразились.
- Для production 30d подтверждены корректные границы current period и
  согласованные ненулевые показатели active members, messages, unique Voice
  users и unique message authors.
  Voice и Text показали current-period и previous-baseline partial-history
  warnings, а зависимые KPI получили предупреждение соответствующего source.
  `NO_BASELINE` отображался как «Нет базы для сравнения» без synthetic
  infinity/+100% fallback. Отобразились 30 daily Voice/Text points; ранняя часть
  истории без доступных данных представлена zero bars, а возможная неполнота
  явно обозначена coverage warning. Heatmap/activity summary и рейтинги также
  отобразились.
- Responsive production browser smoke подтвердил на 640px mobile navigation,
  двухколоночную KPI grid, перенос coverage/warnings, stacked rankings и
  локальный controlled overflow charts без наблюдаемого global horizontal
  overflow. На 430px KPI grid стала одноколоночной, period selector остался
  usable, coverage warnings поместились, heatmap/rankings/methodology не вышли
  за layout, charts сохранили local overflow; global horizontal overflow не
  наблюдался.
- После browser smoke оба service остались active, production checkout — clean
  на `8b02fcc`. В проверенном journal не наблюдались traceback, DB errors или
  application exceptions; присутствовали успешные OAuth/authorization entries.

## Что сейчас делается

D2.9 готовится как отдельный checkpoint trusted `kanami update`: Manager
проверяет bootstrap trust до запуска и делегирует неизменённый workflow
canonical updater-у. Legacy ownership migration, rollback, backup/restore и
остальные lifecycle функции в текущий scope не входят. До восстановления D2.8
trust boundary старый service-user-writable checkout-local `update.sh` по-прежнему
не должен запускаться через `sudo`.

WUI-4A.1 и оба responsive hotfix развёрнуты в production. Первый hotfix исправил
расположение compact-кнопки «Профиль» справа в member row на tablet width. Второй
hotfix `fix/web-nav-900-compact-menu` объединён с `main` в commit `c7781e0`, и
production обновлён до `c7781e0`; он не требовал migration, Alembic остаётся
`d4e8a1c7b962 (head)`. При втором deploy перезапускался только Web Admin, bot не
перезапускался. После deploy `/admin/health` вернул HTTP 200 со status `healthy`;
CSP остался без изменений, включая `script-src 'none'`.

Authenticated production browser smoke WUI-4A.1 прошёл: на 1024px сохранился
desktop sidebar, а compact-кнопка «Профиль» корректно расположена справа в member
row; на 900px compact top navigation проверена в закрытом и раскрытом состояниях,
раскрытая navigation использует 3 колонки; на 640px compact navigation и
responsive layout корректны; на 430px и 320px narrow layout не показал
horizontal overflow. Реальные Discord avatars отображаются, CSS monogram fallback
также визуально подтверждён. Post-browser journal Web Admin чист: наблюдались
только ожидаемый warning о private non-loopback bind и успешные
OAuth/authorization события. Локальные проверки hotfix: focused Web Admin suite —
66 passed, 3 skipped без `TEST_DATABASE_URL`; полный hermetic suite — 1288 passed,
15 skipped и 39 существующих dependency warnings; Ruff lint/format и
`git diff --check` проходят.

Server Analytics v1 объединён с `main`, развёрнут на production commit `8b02fcc`
и прошёл authenticated production browser smoke для 7d/30d на desktop и mobile
breakpoints 640px/430px. Отдельный, выполненный ранее read-only backend A1 smoke
против production PostgreSQL подтвердил реальные Voice/Text/estimated данные и
необходимость coverage warnings: report занимал примерно 0,15 с для 7d и 0,23 с
для 30d. Этот A1 data-path smoke остаётся отдельной проверкой и не подменяет
финальный Web browser smoke.

WUI-4A Members & Profiles v2 объединён и развёрнут в production commit `84937b9`.
Authenticated production browser smoke прошёл для Members/Profile на desktop и
responsive widths 1024, 640, 430 и 320 px: directory участников, sorting, профиль
текущего участника и departed member/lifecycle profile отобразились корректно, без
наблюдаемого global horizontal overflow. После rollout `/admin/health` вернул
HTTP 200. Основной `kanami.service` не перезапускался; был перезапущен только
`kanami-web-admin.service`. После browser smoke журнал Web Admin не содержал
`ERROR`, `Exception` или `Traceback`.

`WUI-4A.1 Member Avatars` ранее объединён с `main`, развёрнут и получил production
smoke PASS на commit `c7781e0`; production Alembic — `d4e8a1c7b962 (head)`.
WUI-4B Member Analytics 7d/30d завершён, объединён с `main`, развёрнут на
production commit `4c951ea` и прошёл authenticated production browser smoke для
обоих периодов и responsive widths 1024, 900, 640, 430 и 320px. Production
Alembic остался `d4e8a1c7b962 (head)`; WUI-4B не требовал migration, а rollout
перезапустил только `kanami-web-admin.service`.

WUI-3 Responsive Members / Administrators / Audit реализован локально в
feature-ветке `feature/web-ui-responsive-tables`. WUI-2/WUI-2.1 уже
production-smoke-verified; WUI-3 развёрнут в production и прошёл реальный mobile browser smoke для members, owner-блока administrators и audit; managed admin revoke в production не упражнялся, но покрыт regression tests. Изменения этапа ограничены server-rendered presentation и
regression tests без новых queries, routes, dependencies или migrations.

Rules Publication v1 исторически production-smoke-verified на commit `9e149eb` с
Alembic head `e1a7c4d92b60`. Rules Compliance R3A теперь также развёрнут и
production-smoke-verified на текущем production commit `560703e` с Alembic head
`a4f6c8d21e73`; подробности вынесены в отдельный smoke-отчёт. Во время R3A smoke
managed Discord publication была отключена, а compliance корректно вычислялась
из persisted Rules/acceptance history.

Единственный намеренно не выполненный вручную production smoke scenario — Publish
новой реальной Rules version с успешным DB commit и последующим automatic Bot
Control sync, который обновляет существующее managed message без изменения его
ID. Фиктивная version 1.1 только ради smoke не создавалась; сценарий будет
проверен на будущем этапе Rules Compliance / reacceptance при появлении реальной
новой версии правил. Сам механизм реализован и покрыт автоматическими тестами.

Унификация reverse-proxy routing templates и regression test реализованы в
commit `61252ff` (`fix(web): unify public proxy routing`) и находятся в `main`.
Production Nginx routing приведён к тому же контракту и прошёл HTTPS smoke: `/`
перенаправляет на `/admin/`, Web Admin работает, произвольный неизвестный path и
`/control` возвращают `404`; Bot Control наружу не публикуется.

Rules v1 и Web Admin Rules v1 уже развёрнуты и функционально проверены на
production. Reminders, grace workers, enforcement, new-member onboarding,
diff/rollback/scheduling и forced reacceptance остаются отдельными будущими
этапами.

Persistence, voice runtime/recovery/checkpoint, общий voice statistics query
layer, Text Activity, шестнадцать slash-команд, Audit Logging, Member Profile v1 и optional Autorole
реализованы. Production smoke для Text Activity и `/achievements` выполнен.
Message-based achievements, achievement notifications и автоматическая/фоновая выдача вне
явного вызова команды пока не реализованы. GitHub Actions CI реализован и прошёл
первый remote run в Pull Request #1. Для
объединённого состояния репозитория остаётся проверить install/update flow
end-to-end на чистой Debian 13 VM. Achievement evaluator остаётся независимым от
discord.py, voice repository и Text Activity repository. Автоматические тесты не
используют production credentials.

Stage 3A Web Admin member identity, Stage 3B read-only detail, Stage 3C Discord
OAuth2 authentication, Stage 3D authorization и Stage 4 bot-profile actions реализованы локально. WUI-4A.1 расширяет schema/reference provisioning nullable global/guild avatar hashes; runtime обновляет всю persisted identity на full startup, targeted message/voice provisioning и релевантных member/user событиях, а `/admin/members` использует безопасный display fallback и поиск по имени. Detail показывает configured-guild membership, last-known avatar, persisted achievements и bounded lifecycle history. Authentication
подтверждает Discord user ID, после чего authorization назначает env ID роль
OWNER либо active grant роль ADMIN и требует current non-bot guild membership до
создания session. OWNER имеет приоритет; публикация поддерживается только через документированный HTTPS
reverse proxy с loopback или explicitly opted-in private bind.
Avatar history остаётся вне этих этапов; Stage 4 отдельно управляет только guild
avatar самого бота через его live Discord member. WUI-4A.1 migration применена в
production, Alembic находится на `d4e8a1c7b962 (head)`, authenticated production
browser smoke пройден.

Web Admin Dashboard v1 реализован локально на `feature/web-dashboard-v1` без
schema/migration и без расширения Bot Control. Стартовая страница использует
существующие OAuth/session и OWNER/ADMIN boundaries, PostgreSQL read model и
existing Voice statistics semantics; production smoke и deployment этого этапа
ещё не выполнялись.

Production verification от 31.08.2026 подтвердила, что Game Tracking включён,
реальные game sessions сохраняются, открытые sessions получают свежие
checkpoint и start/switch/stop переходы записываются корректно. В проверенном
journal не обнаружено Game Tracking errors; Alembic находится на
`d4e8a1c7b962 (head)`. G3A развёрнут отдельно как Web-only изменение и прошёл
authenticated production browser/server/responsive smoke 31.08.2026.

Stage 5 deployment/security реализован без изменений Discord feature set и schema.
Поддержаны standalone Caddy, existing same-host proxy и remote central proxy.
Production TLS reverse proxy настроен оператором: публичный routing contract
исправлен вручную и подтверждён HTTPS smoke. Repository-side Nginx/Caddy examples
теперь унифицируют это production-поведение и готовятся к commit/review.

Stage 6A managed-administrator persistence, atomic audit, bot-side loopback
grant/revoke wiring и OWNER-only management/audit UI объединены с `main` и
развёрнуты на production-host с revision `722e420`; migration `8d44cacc791e` применена
как Alembic head. GitHub Actions был зелёным. Production smoke подтвердил два
configured permanent OWNER accounts, managed ADMIN grant и OAuth login,
отсутствие OWNER-only navigation у ADMIN, revoke, fresh deny при повторном login
и два grant/revoke audit events newest-first. Финальный active grant
отсутствовал; сервисы оставались активны без
ERROR/CRITICAL/Exception/Traceback. Подробный результат и rollback checkpoint
зафиксированы в `docs/WEB_ADMIN_STAGE6A_PRODUCTION_SMOKE.md`.

Stage 6B.1 server-settings backend объединён с `main` и production-validated на
production-host с revision `20dbd6a`; migration `3e7b9c2a6f41`, env baseline
compatibility и Bot Control mode=`env` no-op подтверждены без создания DB/audit
данных. Stage 6B.2 Server Settings UI, OWNER/ADMIN fresh authorization, CSRF,
PRG mutations и bounded runtime options source production-validated на
revision `a585149`. Standalone Web Admin получил те же четыре ENV baseline для
корректного read-side display; bot env напрямую не читается. Последний
подтверждённый production state сохранял active DB autorole override. Stage 6B.3
dropdown active-selection и persisted server-setting audit presentation
production-validated на revision `599584a`. Browser read smoke не изменил одну
settings row и пять setting-change events; только Web Admin был перезапущен, а
active DB autorole override остался активным.

Административная guild-only `/health` реализована как ephemeral read-only
диагностика для участников с `manage_guild`. Она показывает Gateway/latency,
изолированный PostgreSQL `SELECT 1`, process uptime, локальное число команд,
command sync, guild channel/member counts и существующий voice startup state.
Production smoke-test успешно выполнен на production-host с HEAD `10e65fb` и Alembic
`4b9c1e7a2d63` (head): deployment оставил `kanami.service` active/running,
application command sync завершился с `commands=10`, а участник с Manage Server
получил корректный ephemeral embed с Gateway/PostgreSQL latency, uptime, десятью
локальными guild-командами, успешным command sync, member/Voice/Stage counts и
готовым Voice tracking. После вызова в journal отсутствовали `PostgreSQL health
probe failed`, `ERROR`, `Exception` и `Traceback`; service остался active.
Команда остаётся read-only: Audit queue и Autorole status в MVP не входят, HTTP
Prometheus и полноценный monitoring не реализованы. Deployment
automation, settings/env, migrations и intents для `/health` не добавлялись.

## Известные проблемы

- Live text ingestion, reply counting и `/topmessages` прошли production smoke;
  полный install/update flow объединённого состояния ещё не проверен на чистой
  Debian 13 VM. Автоматические тесты не подключаются к production
  Discord/PostgreSQL.
- Не определены pagination, дедупликация/коррекция message events и правила opt-out/удаления данных.
- Audit actor attribution, kick detection, `/history`, category routing и message logging отложены; редкий delivery duplicate возможен в crash-gap между Discord send и DB mark.
- Автоматические годовщины имеют ту же честную at-least-once границу: unique outbox key исключает повторную постановку и обычные restart/reconnect duplicates, но авария после Discord send и до commit `delivered_at` теоретически может повторить сообщение.
- Для Autorole production требуется `Manage Roles`, а highest role Kanami должна находиться выше configured autorole; production smoke Autorole ещё не выполнен.
- Локально тестовая БД и `TEST_DATABASE_URL` по-прежнему предоставляются
  вручную; первый remote GitHub Actions run подтвердил полный suite с disposable
  PostgreSQL 17 без skipped integration tests. Online Alembic migration
  smoke-test в workflow по-прежнему не входит; существующие 27 warnings не
  исправлялись.
- Автоматическая backup/restore policy и полноценный production health monitoring пока не реализованы; operator обязан отдельно защищать PostgreSQL data.
- Текущее состояние Operations использует факты Web Admin/Bot Control, а W1.3
  history — отдельные bot-owned sampled observations. Gateway latency не
  симулируется; потерянные при недоступной PostgreSQL samples не считаются
  healthy или unavailable автоматически. Alerting и внешний polling не входят.
- Installer и updater прошли static syntax review, но ещё не прошли end-to-end smoke на чистой Debian 13 VM; partial failure installer требует ручной диагностики и не затирается повторным запуском.
- Message-based achievements, notifications и автоматическая/фоновая выдача
  пока отсутствуют; достижения вычисляются и выдаются только при явном вызове
  `/achievements`.
- Profile v1 не выдаёт роли и не вычисляет XP/прогресс автоматической иерархии
  `Гость → Посвящённый → Страж`; эти механизмы остаются отдельным будущим этапом.
- Web Admin использует только внутренние роли OWNER/ADMIN и пока не проверяет
  Discord roles. Membership берётся из локальной PostgreSQL snapshot: при
  недоступности БД или ещё не provisioned/stale membership доступ fail-closed.
- Management UI не предоставляет Discord API search, invite flow, историю
  revoked grants или дополнительные роли; это сознательно вне текущего этапа.
- Stage 4 production wiring не автоматизирован: installer пока не создаёт
  отдельный Web Admin unit/env и не выдаёт процессам раздельные secrets. До
  ручного разделения Web Admin нельзя давать общий bot env, потому что он содержит
  `DISCORD_TOKEN`; control listener нельзя выводить за `127.0.0.1`.
- Sessions, OAuth transactions и write limiter process-local: restart отзывает
  sessions и очищает limiter; multi-worker deployment без общего session store не
  поддерживается. Reverse proxy network limiting документирован для Nginx, но
  vanilla Caddy требует external edge/firewall для login/callback rate limiting.

## Важные принятые решения

- G3A не создаёт второй алгоритм игровой статистики: Web Admin read-side
  переиспользует `GameStatisticsService`, его timezone/canonicalization semantics
  и существующий `SqlAlchemyGameTrackingRepository`. Параметр `game_period`
  отделён от Activity `period`; live Presence/Gateway state, filtering/catalog,
  server-wide и cross-member analytics в G3A не входят.
- Официальная публичная поверхность Web Admin состоит только из redirect
  `/ -> /admin/` и proxy namespace `/admin/*`; все остальные paths завершаются
  proxy-side `404`, а loopback Bot Control никогда не публикуется.
- Rules content и точные acceptances хранятся только в PostgreSQL. На guild
  может быть ровно один current `published` ruleset; опубликованная версия
  immutable для service API, а новая редакция создаётся новой строкой.
  Discord-роль не является доказательством принятия. Persistent callback всегда
  заново разрешает current version и не доверяет версии старого сообщения.
- Managed Rules publication хранит только channel/message/reflected-ruleset
  cursor в существующей guild settings row. Discord mutation принадлежит bot
  process; Web Admin не получает Bot token; sync идемпотентен и восстанавливает
  удалённое message без создания параллельной Rules/acceptance системы.
- Channel configuration, disable и manual sync инициируются на `/admin/rules`,
  но Discord cleanup/validation/mutation принадлежат bot process. Publish и
  Discord sync являются двумя последовательными операциями: committed Rules
  version никогда не откатывается из-за presentation failure.
- Web Admin DB connection допускает scoped транзакционные writes только для
  Rules management. Остальные query services остаются SELECT-only, а Bot
  profile, administrators и server settings продолжают изменяться через узкий
  authenticated Bot Control API.
- Проект разрабатывается на Python и предназначен для self-hosted-развёртывания.
- Реальные секреты и локальные env-файлы не должны попадать в Git.
- Состояние проекта поддерживается в этом документе, а долгосрочные архитектурные решения — в `docs/ARCHITECTURE.md`.
- Целевой MVP сочетает voice-статистику, суточную текстовую активность без
  содержимого, member tracking и slash-команды; live text collection реализован.
- Voice учитывается логическими сессиями и канальными сегментами; AFK и Stage отделены, боты исключены.
- Перезапуски обрабатываются через reconciliation с явным разделением exact и estimated данных.
- Per-message metadata не сохраняются; voice-сессии и дневные текстовые
  агрегаты хранятся постоянно, а применимые retention-политики должны оставаться
  настраиваемыми.
- Для будущих raw per-message metadata и отдельных server events зарезервированы
  defaults 90/365 дней; соответствующие collectors и таблицы отсутствуют, а
  `RAW_MESSAGE_RETENTION_DAYS` не применяется к `daily_text_activity`. Audit
  retention реализован и настраивается отдельно.
- Основная БД — PostgreSQL с миграциями; timestamps хранятся в UTC.
- Operational history записывает только основной bot process с минутной
  частотой и восьмидневным bounded retention; Web Admin её только читает.
  24h/7d различают nominal, monitored и covered slots: pre-history показывается
  как Not monitored, а gaps внутри monitored period — отдельной долей Missing и
  не считаются Healthy либо конкретным outage.
- Первая версия обслуживает один guild, но данные и бизнес-логика используют `guild_id`.
- `GUILD_MEMBERS` используется всегда; `GUILD_PRESENCES` запрашивается только
  optional Game Tracking. `MESSAGE_CONTENT`, typing и DM tracking не
  используются.
- Runtime и управление проектом: Python 3.13, asyncio, uv, `pyproject.toml` и версионируемый `uv.lock`.
- Discord-интеграция: discord.py и application/slash commands.
- Persistence: PostgreSQL, async SQLAlchemy, asyncpg и обязательные Alembic-миграции.
- Приложение — feature-first modular monolith с тонкими Discord handlers и переиспользуемыми application/services.
- Первый документированный production target — Debian 13, systemd и локальный PostgreSQL; Docker/container flow пока не поддерживается официальной инструкцией. Web Admin остаётся отдельным process со scoped Rules DB writes, Discord OAuth2 authentication, OWNER/ADMIN + current membership authorization и узкими bot-profile actions через bot-owned loopback control interface. Public deployment использует HTTPS reverse proxy; bot control остаётся loopback-only.
- Discord OAuth identity и Web Admin authorization разделены: env IDs являются постоянными OWNER, active DB grants добавляют ADMIN, а session создаётся только после bounded current non-bot configured-guild membership SELECT. Пустые оба источника означают deny-all.
- Web Admin никогда не получает Discord Bot token. Bot-profile writes принадлежат
  Discord process и доступны Web Admin только как фиксированные authenticated
  loopback operations; actor берётся из server-side WebSession, POST защищены
  CSRF, а binary avatar/shared secrets не логируются.
- Fresh write authorization выполняется только после session/CSRF validation и
  непосредственно перед control API; denial или DB failure отзывает session и
  не вызывает Discord process. Этот порядок не создаёт CSRF authorization oracle.
- Managed-administrator writes принадлежат только основному bot process: Web
  Admin инициирует фиксированные loopback grant/revoke operations без `guild_id`,
  а изменение grant и important audit event фиксируются одной транзакцией.
- Uvicorn не доверяет forwarded headers. Public URL, OAuth redirect и Secure cookie
  являются explicit config, HSTS принадлежит TLS reverse proxy. Private bind
  разрешён только для конкретного internal IP с отдельным opt-in и firewall.
- Тестирование строится на pytest с разделением unit- и DB integration-тестов.
- Конфигурация загружается pydantic-settings из environment variables и валидируется при запуске; локально допускается `.env`.
- Конфигурационный контракт ограничен утверждёнными Discord, PostgreSQL, timezone, retention, voice threshold и logging-переменными.
- Стандартный Python `logging` пишет в stdout/stderr и не раскрывает секреты.
- Ruff используется как linter и formatter; async-тесты используют pytest-asyncio и временный PostgreSQL для DB integration tests.
- Production пути: root-owned `/opt/kanami` для checkout и `.git`,
  `/etc/kanami/kanami.env` для secrets/config; service работает как system user
  `kanami`. Tracked source не writable для service account; ignored
  `/opt/kanami/.venv`, `/var/lib/kanami` и `/var/cache/kanami/uv` принадлежат
  `kanami` и остаются его минимальными writable paths.
- Kanami Manager развивается как отдельный Bash entrypoint, пригодный для
  установки в `/usr/local/bin/kanami`. Read-only команды
  status/doctor/version/help/logs не требуют root со стороны Manager; D2.5
  добавляет только root-only restart основного `kanami.service` через
  фиксированный `/usr/bin/systemctl`. D2.6 читает только его raw journal через
  фиксированный `/usr/bin/journalctl`, отключает pager и валидирует bounded
  `--lines`; фактический доступ остаётся под system journal permissions. Manager
  D2.7 добавляет root-only start/stop основного bot с точными post-state checks,
  не затрагивая Web Admin или boot policy. Manager не объявляет release version
  до появления version lifecycle.
  Doctor считает основной bot deployment обязательным, а отдельный Web Admin —
  optional; hermetic tests подменяют read-only manager paths и `PATH`. Installer
  создаёт regular root-owned copy, а updater после успешного pull refresh-ит её
  из канонического installed checkout до dependency/migration/restart stages.
- D2.8 закрепляет privilege boundary между runtime и deployment: root позднее
  выполняет или устанавливает только source из root-owned checkout. Updater
  проверяет root ownership/non-writability source tree и отдельную
  `kanami:kanami` `.venv`, выполняет Git от root и не делает recursive ownership
  repair. Проверка предназначена для trusted canonical updater и не делает
  legacy checkout-local `update.sh`, writable пользователем `kanami`, безопасным
  для запуска через `sudo`; такая installation требует manual trust restoration
  из trusted source.
- D2.9 разделяет bootstrap и full invariant validation. Manager до execution
  проверяет только root-owned/non-writable non-symlink chain до updater и
  запускает его fixed `/usr/bin/bash`; trusted updater затем сам проверяет весь
  checkout и выполняет canonical workflow. Direct exit code не маскируется, а
  menu после invocation завершается из-за возможного Manager self-refresh.
- Миграции выполняются отдельной командой; приложение не запускает Alembic автоматически.
- Lifecycle имеет единую точку запуска, graceful shutdown и централизованное управление background tasks.
- Штатный SIGINT завершается успешно только после async cleanup Discord client/background tasks и database engine; entrypoint подавляет соответствующий `KeyboardInterrupt`, но не произвольную cancellation или runtime exceptions.
- Python-пакет использует src-layout, устанавливается через uv и запускается командой `python -m discord_stats_bot`.
- Текущая точка запуска валидирует Settings, настраивает logging, явно создаёт persistence resources, запускает Discord Gateway и освобождает engine при завершении.
- `DATABASE_URL` валидируется локально как `postgresql+asyncpg`, а timezone — как IANA key; эти проверки не создают сетевых подключений.
- Persistence resources создаются только явно, не открывают соединение при import и не определяют скрытые транзакционные границы.
- Alembic использует async pattern и `Base.metadata`, получает URL из общей database-only конфигурации и требует только `DATABASE_URL`, независимо от Discord credentials.
- PostgreSQL integration smoke-test является opt-in, использует только `TEST_DATABASE_URL` и без неё не создаёт сетевых подключений; автоматическое развёртывание тестовой БД не входит в текущую инфраструктуру.
- Первая business-схема состоит из шести таблиц и использует логические `voice_sessions` с канальными `voice_intervals`; обычная статистика считает только exact-время, а estimated duration остаётся отдельной величиной.
- Discord snowflake ID хранятся как `BIGINT`, внутренние session/interval ID — как identity `BIGINT`, timestamps — как `TIMESTAMPTZ`; duration и aggregate/cache tables не хранятся.
- Единственность открытых session и interval для пары guild/user описана в ORM metadata через PostgreSQL partial unique indexes; будущая runtime-обработка участника сериализуется транзакционно.
- Live voice transition repository сериализует пару guild/user блокировкой существующей строки `guild_members`; создание guild/user/member/channel выполняется заранее отдельным кодом.
- Repository использует caller-owned `AsyncSession` без скрытого commit, а application service рассчитан на один внешний transaction scope для lock/read/transition.
- Runtime voice-переходы создают только exact intervals; stale-проверка учитывает подтверждённую границу как открытой session, так и закрытой истории, а повторное актуальное состояние возвращает `unchanged`.
- Startup/crash reconciliation использует сохранённый `H = confirmed_through_at` и единый `R`: только совпадение полного channel snapshot позволяет сохранить logical session и обозначить `[H, R]` как estimated; отличие snapshot или отсутствие пользователя закрывает старую историю на `H` без приписывания downtime конкретному каналу.
- Gateway startup adapter на `on_ready` использует одно `R` для всех пользователей, получает persisted open users через repository, преобразует cached voice/stage states в Discord-независимые snapshots и выполняет существующие service-операции в отдельных транзакциях; повторные ready-вызовы сериализуются.
- Reference provisioning отделён от voice tracking: одна caller-owned транзакция upsert-ит доступный guild cache в порядке внешних ключей и не удаляет отсутствующие строки; только после commit запускается startup reconciliation, а `VoiceTrackingService` сохраняет требование заранее provisioned references.
- Live Gateway adapter использует одну caller-owned транзакцию на событие, выполняет отдельный targeted provisioning и делегирует join/move/leave/no-op semantics существующему `VoiceTrackingService`; bots и другие guild отбрасываются до persistence.
- Periodic voice checkpoint использует authoritative connected cache snapshot, единый UTC timestamp на цикл и отдельную transaction на пользователя; отсутствующие cache users не закрываются, а connected observations проходят через существующий `VoiceTrackingService.observe_connected`.
- Checkpoint loop существует только после успешного recovery, отменяется на disconnect/recovery/shutdown и не дублируется повторными ready/resumed.
- Game Tracking хранит только Playing identity и подтверждённые границы в
  `game_sessions`. Application ID приоритетнее normalized display name;
  checkpoint выполняется batched, а crash/reconnect downtime никогда не
  добавляется автоматически. Partial unique open index дополняет, но не заменяет
  member-row locking и transaction state machine.
- Voice statistics одним PostgreSQL aggregate query считает exact/estimated пересечения четырёх окон; open intervals ограничены `confirmed_through_at`, а `VOICE_MIN_SESSION_SECONDS` применяется к whole-session exact duration.
- `/stats [user] [period]` зарегистрирована только для configured guild, отвечает ephemeral, допускает выбранного non-bot member, использует default 7d и choices today/7d/30d/all; command sync остаётся одноразовым в `setup_hook`.
- `/top` использует те же effective interval/window/threshold правила, публично показывает deterministic non-bot TOP 10 и не меняет ranking из-за отсутствия member в Discord cache; кликабельные user mentions не уведомляют пользователей благодаря `AllowedMentions.none()`.
- `/stats` использует два read-only query из одного `REPEATABLE READ` window snapshot: core profile aggregate и period TOP 3 companions; rank доступен за пределами TOP 10 и вычисляется тем же полным deterministic ordering, что `/top`.
- `/help` остаётся Discord-only presentation concern: статический ephemeral embed не зависит от БД и перечисляет только зарегистрированные пользовательские команды.
- `/profile` отделяет reusable self/Purple/Gold member-statistics policy и
  Discord-независимый aggregate result от handler/embed. Privileged access
  определяется только optional stable Role ID и при неполной конфигурации
  fail-closed; автоматическая progression ролей в Profile v1 не входит.
- Автоматические годовщины используют единый `audit_events` outbox и delivery runner: partial unique guild/user/local-date key обеспечивает durable enqueue idempotency, Discord failure остаётся pending с retry, а отдельный local-time worker не дублируется READY/RESUME и отменяется при shutdown.
- Channel и companion analytics агрегируют существующие persisted `voice_intervals`; profile favorite/companions ограничены выбранным периодом, а `/channels` остаётся отдельным period aggregate с defensive bot/AFK exclusion.
- `/channelstats` использует один statement snapshot: whole-session threshold считается до channel filter, channel total — по полному per-user набору до TOP-10, а выбранные Voice/Stage и текущие имена остаются Discord presentation concern.
- `/together` одним read-only statement считает pair exact/estimated total, оба individual all-time totals и TOP 3 common channels; percentages вычисляются только в presentation, denominator zero даёт `0%`, pair total не ограничивается и не маскируется.
- `/serverstats` одним read-only statement считает person-time exact/estimated total, число пользователей с положительным period overlap и TOP-1 user/channel через те же ordering helpers, что `/top` и `/channels`; average равен total/active users и безопасно даёт zero при пустом периоде.
- `/activity` получает только пересекающие 7/30/90-day window eligible intervals configured guild и распределяет person-time по local buckets в `REPORT_TIMEZONE`; recurring hours нормализуются по фактическому exposure, weekdays — по календарным появлениям, ties разрешаются deterministically. Numeric 8×7 heatmap остаётся Discord-independent и не требует secondary storage.
- PostgreSQL является durable source of truth audit history, Discord channel — at-least-once presentation. Ingestion commit предшествует send; actor enrichment пока не выполняется.
- Чистые channel position events сохраняются отдельно, а delivery объединяет только близкие события одного parent/category scope после 1,5-секундного debounce; delivered/failure state всей Discord-группы изменяется одной транзакцией.
- Voice tables являются source of truth для session/interval duration, а audit `details_data` хранит immutable historical presentation snapshot. Leave today total использует тот же timezone/threshold/AFK/exact-estimated query contract, что voice statistics; move не закрывает logical session.
- Transient audit events живут `AUDIT_TRANSIENT_RETENTION_DAYS`, important rows имеют `expires_at = NULL`; аватары сохраняются только как key/URL. Voice analytics остаётся в отдельных permanent voice tables.
- Audit использует guild/member/voice-state/moderation intents без message
  content, presence, typing и DM; optional Presence Intent принадлежит только
  Game Tracking.
- Autorole использует уже включённый members intent, не добавляет intents и не смешивается с Audit Logging domain; успешная выдача роли логируется audit через штатный member update.
- Achievement identity — стабильный lowercase key, а не title/tier; definitions живут в immutable code catalog. Дубликаты физически запрещены composite PK, repository не владеет transaction и возвращает unknown keys без автоматического удаления.
- Discord identity хранит только исходные mutable поля: глобальные username/global name/avatar hash в `discord_users` и guild nickname/avatar hash в `guild_members`; display name вычисляется при чтении, image bytes и identity/avatar history не сохраняются. Full Member может достоверно записать nullable guild identity, partial User обновляет global identity, но не очищает guild-specific поля.

## Следующие шаги

1. Продолжить D2.10+ отдельными небольшими manager checkpoint-этапами без
   преждевременного проектирования следующей mutating команды.
   Follow/Web Admin logs, install, Web Admin lifecycle, enable/disable,
   backup/restore/rollback и PostgreSQL/Alembic/HTTP doctor probes не входят в
   D2.9.
2. При следующем реальном этапе Rules Compliance / reacceptance проверить
   оставшийся production scenario: Publish новой Rules version → успешный DB
   commit → automatic Bot Control sync → обновление существующего managed message
   с сохранением message ID и отображением новой версии. Reminders, enforcement
   и forced reacceptance пока не реализованы и остаются future work.
3. Следующим audit-этапом отдельно спроектировать actor enrichment и `/history`,
   не смешивая их с текущим ingestion.
4. Отдельно спроектировать дедупликацию/коррекцию message events,
   opt-out/data deletion, text channel analytics и pagination.
5. После проверки Debian install/update flow определить backup и полноценный
   production health monitoring.
6. Для production migration с локального Caddy на отдельную Nginx VM: подготовить
   DNS/TLS и remote Nginx config, создать отдельный web env/user/unit, задать
   конкретный private `WEB_ADMIN_HOST` + opt-in, разрешить TCP/8000 только с IP
   proxy VM, сохранить 8765/5432 loopback-only, затем выполнить OAuth и все четыре
   bot-profile smoke tests. Только после проверки переключить DNS и убрать Caddy.
