# Safe source updates / Обновление без переустановки

## По-русски

Открой **Ещё → Моды → Обновления и резервные копии** для выбора канала, просмотра доступных релизов, закрепления версии, паузы и настроек уведомлений. **Ещё → Моды → Обновить Nook** показывает проверенный план обновления. При первой установке Nook загружает последний допустимый опубликованный релиз выбранного канала (по умолчанию Stable). `bootstrap.sh --version X` устанавливает и закрепляет опубликованную версию `X`, а `--dev` явно выбирает ветку разработки. У старых установок без сохранённого канала режим `main` сохраняется до выбора владельца.

Доступны Stable, Release Candidate, Beta, Alpha и Nightly. Stable принимает только стабильные версии; менее стабильные каналы включают все более стабильные версии. При downgrade Nook показывает отдельное предупреждение и требует дополнительного подтверждения. Выбор канала сам по себе ничего не устанавливает.

Не запускай установщик повторно для обновления. Он предназначен для новой установки и отказывается перезаписывать настроенную папку.

### Что сохраняется

Обновляются только разрешённые пути исходников. Не заменяются `config.json`, `secrets.env`, `state.json`, `memory/`, `spool/` с перепиской и заданиями, `keys/`, `nodes.json`, `ssh_config`, файлы провайдеров в `.codex/`, локальный `AGENTS.md`, Python-окружение и служба systemd. Шаблоны из `templates/runtime/` повторно не разворачиваются. Свои моды и навыки сохраняются; если новый файл проекта столкнётся с твоим — обновление отменится.

Установщик записывает хеши установленных исходников после подстановки местных путей. Если и ты, и GitHub изменили один файл, обновлятор откажется писать поверх него. Местная правка сохраняется и тогда, когда на GitHub этот файл не менялся. Автоматического слияния, `git reset`, очистки папки и переустановки с новыми настройками нет.

### Порядок обновления

1. В старом режиме загружается конкретный коммит `main`. Для выбранного канала Nook проверяет релиз, тег, SHA-256, архив и Sigstore-аттестацию официального workflow. Для проверки происхождения установи актуальный GitHub CLI (`gh`). Вход в GitHub не требуется.
2. Изменения зависимостей и несовместимый `update_api` требуют отдельной миграции. При подтверждённом переходе на старый релиз удаляются только неизменённые файлы программы из установочного эталона. Конфликт с местной правкой останавливает переход.
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

### Если установлено до появления обновлятора

Для публичных установок на точных версиях `v0.1.0` и `v0.1.1` есть
переход из SSH. В `v0.1.0` нет кнопки обновления, а установленная кнопка
`v0.1.1` умеет обновлять только код без смены зависимостей.

На Linux под владельцем пользовательской службы дождись завершения задач и
отмени запросы старого обновлятора. Из каталога вне инстанса запусти
`bootstrap.sh` из доверенного источника, указав **точную опубликованную**
версию с аттестацией Nook:

```bash
bash /path/to/bootstrap.sh --migrate --home "$HOME/nook" --version X.Y.Z --yes
```

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

Новая `nook.service` проверяет журнал перед запуском бота. После прерывания
обновления она запускает сохранённую копию обновлятора и восстанавливает
предыдущий код. Существующие unit-файлы не переписываются. При первой
миграции со старого обновлятора или если изменены файлы журнала, используй
офлайн-откат и проверенный архив. Ошибку, которая проявилась после окна
проверки, можно откатить отдельно.

## English

Open **More → Mods → Обновления и резервные копии** to select a channel, review releases, pin a version and configure checks. The installer fetches the newest eligible published release for the selected channel. `bootstrap.sh --version X` installs and pins exact published version `X`. Use `--dev` for a development installation. Release installs and updates require a current GitHub CLI (`gh`) for Sigstore verification without login. Confirm updates separately from the installer.

The updater plans source changes against the installed hashes. Local
modifications and custom files are preserved. A conflict stops the update.
Dependency and update API changes require separate migration. An explicitly
approved downgrade can remove unchanged managed source paths.

Configuration, secrets, state, memory, conversations, jobs, SSH keys/nodes, provider configuration, `AGENTS.md`, the virtual environment, and systemd configuration are preserved. Runtime templates are never reseeded. Custom mods and skills are preserved or cause an explicit conflict. The installer records hashes after rendering local paths.

Once confirmed, the enabled `self_update` mod waits for active and queued agent work to finish and reserves both agent lanes. An independent user-systemd process stops this installation's `nook.service`, backs up every affected source file and its permissions, writes a persistent transaction journal, applies the source changes, and restarts the bot. The bot and updater share a process lock. The check expires after one hour.

The supervisor requires successful bot startup with Telegram, no failed
mods, matching fresh process/version health signals, and 20 seconds of
stable operation. Failed startup triggers rollback and restarts the previous
service. New systemd units run a pre-start guard that invokes the frozen
updater after an interrupted transaction. Existing unit files stay intact.
Automatic updates are off by default. An owner can choose notification,
confirmation or unattended upgrade. Downgrades always require approval.

Automatic service updates support the installer's user `nook.service`; PM2, containers, manual launches and the original private Domovoy need separate adaptation. Older Nook installations without the updater require a reviewed initial migration. Do not establish a clean baseline from arbitrary local files to bypass conflicts; older raw manifests cannot identify all rendered files as unchanged.

From the installed directory, use `bin/nook-update check`, then `bin/nook-update request <id> --yes`, and `bin/nook-update status`. Stop the service and external writers for an offline instance snapshot. Online backup from Telegram reserves the agent lanes, uses SQLite's Backup API, and fails if other captured files change. External provider data is opt-in via `--include-providers` and restores under `provider-home/`. Use age recipient and identity options to encrypt and verify backups, `--keep N` to rotate after verification, and the `--max-*` options to support larger instances. For manual recovery, stop `nook.service`, run `bin/nook-update rollback-offline --yes`, and start it again. `apply-offline <id> --yes` is for administrators with a stopped bot and does not perform startup health checks. Rollback refuses to overwrite edits made after the update.

The journal and source backups can recover interrupted code changes. Keep a
separate instance backup for restoring runtime data. The initial migration
from a legacy updater can require manual offline recovery.

Channel policy and release workflow: [RELEASE_POLICY.md](../RELEASE_POLICY.md). The release workflow validates tags and tests before publishing `.zip`, `source.json` and `SHA256SUMS`; same-artifact promotion and detached signatures are not yet implemented.

## Maintainer compatibility contract

`MANIFEST.json` uses `format: 1` and `update_api: 1`. Increment `update_api` when a release requires runtime data/configuration changes or a different update protocol. The current updater refuses other values. Do not add migrations with import/startup side effects under the same API. Dependency changes and source deletions are intentionally refused even under API 1. Test migrations separately before making them available as an automatic update.
