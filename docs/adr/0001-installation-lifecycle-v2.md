# ADR 0001: Installation & Lifecycle v2

- Статус: **Accepted**
- Дата: 2026-09-13
- Этап: D2.14 Architecture Baseline
- Ветка baseline: `feature/installer-v2`
- Commit baseline: `e6655758de7948f856bb8c95e4243eb7fbedec07`

## Context

Текущий installation/lifecycle flow вырос из последовательности D2.1–D2.13.
Он уже устанавливает Kanami на Debian 13, создаёт root-owned production
checkout, разделяет identities `kanami` и `kanami-web`, настраивает локальный
PostgreSQL, Alembic, scoped ACL, optional Web Admin, loopback Bot Control,
Manager, updater, systemd и содержит managed Caddy activation/hardening logic.
Регрессионные тесты этого flow защищают важные security invariants; этот baseline
не утверждает, что старый D2.13 Caddy flow полностью прошёл clean-host
end-to-end acceptance.

При этом D2.13 остался интерактивной root-shell orchestration: состояние
распределено между Bash stages и файлами, recovery зависит от ручной
диагностики, release boundary основана на Git checkout, а setup UX не имеет
отдельной минимально привилегированной Web control plane. Продолжать расширять
этот flow как конечную модель нельзя без явного protocol boundary, immutable
plan, durable operation state и platform contracts.

D2.14 фиксирует целевую архитектуру до реализации. Этот ADR нормативен для
D2.15–D2.23. В D2.14 не создаются daemon, socket, HTTP routes, systemd units,
platform code или provisioning mutations.

Термины **MUST**, **MUST NOT**, **SHOULD** и **MAY** в этом документе задают
обязательные, запрещённые, рекомендуемые и допустимые свойства будущей
реализации.

## Baseline

Архитектурный baseline принят на commit
`e6655758de7948f856bb8c95e4243eb7fbedec07` ветки
`feature/installer-v2` после следующих проверок:

| Среда или проверка | Результат |
| --- | --- |
| Windows pytest | 1556 passed, 354 skipped, 39 warnings |
| Debian 13 Linux pytest | 1864 passed, 46 skipped, 39 warnings |
| Ruff | PASS |
| `ruff format --check` | PASS |
| `git diff --check` | PASS |

Проверенная Linux platform evidence:

```text
os_id=debian
version_id=13
pretty_name=Debian GNU/Linux 13 (trixie)
uname_machine=x86_64
dpkg_arch=amd64
systemd=systemd 257 (257.13-1~deb13u1)
apt=apt 3.0.3 (amd64)
```

Первый официальный tested support profile:
`debian-13-amd64-systemd-apt-v1`.

## Superseded decisions

D2.13 имеет статус **transitional prototype — superseded by Web Setup
redesign**. Его старый интерактивный clean-install/root-shell UX не является
целевой архитектурой и не развивается как D2.14+ setup experience.

Это решение не удаляет и не обесценивает текущую реализацию:

- `scripts/install.sh`, `scripts/web-admin-setup.sh` и `scripts/update.sh`
  остаются transitional implementation и эксплуатационным reference;
- текущие security и negative tests остаются обязательными до feature parity;
- установленные D2.12/D2.13 hosts не изменяются самим D2.14;
- `sudo kanami web-setup` описывает текущую transitional operation, а не
  конечный Web Setup API или UX;
- retirement старых scripts разрешён только после feature и negative-test
  parity нового flow и отдельного migration decision.

## Target architecture

```text
Browser
    |
    | HTTPS
    v
Temporary / Managed Proxy
    |
    v
kanami-setup-web
user: kanami-setup
UNPRIVILEGED
    |
    | typed, versioned RPC
    | local Unix socket
    v
kanami-installerd
user: root
NO TCP listener
    |
    +--> HostPlatform / PlatformProvider
    +--> packages and filesystem
    +--> PostgreSQL, migrations and ACL
    +--> systemd
    +--> ManagedProxy
    +--> verified release activation
```

`kanami-setup-web` владеет только browser UX, authentication/session state,
построением draft plan и отображением результата. Он MUST работать без root и
не получает generic mutation primitive.

После ограниченного D2.16 bootstrap, описанного ниже, `kanami-installerd`
является единственным privileged production-mutation boundary. Он MUST сам
валидировать peer, protocol, plan, platform, trusted release и allowlisted
operation. Он MUST NOT доверять browser-derived paths, packages, units,
commands, SQL, URLs или provider selection.

Production identities `kanami` и `kanami-web` не являются setup identities.
Отдельный `kanami-setup` MUST NOT входить в группы `kanami` или `kanami-web`.
Production users MUST NOT иметь доступ к installer Unix socket.

