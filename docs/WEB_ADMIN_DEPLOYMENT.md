# Публичное развёртывание Kanami Web Admin

Debian 13 installer D2.12 может опционально подготовить Web Admin foundation:
отдельные `kanami-web`, runtime, protected env, least-privilege DB role и
systemd unit. Он не устанавливает proxy/TLS, не проверяет публичный callback, не
включает Bot Control и не запускает Web Admin. Шаги ниже завершаются оператором
отдельной D2.13 operation перед первым запуском; prepared port 8000 нельзя
публиковать напрямую.

Canonical recommended completion после настройки DNS:

```bash
sudo kanami web-setup
```

Команда поддерживает только same-host managed Caddy topology. Existing Caddy,
Nginx, Traefik и central/remote proxy остаются manual advanced modes из этого
документа: automation не пытается объединять или переписывать чужую
конфигурацию.

Web Admin публикуется только через TLS reverse proxy. Процесс Web Admin не имеет
`DISCORD_TOKEN`, работает от отдельного пользователя, использует отдельную
PostgreSQL role с минимально необходимыми правами и вызывает
bot-profile/managed-access операции только через `127.0.0.1:8765`.
Control API никогда не публикуется и не слушает LAN.

## Поддерживаемые схемы

### 1. Standalone: Caddy и Kanami на одной VM

Рекомендуемый вариант, если готового reverse proxy нет:

```text
Internet
  -> Caddy :443
  -> 127.0.0.1:8000
  -> Kanami Web Admin
```

Оставьте `WEB_ADMIN_HOST=127.0.0.1`, настройте public DNS и выполните
`sudo kanami web-setup`. Operation выводит hostname только из уже сохранённого
точного OAuth callback, проверяет DNS, устанавливает официальный Debian 13
package `caddy`, генерирует `/etc/caddy/Caddyfile` из tracked
`deploy/caddy/Caddyfile.managed.template` и использует обычный automatic HTTPS.
Kanami не добавляет Cloudsmith или другой сторонний apt repository. `tls
internal` для публичного домена не используется.

