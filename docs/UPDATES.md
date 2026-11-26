# Safe source updates / Обновление без переустановки

## По-русски

### Как обновить существующего бота

- **Есть меню «Обновления и резервные копии».** Открой **Ещё → Моды →
  Обновления и резервные копии → Проверить обновления**. Проверь версию и
  нажми **Установить с резервной копией**. Версия выбирается из твоего канала
  и с учётом закрепления. Если канал ещё не выбран, можно сохранить режим
  `main` и обновляться через **Обновить Nook**.
- **Исходная v0.1.1.** Открой **Ещё → Моды → Обновить Nook**. Эта кнопка
  загружает совместимый код из `main`. Если появится сообщение о
  несовместимых зависимостях, используй подписанный переход по SSH ниже.
- **Исходная v0.1.0.** В ней ещё нет кнопки. Один раз выполни подписанный
  переход по SSH по инструкции ниже. Дальше обновляйся через Telegram.
- **PM2, Docker и частный Домовой.** Сначала нужна миграция конкретной
  установки с проверкой её службы, данных и местных доработок.

Актуальные версии видны в [GitHub Releases](https://github.com/Sppqq/nook/releases).
Публикация кандидата и выбор канала владельцем происходят отдельно.

Открой **Ещё → Моды → Обновления и резервные копии** для выбора канала, просмотра доступных релизов, закрепления версии, паузы и настроек уведомлений. **Ещё → Моды → Обновить Nook** показывает проверенный план обновления. При первой установке Nook загружает последний допустимый опубликованный релиз выбранного канала (по умолчанию Stable). `bootstrap.sh --version X` устанавливает и закрепляет опубликованную версию `X`, а `--dev` явно выбирает ветку разработки. У старых установок без сохранённого канала режим `main` сохраняется до выбора владельца.

Доступны Stable, Release Candidate, Beta, Alpha и Nightly. Stable принимает только стабильные версии; менее стабильные каналы включают все более стабильные версии. При downgrade Nook показывает отдельное предупреждение и требует дополнительного подтверждения. Выбор канала сам по себе ничего не устанавливает.

Не запускай установщик повторно для обновления. Он предназначен для новой установки и отказывается перезаписывать настроенную папку.

### Что сохраняется

Обновляются только разрешённые пути исходников. Не заменяются `config.json`, `secrets.env`, `state.json`, `memory/`, `spool/` с перепиской и заданиями, `keys/`, `nodes.json`, `ssh_config`, файлы провайдеров в `.codex/`, локальный `AGENTS.md` и служба systemd. Python-окружение меняется только при поддерживаемом обновлении зависимостей. Шаблоны из `templates/runtime/` повторно не разворачиваются. Свои моды и навыки сохраняются; если новый файл проекта столкнётся с твоим — обновление отменится.

Установщик записывает хеши установленных исходников после подстановки местных путей. Если и ты, и GitHub изменили один файл, обновлятор откажется писать поверх него. Местная правка сохраняется и тогда, когда на GitHub этот файл не менялся. Автоматического слияния, `git reset`, очистки папки и переустановки с новыми настройками нет.

### Порядок обновления

1. В старом режиме загружается конкретный коммит `main`. Для выбранного канала Nook проверяет релиз, тег, SHA-256, архив и Sigstore-аттестацию официального workflow. Для проверки происхождения установи актуальный GitHub CLI (`gh`). Вход в GitHub не требуется.
2. Несовместимый `update_api` требует отдельной миграции. При подтверждённом переходе на старый релиз удаляются только неизменённые файлы программы из установочного эталона. Конфликт с местной правкой останавливает переход.
3. Подтверждённая версия ждёт завершения активных задач и очереди. Проверка действительна час; потом её нужно повторить.
4. Отдельный пользовательский процесс systemd останавливает именно `nook.service` этой установки. Блокировка не позволяет менять исходники, пока новый бот с этой блокировкой работает.
5. До первой записи сохраняются старые файлы, права и журнал операции в `spool/updates/backups/<номер>`. Меняются только файлы программы; установщик не запускается.
6. Бот запускается. Обновлятор ждёт успешного обращения бота к Telegram, загрузки модов без ошибок, свежего сигнала от правильного процесса/версии и 20 секунд устойчивой работы. При неудаче восстанавливает старый код и запускает прежнюю службу. Результат приходит в Telegram.

Проверка релиза связывает архив с подписанным workflow, версией и commit
тега. Автообновления по умолчанию выключены. Владелец может выбрать
уведомления, установку после подтверждения или автоустановку новой версии.
Последняя ждёт завершения задач и использует те же проверки и откат. Переход
на старую версию всегда подтверждается отдельно. При первой установке режим
можно выбрать флагом `--auto-updates off|notify|confirm|install`.

Автоматический перезапуск поддерживает установку через пользовательскую `nook.service`. PM2, контейнеры, вручную запущенные боты и исходный частный Домовой требуют отдельной адаптации. Нельзя подменять их публичным Nook поверх неизвестной структуры.

### Смена зависимостей через Telegram

После получения нового обновлятора выбери канал официальных релизов и
подтверди обновление обычной кнопкой. Подписанный релиз должен поддерживать
смену окружения, а служба — запускать `bot.py` из штатной папки `.venv`.
Локальные правки списков зависимостей и защиты запуска, а также пакеты из
VCS требуют ручного согласования. Версии пользовательских пакетов из
индекса сохраняются. Nook строит и проверяет окружение до остановки службы.
Проверенный полный снимок сохраняется после остановки в
`.<имя-инстанса>.nook-update-<номер>/complete-backup` рядом с инстансом.

Обновлённый бот ждёт проверки перед приёмом задач. При сбое запуска либо
обрыве транзакции защита возвращает прежние код и окружение. Файлы владельца
и внешние данные провайдеров остаются на прежних путях. Полный снимок доступен
для отдельного восстановления данных. Переход на старый релиз со сменой
зависимостей требует отдельной миграции.

### Переход со старых версий

В `v0.1.1` можно воспользоваться кнопкой **Ещё → Моды → Обновить Nook**,
когда совместимая версия появится в `main`. Старая кнопка обновляет код при
совпадающих зависимостях. После проверки запуска Nook сохраняет режим
`main`. Канал официальных релизов выбирает владелец в настройках обновлений.

Для `v0.1.0`, где кнопки ещё нет, и для смены зависимостей через исходный
обновлятор `v0.1.1` используй миграцию из SSH. Она поддерживает точные публичные версии
`v0.1.0` и `v0.1.1`.

В интерактивном SSH под владельцем службы дождись завершения задач и отмени
запросы старого обновлятора. Сохрани официальный загрузчик в отдельную
временную папку. Укажи фактический абсолютный путь к установленному боту и
канал. Загрузчик выберет новейший подходящий подписанный релиз этого
канала. Например, для штатной папки `~/nook` и канала Alpha:

```bash
stage="$(mktemp -d)"
curl -fsSL https://raw.githubusercontent.com/Sppqq/nook/main/bootstrap.sh -o "$stage/bootstrap.sh" &&
  bash "$stage/bootstrap.sh" --migrate --home "$HOME/nook" --channel alpha --yes
```

Чтобы выбрать конкретный релиз, замени `--channel alpha` на `--version X`.
Эта команда выбирает цель только для миграции. Для следующих обновлений
выбери канал в Telegram.

Нужны пользовательский systemd, Python 3.11+ с `venv`, GitHub CLI с
поддержкой Sigstore и файловая система Linux с атомарным обменом каталогами.
Переход проверяет подпись релиза и сверяет установленный код с историческим
тегом. При конфликте с местными правками он останавливается. Зависимости
устанавливаются в отдельную среду до остановки службы. Полный снимок
создаётся после остановки, включая последние записи в пользовательские
файлы. Внешние каталоги провайдеров в снимок не входят.

После переключения новая версия ждёт окончания проверки запуска перед
приёмом задач из Telegram и запуском фоновой работы. При сбое переход
возвращает прежнюю папку. Если она недоступна, восстановление разворачивает
проверенную полную копию в отдельный каталог. Копия и журнал остаются
рядом с инстансом. Если процесс
прервался после остановки, запусти команду из соседнего каталога с сохранённым
проверенным супервизором `<рабочая-папка>/target-release/source/legacy_migration.py`.
Рабочая папка называется `.<имя-инстанса>.migration-…`, а её путь записан в
`.<имя-инстанса>.nook-migration.json`.

```bash
python3 /path/to/target-release/source/legacy_migration.py recover --home "$HOME/nook"
```

После успешного перехода открой **Ещё → Моды → Обновления и резервные копии**
в Telegram. Nook продолжает отслеживать `main`, пока ты не подтвердишь
официальный канал. Просмотр канала и его подтверждение отделены от установки
релиза. Автообновления управляются отдельно.

### Терминал и восстановление

Из установленной папки:

```bash
bin/nook-update check
# Подставь id из результата проверки; это явное согласие на эту версию:
bin/nook-update request <id> --yes
# Для явного перехода на старую версию добавь --allow-downgrade.
bin/nook-update status
bin/nook-backup create --offline
bin/nook-backup verify spool/backups/nook-backup-....tar.gz
```

`bin/nook-backup` архивирует исходники, память, задания, настройки и локальные модификации. Из Telegram Nook резервирует очереди пользовательских и фоновых агентских задач. Копия создаётся под блокировкой обновления. SQLite копируется через штатный Backup API с учётом подтверждённых транзакций в WAL. Если другие данные меняются при чтении, копия отклоняется. Для согласованности между несколькими независимыми базами и внешними процессами останови всех писателей и используй `create --offline`.

Внешние данные провайдеров включаются только с `--include-providers`. Реестр и сохранённые входы пула Codex входят в эту опцию. Временные каталоги пула создаются заново. Известные файлы Codex и Antigravity попадают под `provider-home/` и восстанавливаются в новый каталог. Архив и приватные файлы остаются доступными только владельцу. Для шифрования установи `age` и используй `create --recipient PUBLIC_KEY --identity KEY_FILE`, а для проверки и восстановления передавай `--identity KEY_FILE`. Ключ держи вне инстанса и отдельно от архивов.

`--max-files`, `--max-file-mib`, `--max-total-mib` и `--max-archive-mib` настраивают пределы при создании, проверке и восстановлении. `create --keep N` удаляет старые архивы того же формата только после сохранения и проверки новой копии. По умолчанию копии не удаляются. Для пробного восстановления используй новый путь:

```bash
bin/nook-backup restore spool/backups/nook-backup-....tar.gz --to "$HOME/nook-restore-test"
```

Восстановление не перезаписывает существующую установку и не устанавливает сохранённые provider credentials в домашнюю папку. Провайдерские файлы остаются внутри `provider-home/` восстановленного каталога для ручной проверки и переноса владельцем. Для запуска копии заново подготовь виртуальное окружение и проверь конфигурацию; для production-cutover сначала создай отдельную копию текущей установки и спланируй переключение вручную. `.venv` намеренно не входит в архив, так как зависит от платформы.

Новые копии создаются только после окончания или отката транзакции
обновления. Очередь установки и временные загрузки в архив не входят.
Если архив сделан до переработки путей, проверь унаследованные абсолютные
пути в модах, настройках и подключениях к соседним сервисам.

Запрос подхватит включённый мод `self_update`. Для ручного отката последнего обновления сначала останови службу, затем восстанови код и запусти бот:

```bash
systemctl --user stop nook.service
bin/nook-update rollback-offline --yes
systemctl --user start nook.service
```

`apply-offline <id> --yes` — отдельный режим для администратора при остановленном боте; он не запускает службу и не проверяет её здоровье. Последующие местные правки блокируют откат, чтобы не потерять их. Резервные копии автоматически не удаляются.

При запуске Nook проверяет журнал и восстанавливает предыдущий код после
обрыва обновления. Для первого перехода через кнопку `v0.1.1` поддерживается
сохранённый обновлятор этой версии. Существующие unit-файлы сохраняются.
Если повреждён журнал или ошибка проявилась после проверки запуска,
воспользуйся офлайн-откатом и проверенным архивом.

## English

### Upgrade an existing bot

- **Updates and backups is available.** Open **More → Mods → Updates and
  backups → Check for updates**, review the version, then select **Install
  with backup**. Nook follows the selected channel and version pin. If you
  still follow `main`, use **Update Nook** to retain that mode.
- **Original v0.1.1.** Use **More → Mods → Обновить Nook** for compatible
  code from `main`. If the original updater reports incompatible
  dependencies, use the signed SSH migration.
- **Original v0.1.0.** Use the one-time signed SSH migration. Its original
  Telegram menu predates the updater.
- **PM2, Docker and private Domovoy builds.** These need an installation-specific
  migration that accounts for the service, data and local changes.

The SSH migration command above selects the newest eligible signed
release on Alpha. Run it in an interactive SSH session under the service
owner and substitute the actual installation directory. To choose an exact
version from [GitHub Releases](https://github.com/Sppqq/nook/releases),
replace `--channel alpha` with `--version X`. The migration leaves your
update settings intact. Choose a channel in Telegram for future updates.

Open **More → Mods → Updates and backups** to select a channel, review releases, pin a version and configure checks. The installer fetches the newest eligible published release for the selected channel. `bootstrap.sh --version X` installs and pins exact published version `X`. Use `--dev` for a development installation. Release installs and updates require a current GitHub CLI (`gh`) for Sigstore verification without login. Confirm updates separately from the installer.

The updater plans source changes against the installed hashes. Local
modifications and custom files are preserved. A conflict stops the update.
Update API changes require separate migration. An explicitly approved
downgrade can remove unchanged managed source paths. An installed instance
with the modern updater can update dependencies from a compatible signed
release after its owner selects a release channel.

Configuration, secrets, state, memory, conversations, jobs, SSH keys/nodes, provider configuration, `AGENTS.md`, and systemd configuration are preserved. The virtual environment changes only for a supported dependency update. Runtime templates are never reseeded. Custom mods and skills are preserved or cause an explicit conflict. The installer records hashes after rendering local paths.

Once confirmed, the enabled `self_update` mod waits for active and queued agent work to finish and reserves both agent lanes. An independent user-systemd process stops this installation's `nook.service`, backs up every affected source file and its permissions, writes a persistent transaction journal, applies the source changes, and restarts the bot. The bot and updater share a process lock. The check expires after one hour.

The supervisor requires successful bot startup with Telegram, no failed
mods, matching fresh process/version health signals, and 20 seconds of
stable operation. Failed startup triggers rollback and restarts the previous
service. New systemd units run a pre-start guard that invokes the frozen
updater after an interrupted transaction. Existing unit files stay intact.
Automatic updates are off by default. An owner can choose notification,
confirmation or unattended upgrade. Downgrades always require approval.

Automatic service updates support the installer's user `nook.service`. PM2,
containers, manual launches and the original private Domovoy need separate
adaptation.

Once compatible code reaches `main`, v0.1.1 can use its existing Telegram
update button if dependencies match. Nook keeps using `main` until the
owner selects a release channel. Use the signed-release SSH migration for
v0.1.0 or dependency changes with the original v0.1.1 updater. The current
updater checks the new environment while the old service runs and verifies a
full snapshot after stopping it. Failed startup or interruption restores
both the original source and environment. Runtime files and external
provider stores stay in place. The snapshot supports manual data recovery.

After migration, confirm an official channel in Telegram to leave legacy main
mode. Previewing a channel does not save it. Choosing a channel, installing
a release and enabling automatic updates are separate actions.

From the installed directory, use `bin/nook-update check`, then `bin/nook-update request <id> --yes`, and `bin/nook-update status`. Stop the service and external writers for an offline instance snapshot. Online backup from Telegram reserves the agent lanes, uses SQLite's Backup API, and fails if other captured files change. External provider data is opt-in via `--include-providers` and restores under `provider-home/`. Use age recipient and identity options to encrypt and verify backups, `--keep N` to rotate after verification, and the `--max-*` options to support larger instances. For manual recovery, stop `nook.service`, run `bin/nook-update rollback-offline --yes`, and start it again. `apply-offline <id> --yes` is for administrators with a stopped bot and does not perform startup health checks. Rollback refuses to overwrite edits made after the update.

The journal and source backups can recover interrupted code changes. Keep a
separate instance backup for restoring runtime data. The initial migration
from a legacy updater can require manual offline recovery.

Channel policy and release workflow: [RELEASE_POLICY.md](../RELEASE_POLICY.md). The verified workflow checks the tag and tests, then attaches the Sigstore bundle to the release. Promotion checks the published predecessor and compares application trees.

## Maintainer compatibility contract

`MANIFEST.json` uses `format: 1` and `update_api: 1`. Increment `update_api` when a release requires runtime data/configuration changes or a different update protocol. The current updater refuses other values. Do not add migrations with import/startup side effects under the same API. Targets advertising `environment_update: 1` support the provisional startup gate. Dependency changes require a compatible signed upgrade and a clean local dependency baseline. Development-branch dependency changes and source deletions during an upgrade require separate migration.