`kanami-setup` и `kanami-setup-web` MUST NOT читать production
`/etc/kanami` secret env/config, protected secret generations, Bot Control
secret или arbitrary production release filesystem. Setup Web MUST NOT иметь
PostgreSQL credentials, подключаться к PostgreSQL напрямую или выполнять
privileged local subprocesses. Любая production inspection или mutation
доступна ему только через typed RPC к `kanami-installerd`. Временно принятые от
пользователя `SecretInputs` не сохраняются и не логируются и передаются helper-у
one-shot.

## Bootstrap privileged exception

До запуска Setup Web допускается единственное исключение из правила root-helper
boundary: минимальный root-owned bootstrap для bring-up самой setup control
plane. Bootstrap MAY только:

- создать identity `kanami-setup`;
- создать фиксированные setup-only directories;
- установить фиксированные setup units и assets;
- установить или запустить temporary Managed Caddy setup route;
- создать installer socket/service;
- создать собственный durable bootstrap receipt/state;
- включить только setup control plane.

Bootstrap MUST быть small, idempotent, fixed-input, fail-closed и построен как
inspect/apply/verify. Все assets, paths, units и допустимые actions заданы
root-trusted bootstrap implementation; generic command, package, path, URL или
provider inputs запрещены. Receipt MUST позволять отличить `ABSENT`, ожидаемый,
partial и foreign bootstrap state до повторного применения.

Bootstrap implementation, metadata и assets MUST происходить только из
bundled/pinned trusted bootstrap artifact либо с fixed allowlisted official
HTTPS origin с обязательной immutable manifest/digest verification. User,
browser и `InstallPlan` MUST NOT задавать bootstrap/repository/artifact URL,
branch или arbitrary package list. Unverified moving `curl | sh`, `latest`
branch или аналогичный moving target не является trust model.

На clean host bootstrap MAY установить только фиксированный allowlisted набор
setup prerequisites, например необходимый runtime или Managed Caddy. Этот набор
задаётся trusted bootstrap metadata и exact supported-platform semantics; это
не generic `install_package(name)`. Future signature verification остаётся
отдельным hardening.

Bootstrap MUST NOT создавать production users `kanami`/`kanami-web`, production
database, production config или production ACL; запускать migrations;
устанавливать production release; выполнять Kanami provisioning или arbitrary
shell actions. После bring-up setup plane все дальнейшие privileged production
mutations MUST выполняться только через `kanami-installerd`.

## Trust boundaries

1. **Browser → HTTPS proxy.** Browser считается недоверенным. Все поля,
   headers, cookies и порядок запросов валидируются server-side. Proxy
   публикует только setup surface, пока setup plane активна.
2. **Proxy → `kanami-setup-web`.** Setup Web не доверяет forwarded authority
   без точной deployment configuration. Host и Origin проверяются точно.
3. **`kanami-setup-web` → Unix socket.** Web process аутентифицируется kernel
   peer credentials, но его payload всё равно считается недоверенным и проходит
   строгую schema validation.
4. **Bootstrap → setup plane.** Минимальное root-owned fixed-input исключение
   создаёт только setup identity/assets/units/socket/temporary route и durable
   receipt. Оно не выполняет production provisioning.
5. **`kanami-installerd` → host.** После bootstrap root helper выбирает только
   закрытые operations, providers, capabilities и trusted release assets. Это
   единственная граница дальнейших privileged production mutations.
6. **Release boundary.** Manifest, templates и handler definitions из
   verified immutable release считаются trusted input после проверки identity и
   digest и membership release ID в root-trusted catalog. Digest сам по себе не
   доказывает publisher authenticity.
7. **Production runtime.** `kanami` и `kanami-web` получают только необходимые
   runtime/config/DB права. Ни один production process не получает installer
   socket или setup credentials.

Unix socket MUST быть локальным и не может иметь TCP equivalent. Ownership,
mode и parent-directory traversal MUST допускать ровно ожидаемую setup identity.
Root helper MUST проверять exact peer identity через `SO_PEERCRED` при каждом
принятом соединении. Групповая принадлежность production users не должна
создавать косвенный доступ.

## Root helper and RPC

Protocol MUST быть typed и versioned. Каждое сообщение имеет bounded byte size,
строгую schema version, request ID, ровно один allowlisted method и typed result
или typed error. Parser MUST отклонять malformed encoding, duplicate JSON keys,
unknown fields, invalid types, out-of-range values и trailing/oversized data.

