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
2. Изменения зависимостей, удаление старых исходников или несовместимый `update_api` требуют отдельной миграции. Автоматическое обновление их не выполняет.
3. Подтверждённая версия ждёт завершения активных задач и очереди. Проверка действительна час; потом её нужно повторить.
4. Отдельный пользовательский процесс systemd останавливает именно `nook.service` этой установки. Блокировка не позволяет менять исходники, пока новый бот с этой блокировкой работает.
5. До первой записи сохраняются старые файлы, права и журнал операции в `spool/updates/backups/<номер>`. Меняются только файлы программы; установщик не запускается.
6. Бот запускается. Обновлятор ждёт успешного обращения бота к Telegram, загрузки модов без ошибок, свежего сигнала от правильного процесса/версии и 20 секунд устойчивой работы. При неудаче восстанавливает старый код и запускает прежнюю службу. Результат приходит в Telegram.

Проверка релиза связывает архив с подписанным workflow, версией и commit
тега. Автоматические проверки по умолчанию выключены. Доступны уведомления
или показ проверенного плана с подтверждением.

Автоматический перезапуск поддерживает установку через пользовательскую `nook.service`. PM2, контейнеры, вручную запущенные боты и исходный частный Домовой требуют отдельной адаптации. Нельзя подменять их публичным Nook поверх неизвестной структуры.

### Если установлено до появления обновлятора

В старом боте этой кнопки ещё нет. Попроси помощника перенести обновлятор и проверить миграцию исходников. Старый `MANIFEST.json` может служить эталоном только для совпадающих файлов; подставленные установщиком пути и местные правки могут вызвать отказ. Нельзя объявлять текущее содержимое «чистой версией» или повторно разворачивать шаблоны ради обхода конфликта. Сначала резервная копия и просмотр различий.

### Терминал и восстановление

Из установленной папки:

```bash
bin/nook-update check
# Подставь id из результата проверки; это явное согласие на эту версию:
bin/nook-update request <id> --yes
bin/nook-update status
bin/nook-backup create
bin/nook-backup verify spool/backups/nook-backup-....tar.gz
```

`bin/nook-backup` архивирует исходники, конфигурацию, память, задания, GitHub/Mercury-настройки, SSH и локальные модификации. Он также включает только известные внешние данные провайдеров: `~/.codex/auth.json`, `~/.codex/config.toml`, JSONL-сессии Codex и OAuth-токен Antigravity. Они лежат в архиве под `provider-home/`; `.git`, `.venv`, временные каталоги и прежние архивы исключены. Архив содержит секреты и историю разговоров, остаётся локальным и создаётся с правами `0600`. Проверяй его перед копированием. Для пробного восстановления используй новый пустой путь:

```bash
bin/nook-backup restore spool/backups/nook-backup-....tar.gz --to "$HOME/nook-restore-test"
```

Восстановление не перезаписывает существующую установку и не устанавливает сохранённые provider credentials в домашнюю папку. Провайдерские файлы остаются внутри `provider-home/` восстановленного каталога для ручной проверки и переноса владельцем. Для запуска копии заново подготовь виртуальное окружение и проверь конфигурацию; для production-cutover сначала создай отдельную копию текущей установки и спланируй переключение вручную. `.venv` намеренно не входит в архив, так как зависит от платформы.

Запрос подхватит включённый мод `self_update`. Для ручного отката последнего обновления сначала останови службу, затем восстанови код и запусти бот:

```bash
systemctl --user stop nook.service
bin/nook-update rollback-offline --yes
systemctl --user start nook.service
```

`apply-offline <id> --yes` — отдельный режим для администратора при остановленном боте; он не запускает службу и не проверяет её здоровье. Последующие местные правки блокируют откат, чтобы не потерять их. Резервные копии автоматически не удаляются.

Проверка запуска не доказывает исправность всех функций. Поломка после окна проверки, несовместимый сторонний мод, потеря питания или принудительное завершение независимого обновлятора могут потребовать ручного восстановления. Журнал записи позволяет восстановить и частично записанное обновление. Нужны собственные бэкапы; обещания «никогда ничего не сломается» здесь нет.

## English

Open **More → Mods → Обновления и резервные копии** to select a channel, review releases, pin a version and configure checks. The installer fetches the newest eligible published release for the selected channel. `bootstrap.sh --version X` installs and pins exact published version `X`. Use `--dev` for a development installation. Release installs and updates require a current GitHub CLI (`gh`) for Sigstore verification without login. Confirm updates separately from the installer.

The updater downloads a pinned commit from `Sppqq/nook`, validates archive paths, the complete integrity manifest and Python syntax, and plans source changes against the installed hashes. Local changes are retained; a conflicting upstream change or a new upstream file colliding with a custom file cancels the update. Dependencies, source deletions, and incompatible update API versions require a separate migration.

Configuration, secrets, state, memory, conversations, jobs, SSH keys/nodes, provider configuration, `AGENTS.md`, the virtual environment, and systemd configuration are preserved. Runtime templates are never reseeded. Custom mods and skills are preserved or cause an explicit conflict. The installer records hashes after rendering local paths.

Once confirmed, the enabled `self_update` mod waits for active and queued agent work to finish and reserves both agent lanes. An independent user-systemd process stops this installation's `nook.service`, backs up every affected source file and its permissions, writes a persistent transaction journal, applies the source changes, and restarts the bot. The bot and updater share a process lock. The check expires after one hour.

The supervisor requires successful bot startup with Telegram, no failed mods, matching fresh process/version health signals, and 20 seconds of stable operation. Failed startup triggers source rollback and a restart of the previous service. The result is sent to the owner. Backups live in `spool/updates/backups/<id>` and are not automatically deleted.

Automatic service updates support the installer's user `nook.service`; PM2, containers, manual launches and the original private Domovoy need separate adaptation. Older Nook installations without the updater require a reviewed initial migration. Do not establish a clean baseline from arbitrary local files to bypass conflicts; older raw manifests cannot identify all rendered files as unchanged.

From the installed directory, use `bin/nook-update check`, then `bin/nook-update request <id> --yes`, and `bin/nook-update status`. `bin/nook-backup` also captures the selected Codex and Antigravity credential/session files under `provider-home/` in its archive; restore keeps them inside the new destination and never overwrites the active provider home. For manual recovery, stop `nook.service`, run `bin/nook-update rollback-offline --yes`, and start it again. `apply-offline <id> --yes` is for administrators with a stopped bot and does not perform startup health checks. Rollback refuses to overwrite edits made after the update.

This is source rollback, not a guarantee that every feature works. Later failures, third-party incompatibilities, power loss or termination of the independent supervisor can require manual recovery using the persistent journal. Keep independent backups. Downloads trust GitHub and this repository's maintainers over HTTPS; the manifest is an integrity check, not an independent signature.

Channel policy and release workflow: [RELEASE_POLICY.md](../RELEASE_POLICY.md). The release workflow validates tags and tests before publishing `.zip`, `source.json` and `SHA256SUMS`; same-artifact promotion and detached signatures are not yet implemented.

## Maintainer compatibility contract

`MANIFEST.json` uses `format: 1` and `update_api: 1`. Increment `update_api` when a release requires runtime data/configuration changes or a different update protocol. The current updater refuses other values. Do not add migrations with import/startup side effects under the same API. Dependency changes and source deletions are intentionally refused even under API 1. Test migrations separately before making them available as an automatic update.