Debian package на первой установке включает и пытается запустить Caddy. Поэтому
`web-setup` до `apt-get install caddy` создаёт только собственную временную
systemd mask, после package install снимает package-created enablement, а маску
удаляет на success и failure paths. Уже установленный Caddy принимается только
при точном совпадении Kanami-managed config для OAuth hostname; любой чужой или
неоднозначный Caddyfile приводит к отказу и переходу к manual path. Copy-paste
пример остаётся в
[`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example).

### 2. Существующий proxy на той же VM

```text
Internet
  -> Nginx/Caddy/Traefik :443
  -> 127.0.0.1:8000
  -> Kanami Web Admin
```

Kanami Caddy не устанавливается. Для Nginx используйте
[`deploy/nginx/kanami-same-host.conf.example`](../deploy/nginx/kanami-same-host.conf.example)
и приложенный snippet. Bind и здесь остаётся loopback без дополнительных флагов.
Не запускайте managed `web-setup` поверх этой topology: подготовьте Bot Control
pairing, proxy config и service lifecycle вручную по тем же invariants.

## Что проверяет и изменяет `web-setup`

До final confirmation выполняются только read-only проверки:

- Debian 13 и полный trusted root-owned `/opt/kanami` contract;
- complete D2.12 users/runtime/env/units с exact owner/group/mode и без symlink;
- real `/etc/kanami` `root:kanami` `0750`, взаимная supplementary-group
  изоляция `kanami`/`kanami-web` и уже active+enabled Core;
- bounded exact-key parsing protected env без `source`/`eval`, включая duplicate
  rejection;
- exact same-host Web contract `127.0.0.1:8000`, Secure cookie и выключенный
  либо отсутствующий private-bind opt-in;
- exact HTTPS OAuth callback, обычный public DNS hostname и bounded DNS lookup;
- отсутствие foreign proxy deployment либо exact existing managed Caddy config;
- для installed Caddy — изолированный `caddy` user, canonical effective Debian
  unit без drop-ins, trusted `caddy:caddy` `/var/lib/caddy` и
  inactive+disabled `caddy-api.service`;
- полное отсутствие Bot Control keys или уже complete exact pairing.

Summary показывает hostname, OAuth callback и только loopback endpoints. После
явного `y` operation:

1. создаёт один `openssl rand -hex 32` secret только при полностью отсутствующем
   pairing и отдельно заменяет каждый env с сохранением остальных keys:
   random-name staging остаётся root-only `0600`, canonical files после rename
   получают `root:kanami`/`root:kanami-web` `0640`;
2. безопасно устанавливает Caddy при необходимости, выполняет `caddy fmt` и
   `caddy validate` от Debian service user `caddy`, записывает
   `/etc/caddy/Caddyfile` как `root:root` `0644`; package scripts получают
   scoped `umask 022`, а `/var/lib/caddy` проверяется как real
   `caddy:caddy` non-group/other-writable directory без automatic repair;
3. перезапускает `kanami.service`, проверяет authenticated Bot Control только на
   `127.0.0.1:8765` без secret в argv;
4. запускает Web Admin и требует direct-TCP bounded local
   `200 {"status":"healthy"}` на `127.0.0.1:8000/admin/health` без влияния
   proxy environment;
5. запускает/reload Caddy, выполняет best-effort direct-network public HTTPS
   smoke с exact HTTP 200 и только
   после обязательных local smoke включает Web Admin и Caddy at boot.

Partial, contradictory или mismatched Bot Control state никогда не repair-ится
автоматически. Между двумя env нет cross-file transaction: ошибка после первого
rename или любая более поздняя ошибка явно требует manual inspection. Setup не
обещает rollback, не меняет firewall и не включает HSTS.

### 3. Центральный proxy на отдельной VM

```text
Internet
  -> Nginx/Caddy/Traefik на proxy VM :443
  -> private IP Kanami VM:8000
  -> Kanami Web Admin
```

На Kanami VM задайте ровно один адрес внутреннего интерфейса:

```dotenv
WEB_ADMIN_HOST=192.168.50.10
WEB_ADMIN_ALLOW_PRIVATE_BIND=true
```

Поддерживаются literal RFC1918 IPv4 и IPv6 ULA. Hostname, wildcard `0.0.0.0`/`::`,
link-local и публичный IP отклоняются при startup. При private bind процесс пишет
warning о необходимости firewall. Firewall Kanami VM обязан разрешать TCP/8000
только с IP proxy VM. Используйте
[`deploy/nginx/kanami-remote-proxy.conf.example`](../deploy/nginx/kanami-remote-proxy.conf.example).

Цепочка `Internet -> Nginx -> Caddy -> Web Admin` технически допустима, но обычно
не нужна: два proxy усложняют TLS, диагностику и заголовки. Штатно выбирайте один.

## Домен, TLS и Discord OAuth

Для публичного URL `https://kanami.example.com/admin/` callback должен в точности
равняться:

```text
https://kanami.example.com/admin/auth/discord/callback
```

То же значение задаётся в `WEB_ADMIN_DISCORD_REDIRECT_URI` и Discord Developer
Portal. В production обязательно `WEB_ADMIN_COOKIE_SECURE=true`. OAuth scope остаётся
только `identify`; `guilds`, `administrator` и другие scopes не нужны.

Uvicorn запускается с отключённым доверием к proxy headers. Авторизация, redirect URI
и Secure cookie не вычисляются из `Host`, `Forwarded` или `X-Forwarded-*`. Примеры
Nginx дополнительно очищают эти входные заголовки. Поэтому trusted-proxy модель в
приложении не нужна.

Приложение централизованно добавляет CSP, `nosniff`, `DENY` framing,
`Referrer-Policy: no-referrer`, минимальную Permissions Policy и `Cache-Control:
no-store`. HSTS задаётся на TLS proxy, а не HTTP backend. Включайте HSTS только после
настройки DNS/TLS и успешных HTTPS, OAuth и write-action smoke tests: браузер
запоминает политику и до истечения `max-age` может отказаться открывать HTTP. Поэтому
во всех Caddy/Nginx examples годовой HSTS по умолчанию закомментирован и включается
оператором только после этих проверок. Оба Nginx examples содержат отдельный port 80
server block, перенаправляющий запросы на HTTPS; основной Web Admin proxy остаётся на
443.

Все официальные reverse-proxy examples публикуют только Web Admin namespace и
используют единый routing contract:

```text
/          -> 302 /admin/
/admin/*   -> Kanami Web Admin
остальные публичные paths -> 404
```

Поэтому пользователь может открыть просто `https://kanami.example.com/`, после
чего reverse proxy перенаправит браузер на `/admin/`. Произвольные application
paths, включая `/control`, наружу не проксируются. Bot Control на TCP/8765
по-прежнему доступен только через loopback и не является частью публичного HTTP
routing.

## Sessions, write authorization и abuse protection

Cookies содержат только случайные opaque ID, имеют `HttpOnly`, `SameSite=Lax`,
ограниченный `Path`, абсолютный lifetime (по умолчанию 8 часов) и в production
`Secure`. Session и CSRF находятся в bounded server-side memory и исчезают при
restart. `WEB_ADMIN_COOKIE_SECURE=false` разрешён только для loopback HTTP разработки.

Перед nickname/avatar update/reset приложение сначала проверяет session и CSRF,
затем повторно определяет OWNER/ADMIN и проверяет current non-bot guild membership. Потерянная
авторизация или ошибка БД даёт нейтральный 403, отзывает session и не вызывает control
API. Такой порядок не позволяет запросу с неверным CSRF использовать DB check как
authorization oracle. Успешно авторизованные writes ограничены bounded in-memory
limiter: 10 операций на session за 60 секунд, максимум 1024 ключа; restart очищает его.

`GET /admin/administrators` и оба POST доступны только fresh-authorized OWNER.
Два ID из `WEB_ADMIN_ALLOWED_USER_IDS` остаются постоянными OWNER, показываются
без revoke controls и не заменяются DB grants. Managed ADMIN являются
дополнительными; grant допускает только current non-bot member, а mutation идёт
через фиксированные Bot Control endpoints. Единственное прямое write-направление
Web Admin — Rules management и связанный audit outbox; остальные изменения идут
через Bot Control.

Nginx примеры добавляют мягкие network limits для login, callback и settings. Они
используют реальный TCP client address самого Nginx, а не forwarded headers. Vanilla
Caddy не имеет удобного встроенного rate limiter; сторонний plugin ради этого не
требуется. При необходимости network/WAF limiting для Caddy обеспечивается внешним
edge или firewall, а application limiter продолжает защищать writes.

## Health check

`GET /admin/health` намеренно остаётся без session для systemd/proxy monitoring. Он
выполняет реальный `SELECT 1`, возвращает `200 {"status":"healthy"}` или
`503 {"status":"unhealthy"}` и не раскрывает имя БД, connection string, latency или
текст исключения.

## Firewall

Same-host deployment:

```text
public:   TCP/80, TCP/443 -> reverse proxy
loopback: 127.0.0.1:8000  -> Web Admin
loopback: 127.0.0.1:8765  -> bot control (ALWAYS)
loopback: 127.0.0.1:5432  -> PostgreSQL (recommended)
```

Remote-proxy deployment:

```text
Kanami VM TCP/8000 <- ONLY reverse-proxy VM private IP
Kanami VM TCP/8765 <- loopback only, no exceptions
Kanami VM TCP/5432 <- loopback only in the recommended architecture
```

Пример UFW — замените placeholder после проверки адреса, не копируйте вслепую:

```bash
sudo ufw allow from REVERSE_PROXY_PRIVATE_IP to any port 8000 proto tcp
sudo ufw deny 8000/tcp
```

Порядок UFW rules зависит от существующей политики; сначала проверьте `ufw status
numbered` и не потеряйте SSH-доступ. Kanami не меняет iptables/nftables/UFW
автоматически. SSH не относится к Web Admin. Порты 8000, 8765 и 5432 нельзя открывать
в Internet.

## Разделение процессов и secrets

Рекомендуются users `kanami` и `kanami-web`. Пример Web Admin unit находится в
[`deploy/systemd/kanami-web-admin.service.example`](../deploy/systemd/kanami-web-admin.service.example).

```text
/etc/kanami/kanami.env
  root:kanami 0640
  DISCORD_TOKEN
  DISCORD_BOT_CONTROL_SHARED_SECRET (создаётся D2.13 при первом pairing)

/etc/kanami/kanami-web-admin.env
  root:kanami-web 0640
  DATABASE_URL (отдельная least-privilege PostgreSQL role)
  DISCORD_GUILD_ID
  REPORT_TIMEZONE
  GAME_TRACKING_ENABLED
  GAME_CONFIRM_INTERVAL_SECONDS
  WEB_ADMIN_DISCORD_CLIENT_SECRET
  WEB_ADMIN_BOT_CONTROL_SHARED_SECRET (создаётся D2.13 при первом pairing)
  (никогда не DISCORD_TOKEN)
```

systemd читает `EnvironmentFile` до privilege drop, поэтому runtime user может не
иметь прямого доступа к файлу. D2.12 не включает Bot Control и не создаёт его
URL/shared secrets; D2.13 делает это только когда все шесть pairing keys
отсутствуют. Уже complete exact pairing переиспользуется без rotation; partial
или mismatched state fail-closed. Shared control secret является отдельным
случайным значением не короче 32 символов и совпадает в bot/web env. После
добавления Rules
Admin web connection создаётся с `read_only=False`, поэтому PostgreSQL connection
не получает `default_transaction_read_only=on`. Это не означает право записи во
всю схему: production Web Admin DB role должна получать только необходимые
`SELECT` grants для dashboard/authorization/operations и только необходимые
`INSERT`/`UPDATE`/`DELETE` grants для `rulesets` и `audit_events` (плюс доступ к
используемым ими sequences). Не выдавайте этой роли ownership схемы/database,
`CREATE`, `TRUNCATE` или неограниченные write grants. Реальные секреты и настоящий
`.env` не сохраняются в Git.

D2.12 хранит эту explicit policy в
`deploy/postgresql/kanami-web-admin-grants.sql`: installer применяет её после
migrations, updater — повторно после migrations только для complete inactive
Web installation. Policy отзывает у `PUBLIC` CONNECT/TEMPORARY на созданной
Kanami database и CREATE на её `public` schema. Rules publish использует общий с
bot acceptance transaction advisory guild lock, поэтому web role не получает
`UPDATE` на `guilds`.

За основу web-файла используйте отдельный безопасный пример
[`deploy/systemd/kanami-web-admin.env.example`](../deploy/systemd/kanami-web-admin.env.example),
а не `/etc/kanami/kanami.env` основного bot process. Как минимум следующие
несекретные runtime settings должны явно совпадать в обоих env:

| Переменная | Зачем нужна Web Admin |
| --- | --- |
| `DISCORD_GUILD_ID` | Ограничивает все read models одним configured guild. |
| `REPORT_TIMEZONE` | Сохраняет общие календарные границы и отображение времени. |
| `VOICE_CHECKPOINT_INTERVAL_SECONDS` | Синхронизирует Voice freshness threshold на `/admin/system`; не запускает checkpoint в Web Admin. |
| `GAME_TRACKING_ENABLED` | Только отображает фактическое bot-side состояние на `/admin/system`. |
| `GAME_CONFIRM_INTERVAL_SECONDS` | Только отображает фактический bot-side checkpoint interval. |

`GAME_TRACKING_ENABLED=true` в web env **не запускает tracker**, не включает
Presence Intent и не создаёт Discord runtime: этим владеет только
`kanami.service` с bot env. Расхождение двух env не меняет работу tracker, но
делает Operations page фактически неверной. Аналогично рекомендуется повторить
`VOICE_MIN_SESSION_SECONDS`, потому что Web Dashboard использует общий threshold.
`VOICE_CHECKPOINT_INTERVAL_SECONDS` является существующим bot setting и теперь
также входит в безопасный shared runtime context; это не новый config knob.

W1.3 не меняет это разделение: минутные
`operational_health_observations` пишет и очищает только основной
`kanami.service` с обычной bot-side DB role. Web Admin читает историю через свою
отдельную role и не должен получать для этой таблицы `INSERT`/`DELETE`. Перед
первым запуском W1.3 штатно выполните `alembic upgrade head`; новый env key не
требуется.
Сразу после deployment окна 24h/7d корректно отображаются как частичные.
Если ранее созданная web role получила read grants через разовые команды без
`ALTER DEFAULT PRIVILEGES`, после migration отдельно выдайте ей только
`GRANT SELECT ON TABLE operational_health_observations` (в примере роль
`kanami_web_readonly`). Не выдавайте этой роли доступ к sequence и права
`INSERT`, `UPDATE`, `DELETE` или `TRUNCATE`. Штатный `kanami_app` владеет
database и получает bot-side write права как owner.

### Git metadata при раздельных service users

Checkout `/opt/kanami` остаётся во владении `root:root`, а Web Admin
запускается как `kanami-web` без write-доступа к repository. Современный Git
отклоняет repository другого владельца как `dubious ownership`, пока оператор
не разрешит конкретный путь для конкретного web user. После создания home
настройте ровно `/opt/kanami`:

```bash
sudo install -d -m 0750 -o kanami-web -g kanami-web /var/lib/kanami-web
sudo runuser -u kanami-web -- env HOME=/var/lib/kanami-web \
  git config --global --add safe.directory /opt/kanami
sudo runuser -u kanami-web -- env HOME=/var/lib/kanami-web \
  git -C /opt/kanami rev-parse --short HEAD
```

Пример unit явно задаёт `HOME=/var/lib/kanami-web`, поэтому application Git
subprocess читает тот же user-scoped config. `safe.directory` снимает только
Git-проверку доверия и не выдаёт filesystem write permissions. Не меняйте owner
checkout, не добавляйте `kanami-web` write-доступ и не используйте
`safe.directory=*`.

D2.12 installer выполняет эту настройку только для явно выбранного нового
`kanami-web` и проверяет read-only `rev-parse`. Он не меняет system/global Git
config. Updater использует уже подготовленный user/runtime только при полном
наборе canonical markers и не чинит partial state. Application code сохраняет
безопасный fallback `Unknown`, если Git или metadata недоступны.

Перед обновлением complete D2.12/D2.13 installation updater проверяет canonical
owner/group/mode каталогов, env и unit и требует `kanami-web-admin.service` в
точном state `inactive`. Active или недостоверный state останавливает update до
`git pull`; автоматического stop/start/restart нет. После успешных migrations
updater refresh-ит web unit и выполняет `daemon-reload`, не запуская service.
После production activation сначала явно выполните
`sudo systemctl stop kanami-web-admin.service`; updater не управляет Caddy и не
выполняет silent stop/start Web Admin.

## Checklist перед публикацией

- [ ] Public DNS настроен.
- [ ] Inbound TCP/80 и TCP/443 приводят к managed Caddy host.
- [ ] Для recommended topology выполнен `sudo kanami web-setup`.
- [ ] Установлен действующий публичный TLS certificate.
- [ ] `WEB_ADMIN_COOKIE_SECURE=true`.
- [ ] OAuth redirect совпадает в точности.
- [ ] Настроены allowed admin IDs.
- [ ] Web Admin запущен отдельным Linux user.
- [ ] Shared runtime settings явно синхронизированы в отдельном web env.
- [ ] Для `kanami-web` разрешён только `safe.directory=/opt/kanami`; Git metadata видна.
- [ ] Web DB role имеет только scoped Rules/audit writes и необходимые read grants.
- [ ] Bot control слушает только loopback.
- [ ] Firewall проверен.
- [ ] TCP/8000 не доступен напрямую из Internet.
- [ ] TCP/8765 не опубликован.
- [ ] TCP/5432 не опубликован.
- [ ] Secrets отсутствуют в repository и логах.
- [ ] Сделан backup.
- [ ] Выполнен OAuth login smoke test.
- [ ] Выполнены nickname/avatar/reset smoke tests.
- [ ] Оба env OWNER видны и защищены; managed ADMIN grant/revoke smoke выполнен.