Request ID используется для correlation, но не является authorization token и
не делает mutation идемпотентной сам по себе. Error response раскрывает только
стабильный code, безопасное bounded message и допустимые structured details; raw
exception, command output, SQL, secrets и host paths не возвращаются browser-у.

Одновременно на host допускается не более одной mutating installation
operation. Lock и durable ownership operation появляются до первой production
mutation. Read-only requests не могут обойти lock или превратиться в mutation.

D2.15 allowlist содержит только:

| Method | Семантика в D2.15 |
| --- | --- |
| `preflight` | Read-only discovery и безопасные diagnostics |
| `validate_plan` | Read-only schema/policy/platform validation |
| `status` | Read-only состояние helper/operation |

D2.15 MUST выполнять zero provisioning mutations.

Generic privileged API запрещён. В protocol никогда не добавляются:

```text
execute(command)
shell(...)
run_script(...)
write_file(path, ...)
install_package(name)
systemctl(unit_from_client)
sql(...)
arbitrary argv
arbitrary paths
arbitrary package names
arbitrary unit names
arbitrary URLs
```

Будущие mutating methods должны описывать domain operation, принимать только
closed identifiers и разрешаться root helper в заранее определённую цепочку
inspect/apply/verify из provider и trusted release.

Discord validation выполняется только через fixed trusted Discord API
endpoints. User input не может задавать scheme, host, arbitrary URL или redirect
target для server-side fetch. Root helper MUST NOT выполнять user-controlled
HTTP requests.

## InstallPlan

`InstallPlan` — immutable, secret-free описание подтверждённой операции.
Концептуальная schema v1 содержит как минимум:

```text
schema_version
operation_kind
support_profile
release_id
components
database.mode
proxy.mode
tls.mode
public_hostname
discord.guild_id
discord.report_timezone
web.oauth_client_id
web.owner_user_ids
update_policy
```

Нормативные правила:

- unknown/extra fields MUST быть rejected;
- enums и закрытые identifiers используются везде, где множество известно;
- strings и lists имеют явные min/max bounds;
- Discord snowflakes передаются decimal strings, а не JSON numbers;
- OWNER IDs валидируются как snowflakes, сортируются и дедуплицируются до
  canonical serialization;
- floating point запрещён;
- paths, package names, unit names, SQL, argv и arbitrary URLs запрещены;
- OAuth Redirect URI строится из validated `public_hostname` и фиксированного
  path; пользователь не передаёт arbitrary redirect URL;
- `release_id` content-addressed и имеет формат вида
  `sha256:<64 lowercase hex>`; branch, path, remote или repository URL им не
  являются;
- после final confirmation plan immutable; изменение создаёт новый plan и новый
  digest.

Canonical representation MUST быть RFC 8785 JSON Canonicalization Scheme (JCS)
в UTF-8. `plan_digest` равен lowercase SHA-256 canonical bytes. Root helper MUST
сам повторить strict parsing, normalization, canonicalization и digest
calculation; digest от client не считается доказательством содержимого.

`support_profile` в plan подтверждает результат root-side resolution, но не
позволяет client выбрать provider. Несовпадение plan profile с exact current
platform или installed profile fail closed.

## SecretInputs

Секреты отделены от `InstallPlan`. Концептуальная schema:

```text
schema_version
plan_digest
discord_bot_token
web_oauth_client_secret
```

`plan_digest` связывает write-only secrets с уже подтверждённым immutable plan.
PostgreSQL password и Bot Control secret генерируются root-side и не принимаются
из browser/RPC как выбираемые значения.

Secrets MUST NOT сохраняться в:

- `InstallPlan` или durable operation state DB;
- logs, diagnostics, exception text или audit details;
- argv, URLs или process titles;
- browser `localStorage` или `sessionStorage`;
- API responses после приёма;
- временные файлы с более широкими правами.

Web UI после настройки возвращает только `configured: true` и write-only
replacement action, но никогда не возвращает существующее значение.

После final confirmation root side MUST выполнить единый secret-durability
checkpoint. Сам checkpoint является обязательной protected host write; никакая
provisioning mutation вне него не может начаться до его завершения:

1. получить `SecretInputs`, связанные с exact `plan_digest`;
2. сгенерировать все root-side secrets;
3. сформировать полную protected config generation, например
   `/etc/kanami/generations/<plan-digest>/`;
4. безопасно записать все файлы с root ownership и строгими modes;
5. выполнить `fsync` каждого файла;
6. выполнить `fsync` directory metadata;
7. выполнить atomic activation/rename/current-pointer transition;
8. durable-зафиксировать в operation state, что полная secret/config generation
   существует для exact `plan_digest`.

Только после успешного шага 8 helper может начать provisioning mutations вне
checkpoint: packages, production users, release, PostgreSQL, migrations,
systemd, proxy или ACL. Набор generation активируется целиком; per-file
cross-generation mixing не допускается.

Если crash, reboot или browser/session loss произошёл до этого checkpoint,
production provisioning ещё не начинался, operation переходит или возвращается
в `WAITING_FOR_SECRETS`, а `SecretInputs` вводятся заново. Helper не пытается
восстановить их из logs, state DB или browser storage. Если generation и запись
о ней уже durable, browser/session loss не требует повторного ввода secrets:
state machine продолжает ту же operation по durable state.

## State and recovery

Три модели не взаимозаменяемы:

```text
OperationStatus:
  CONFIRMED
  WAITING_FOR_SECRETS
  RUNNING
  WAITING_FOR_REBOOT
  BLOCKED
  SUCCEEDED
  FAILED
  CANCELLED

StageStatus:
  PENDING
  RUNNING
  SUCCEEDED
  FAILED
  SKIPPED
  WAITING_FOR_REBOOT
  BLOCKED

ResourceState:
  ABSENT
  EXPECTED
  PARTIAL
  FOREIGN
```

Operation status описывает workflow, stage status — исполнение конкретного
шага, resource state — результат независимой inspection host resource. Stage
status MUST NOT выводиться напрямую из resource state или наоборот.

Перед mutation durable journal фиксирует intent, operation/stage identity и
ожидаемое состояние; после mutation и verification фиксируется результат.
После crash сохранённый `RUNNING` не превращается автоматически в `FAILED` или
`SUCCEEDED`. Recovery сначала выполняет stage-specific inspect:

- `EXPECTED` может подтвердить завершение и позволить продолжение;
- `ABSENT` может разрешить безопасный повтор только по правилам stage;
- `PARTIAL` требует explicit stage-specific recovery;
- `FOREIGN` всегда fail closed и не merge/rewrite автоматически.

Global rollback не обещается. Release/config pointer rollback отличается от
PostgreSQL migration rollback; обратимость одного не означает обратимость
другого. Каждая stage обязана документировать idempotency, inspection,
verification, interruption points и допустимое восстановление.

Durable state machine, lock и recovery MUST существовать до production
mutation Managed Caddy. Поэтому durable state — D2.18, а Managed Caddy mutation —
D2.19.

## HostPlatform and PlatformProvider

Root-side discovery создаёт bounded typed `HostPlatform`:

```text
HostPlatform:
  schema_version: 1
  os_id
  version_id
  id_like
  architecture:
    uname_machine
    package_architecture
    canonical
  init_system
  package_family
  support_profile
  support_status
```

`support_status` — closed enum:

```text
supported
acceptance_candidate
unsupported
installed_profile_mismatch
```

Canonical distro/version source — `/etc/os-release`. Future parser MUST быть
bounded, не исполнять содержимое и MUST NOT использовать shell `source` или
`eval`. Architecture определяется через `uname -m`; на APT/DPKG platform
package architecture определяется `dpkg --print-architecture`. `ID_LIKE`
служит только diagnostics и никогда не выбирает provider автоматически.

Только root-side resolver выбирает exact `support_profile`. Browser, Web
process и RPC client не могут выбирать arbitrary provider.

Концептуальный contract provider:

```text
PlatformProvider:
  profile_id
  exact_match(platform_facts)
  inspect_platform()
  inspect_resource(resource_id)
  package_allowlist
  service_semantics(service_id)
  postgresql_semantics()
  proxy_prerequisites(proxy_id)
  verification_rules(stage_id)
```

`ResourceId`, `PackageCapability`, `ServiceId` и `StageId` — closed enums из
trusted release. Например, `PackageCapability.POSTGRESQL` разрешается Debian
provider в фиксированный tuple package names. Package names не приходят из
`InstallPlan` или RPC.

Root helper сам выбирает packages, binary paths, argv, systemd units,
PostgreSQL operations, ACL и trusted templates из exact provider и verified
release. Универсальный provider/plugin execution framework не создаётся.

## Support matrix

| Platform | Profile | Статус D2.14 | Поведение |
| --- | --- | --- | --- |
| Debian 13, amd64, systemd, apt/dpkg | `debian-13-amd64-systemd-apt-v1` | **SUPPORTED / TESTED** | Exact match обязателен |
| Ubuntu LTS как generic family | Нет | `unsupported` | Не является profile и всегда fail closed |
| Конкретная будущая Ubuntu LTS / amd64 / systemd / apt-dpkg | `ubuntu-XX.XX-amd64-systemd-apt-v1` | `unsupported` в D2.14 | Может стать `acceptance_candidate` только после появления exact provider/candidate в D2.20 |
| Linux Mint, Pop!_OS и иные derivatives | Нет | `unsupported` | Fail closed |
| Unknown distro | Нет | `unsupported` | Fail closed |
| Non-systemd | Нет | `unsupported` | Fail closed |
| Иное package family | Нет | `unsupported` | Fail closed |
| arm64 и иные architectures | Отдельный будущий exact profile | `unsupported` до отдельной acceptance | Не наследуют amd64 support |

Exact match `debian-13-amd64-systemd-apt-v1` определяется сочетанием
`os_id=debian`, `version_id=13`, `package_architecture=amd64`, `systemd` и
package family `apt/dpkg`. Наблюдавшиеся при acceptance `systemd 257` и
`apt 3.0.3` являются evidence, а не version pins или частью identity profile.
Обычное совместимое security/update обновление Debian 13 до более новых
`systemd 257.x` или APT не меняет profile автоматически; compatibility всё равно
проверяется provider preflight.

`ubuntu-lts`, `ubuntu >= 24.04` и «все Ubuntu LTS» не являются допустимыми
support profiles или support claims. Generic Ubuntu LTS остаётся только
`unsupported`. Одна конкретная exact Ubuntu LTS amd64 profile может перейти из
`unsupported` в `acceptance_candidate` после появления provider/candidate в
D2.20 и стать `supported` только после clean-VM acceptance D2.23.

После distro upgrade `installed_profile != current_profile` даёт
`installed_profile_mismatch`. `status`, `doctor` и diagnostics MAY работать
read-only и показывать mismatch. `install`, `resume`, `update` и provisioning
MUST быть запрещены до explicit compatibility/migration path.

## ManagedProxy and TLS

Proxy и TLS представлены закрытыми contracts/enums, не универсальной plugin
framework.

```text
ManagedProxy:
  ManagedCaddy
  External       # future

TlsMode:
  PublicDnsAcme  # first MVP
  PublicIp       # future, capability-gated
  PrivateCa      # future/manual
  External       # future
```

Первый Web Setup MVP поддерживает заранее известный public DNS hostname,
Managed Caddy, browser-trusted public HTTPS через ACME, local PostgreSQL,
polling operation status и notify/manual updates.

Public setup hostname MUST быть получен bootstrap-ом из trusted operator input
или root-trusted bootstrap configuration до запуска Setup Web. Bootstrap MUST
проверить hostname syntax, выполнить необходимые network/DNS/TLS preflight,
поднять trusted HTTPS setup endpoint и только затем показать setup URL
пользователю. Wizard MUST NOT начинаться по HTTP с последующим выбором hostname
и MUST NOT выбирать первый TLS hostname с нуля. Первый MVP имеет фиксированный
порядок: pre-known public DNS hostname → bootstrap → HTTPS → browser setup.

Архитектура не обещает browser-trusted HTTPS для private RFC1918 IP, public-IP
TLS без отдельной acceptance или automatic external proxy management. Foreign
proxy/config state всегда fail closed; helper не merge/rewrite его
автоматически. Managed Caddy production mutation появляется только после
D2.18 durable state.

## Setup authentication and lifecycle

Bootstrap token, browser session и durable installation operation — три
независимых lifecycle:

1. **Bootstrap one-time token.** Минимум 256 bits entropy, digest at rest,
   one-shot consume, TTL 15 минут и максимум 5 неуспешных попыток на bootstrap
   session. Token не находится в query URL; default UX — paste/code flow.
2. **Authenticated browser session.** Session cookie имеет `Secure`, `HttpOnly`,
   `SameSite=Strict`, `Path=/setup`; idle timeout равен 30 минутам, absolute
   timeout — 60 минутам. Все mutations требуют CSRF и exact Host/Origin
   validation.
3. **Durable installation operation.** После confirmation operation живёт в
   durable state независимо от token, cookie, browser connection или Setup Web
   process lifetime.

Token или session expiry MUST NOT отменять уже `CONFIRMED`/`RUNNING` operation,
переводить её автоматически в `FAILED`/`CANCELLED` или прерывать apt/package,
migration, systemd, proxy и другие stages. Setup HTTP session может истечь, пока
installer operation продолжает выполняться.

Teardown setup plane по TTL разрешён только при отсутствии активной mutating
operation либо при наличии безопасного reopen/re-auth flow, в котором durable
operation остаётся доступной независимо от browser session. Для активной
operation запрещён destructive teardown, делающий status/recovery/reopen
невозможным. После `SUCCEEDED` setup route/services останавливаются или
удаляются.

Если существует active/recoverable operation в `CONFIRMED`, `RUNNING`,
`BLOCKED`, `WAITING_FOR_SECRETS`, `WAITING_FOR_REBOOT` или ином `WAITING_*`
status, будущий `sudo kanami setup` создаёт новую authentication session к той
же durable operation. Browser/session expiry никогда не создаёт новую
operation. Если предыдущая operation terminal, например `SUCCEEDED`, изменение
configuration является новой mutating operation с новым immutable
`InstallPlan`, новым `plan_digest` и новым explicit confirmation. Confirmed plan
существующей operation никогда не редактируется.

Все Setup HTTP responses MUST иметь как минимум:

- restrictive `Content-Security-Policy` с
  `default-src 'self'; frame-ancestors 'none'`;
- `X-Content-Type-Options: nosniff`;
- `Cache-Control: no-store`;
- `Referrer-Policy: no-referrer`.

Полная nonce policy не определяется в D2.14, но эти свойства обязательны.

Дополнительные target properties:

- token/session/auth data не попадают в logs, URLs или durable operation data;
- authentication failure responses не раскрывают token validity сверх
  необходимого bounded результата;
- setup plane не становится production management surface после completion.

Fragment `#token` MAY быть исследован как будущий UX, но не является default и
не ослабляет server-side one-shot/TTL/attempt controls.

## Release boundary

Текущий `/opt/kanami` Git checkout и `git pull --ff-only` update остаются
transitional. Target Web Setup оперирует только `release_id`, а не Git path,
branch, remote, PAT, repository URL, arbitrary artifact/catalog URL или
user-selected digest вне trusted catalog.

Формат `sha256:<64 lowercase hex>` задаёт content identity, но сам по себе не
является authority и не доказывает publisher authenticity. Root helper MUST
принимать только `release_id`, присутствующий в root-trusted release catalog.
Catalog является либо bundled trusted metadata, либо получен с fixed allowlisted
HTTPS origin, заданного trusted release/bootstrap configuration. Browser,
пользователь и `InstallPlan` не могут выбирать или переопределять catalog/origin
URL.

Перед activation root helper MUST последовательно проверить catalog membership,
release manifest и artifact digest. Только после всех трёх проверок release
допускается к activation. `ReleaseProvider` предоставляет соответствующий
immutable verified release; signature verification остаётся future hardening
D2.22. Целевая раскладка может иметь вид:

```text
/opt/kanami/releases/<version-or-id>
/opt/kanami/current
```

`current` меняется atomically только после проверки release и stage-specific
verification. Core automatic update по умолчанию **OFF**. Default policy —
notify/manual. Update становится state-aware release operation и не использует
arbitrary Git input от setup client.

## Threat model

| Угроза | Обязательная защита |
| --- | --- |
| Remote доступ к root helper | Нет TCP listener; local Unix socket; exact peer через `SO_PEERCRED` |
| Компрометация Setup Web | Только typed allowlisted RPC; no shell/path/package/unit/SQL primitives; один mutating operation |
| Request smuggling/schema ambiguity | Bounded frames, duplicate-key rejection, strict versions/types, unknown-field rejection |
| Plan substitution после подтверждения | Immutable plan, RFC 8785 canonicalization и root-side SHA-256 recomputation |
| Provider/package injection | Root-side exact profile resolver и closed enums/allowlists из trusted release |
| Release substitution | Root-trusted catalog membership, verified manifest/artifact digest; content digest сам не доказывает publisher; future signatures |
| Secret leakage | SecretInputs separation, write-only handling, no logs/DB/argv/URL/browser storage, atomic protected generation |
| Replay setup token/session | 256-bit one-shot token, digest at rest, TTL, attempt cap, strict cookie, CSRF/Origin checks |
| Token/session expiry во время install | Durable operation независима от browser auth lifecycle; expiry не отменяет и не прерывает active stages |
| Concurrent installers | Global mutating-operation lock и durable ownership/state |
| Crash между mutations | Durable before/after journal, inspect-first recovery, explicit PARTIAL handling |
| Provisioning до durable secrets | Checkpoint сам является protected host write; полная fsync+atomic config generation и durable receipt обязательны до любой provisioning mutation вне checkpoint |
| Подмена bootstrap | Только bundled/pinned artifact либо fixed official HTTPS origin с manifest/digest verification; no moving `curl | sh`, branch, URL или arbitrary packages от user |
| Захват чужого host resource | `FOREIGN` fail closed; no automatic merge/rewrite/ownership repair |
| Distro derivative mistaken for supported | Exact profile match; `ID_LIKE` diagnostics only; derivatives fail closed |
| Distro upgrade drift | `installed_profile_mismatch`; mutations blocked до compatibility path |
| Setup identity reaches production data/secrets | Нет production groups/env/generations/DB credentials/Bot Control/release filesystem; только typed RPC |
| SSRF через validation | Только fixed Discord API и trusted catalog origins; никаких user-controlled scheme/host/URL/server fetch в helper |
| Ложная rollback гарантия | Stage-specific recovery; no global rollback; DB migration boundary documented separately |

Дополнительно сохраняются существующие invariants D2.12/D2.13: root-owned
trusted deployment assets; separate `kanami`/`kanami-web` env и runtime;
least-privilege PostgreSQL ACL; unconditional loopback Bot Control; fail-closed
partial/contradictory state; managed Caddy foreign-state checks; direct local
health до public completion; no automatic firewall или Discord Developer Portal
mutation; package first-start protection; ownership/symlink/trust checks.

## Rejected alternatives

1. **Продолжать расширять D2.13 Bash wizard как target UX.** Отклонено: нет
   явного least-privilege Web/root protocol и durable state foundation.
2. **Запустить Setup Web как root.** Отклонено: browser-facing process не должен
   владеть host privileges.
3. **HTTP/TCP root API.** Отклонено: увеличивает remote attack surface и не даёт
   необходимой local peer identity boundary.
4. **Generic command/shell/file/package/systemd/SQL RPC.** Отклонено: превращает
   компрометацию Web process в произвольное выполнение root-команд.
5. **Передавать package/unit/path/argv/URL через InstallPlan.** Отклонено:
   execution details принадлежат trusted provider/release.
6. **Сохранять secrets в plan или durable state.** Отклонено: state и
   diagnostics должны быть безопасны для последующего чтения.
7. **Автоматически продолжать после crash по status `RUNNING`.** Отклонено:
   сначала требуется inspection фактического resource state.
8. **Глобальный rollback.** Отклонено: package, filesystem, service, release и
   PostgreSQL migration имеют разные guarantees.
9. **Выбирать distro по `ID_LIKE` или поддерживать все derivatives.** Отклонено:
   provider выбирается только exact accepted profile.
10. **Универсальная proxy/provider plugin framework.** Отклонено: закрытые
    contracts и enums уменьшают privileged surface.
11. **Автоматически merge/rewrite foreign proxy.** Отклонено: foreign state
    требует оператора и fail-closed результата.
12. **Обещать trusted HTTPS на private IP.** Отклонено: первый MVP ограничен
    public DNS ACME; иные TLS modes требуют отдельных capability/acceptance.
13. **Git branch/path/remote как release identity.** Отклонено: target требует
    immutable content-addressed release из root-trusted catalog; произвольный
    user-selected digest также не является authority.
14. **Core auto-update по умолчанию.** Отклонено: default остаётся
    notify/manual с явным backup/update workflow.
15. **Bootstrap как второй production provisioner.** Отклонено: privileged
    bootstrap ограничен fixed-input setup-plane bring-up и durable receipt; все
    дальнейшие production mutations принадлежат `kanami-installerd`.
16. **HTTP wizard до выбора TLS hostname.** Отклонено: hostname известен и
    validated до Setup Web, а первый browser request идёт по trusted HTTPS.

## Transitional compatibility

До feature parity новый flow сосуществует с D2.12/D2.13. Сохраняются:

- отдельные `kanami` и `kanami-web` identities, env и runtime;
- least-privilege PostgreSQL ACL;
- root-owned trusted deployment assets;
- loopback-only Bot Control;
- fail-closed partial/contradictory state;
- managed Caddy foreign-state checks;
- local direct health до enable/public completion;
- отсутствие automatic firewall и Discord Developer Portal mutations;
- отсутствие global rollback claims;
- package first-start protection и trust/symlink/ownership checks.

Планируемый refactor:

| Transitional mechanism | Target mechanism |
| --- | --- |
| Bash orchestration | Typed inspect/apply/verify handlers |
| Per-file pairing replacements | Atomic protected config generation |
| Root-owned Git checkout | Verified immutable ReleaseProvider |
| `git pull --ff-only` updater | State-aware release workflow |
| Manual partial-state diagnosis | Durable journal и stage-specific recovery |

Migration существующей D2.12/D2.13 installation в target model пока не
определена. Новый helper MUST NOT предполагать ownership существующих resources
без inspection. Transitional scripts не удаляются и не переписываются в D2.14.

## Consequences

Положительные последствия:

- browser-facing setup отделён от root privileges;
- privileged surface ограничена typed domain operations;
- один canonical plan и digest связывают preview, confirmation, secrets и
  durable state;
- platform support становится доказуемой exact matrix, а не семейством
  предположений;
- crash/reboot recovery проектируется до опасных proxy/provisioning mutations;
- release lifecycle перестаёт зависеть от mutable Git coordinates;
- существующие security invariants сохраняются до parity.

Стоимость решения:

- новый flow требует нескольких этапов до полного provisioning;
- необходимы protocol/schema compatibility и durable state migrations;
- Debian и будущий Ubuntu provider требуют независимых acceptance suites;
- existing installations нуждаются в отдельном migration design;
- release catalog, verification и backup gate становятся обязательной
  инфраструктурой до полноценного update UX.

## D2.14 acceptance gate

D2.14 принят только если одновременно выполнено следующее:

- изменена только документация, создан этот отдельный ADR;
- `ARCHITECTURE.md` явно разделяет transitional и target deployment;
- `STATUS.md` показывает D2.14 активным, а D2.13 — superseded transitional;
- exact Debian profile записан; Ubuntu не заявлен supported до exact acceptance;
- derivatives, unknown distro, non-systemd и unsupported architectures fail
  closed;
- `InstallPlan` и `SecretInputs` разделены;
- checkpoint сам является protected host write, а полная protected config
  generation durable до любой provisioning mutation вне него;
- privileged bootstrap exception ограничен только setup-plane bring-up;
- generic privileged execution API явно запрещён;
- durable state предшествует Managed Caddy production mutation;
- нет обещаний private-IP browser-trusted HTTPS или global rollback;
- core auto-update default выключен;
- target release boundary использует только catalog-authorized `release_id`, а
  не Git path/ref/remote, arbitrary URL или self-asserted digest;
- public setup hostname известен до wizard, а token/session expiry не отменяет
  durable operation;
- Setup Web изолирован от production groups, secrets, PostgreSQL, Bot Control,
  release filesystem и privileged subprocesses;
- fixed Discord validation endpoints и обязательные Setup HTTP security headers
  зафиксированы;
- transitional scripts сохранены;
- локально проверяемые Markdown links корректны;
- `git diff --check` проходит;
- отсутствуют изменения `src/`, `scripts/`, `deploy/`, `tests/`, `migrations/`,
  `pyproject.toml` и `uv.lock`.

## Roadmap D2.15–D2.23

| Этап | Scope и gate |
| --- | --- |
| D2.15 | Read-only `kanami-installerd`: protocol/version, `preflight`, `validate_plan`, `status`, Unix socket, `SO_PEERCRED`, Debian 13 inspect-only provider; zero provisioning mutation. |
| D2.16 | Minimal fixed-input bootstrap/setup plane: `kanami-setup`, durable bootstrap receipt, installer socket/service, pre-known public hostname, temporary Managed Caddy HTTPS route, one-time token, session, CSRF, TTL и safe teardown; без Kanami production provisioning. |
| D2.17 | Russian Web Wizard: Discord token validation, guild selection, OAuth, OWNER, mini-wiki, draft plan и immutable preview/digest. |
| D2.18 | Durable operation state: global lock, stage journal, resource inspection, polling, crash/reboot recovery и `WAITING_FOR_SECRETS`. |
| D2.19 | Managed Caddy provider: inspect/render/apply/verify, public DNS automatic HTTPS и foreign proxy fail closed. |
| D2.20 | Full provisioning: packages, users, verified release, config, PostgreSQL, migrations, ACL, systemd, proxy, verification и Debian provider; только конкретная exact Ubuntu profile с новым provider/candidate может перейти из `unsupported` в `acceptance_candidate`. |
| D2.21 | `sudo kanami setup`: active/recoverable operation получает новую auth session к той же operation; terminal operation при изменении configuration создаёт новую mutating operation с новым immutable plan/digest/confirmation. Также: config generations, status, doctor, diagnostics и safe TTL teardown. |
| D2.22 | Release/update/backup foundation: root-trusted immutable release catalog, catalog-authorized `release_id`, manifest/artifact verification, atomic current pointer, manual update, DB backup gate, external proxy contract и future signatures. |
| D2.23 | Final clean-VM acceptance: Debian exact profile и одна exact Ubuntu LTS amd64 profile; one-command HTTPS setup, wizard, install, reboot/resume, reopen, manual update и injected failure/recovery tests. Ubuntu становится supported только после PASS. |
