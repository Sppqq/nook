# Safe source updates / Обновление без переустановки

## По-русски

Открой **Ещё → Моды → Обновить Nook**. Помощник проверит GitHub и покажет число файлов программы. Кнопка **Обновить с резервной копией** подтверждает именно проверенную версию. Запасной вход в Telegram — `/update`, он только проверяет обновления, без немедленной установки.

Не запускай установщик повторно для обновления. Он предназначен для новой установки и отказывается перезаписывать настроенную папку.

### Что сохраняется

Обновляются только разрешённые пути исходников. Не заменяются `config.json`, `secrets.env`, `state.json`, `memory/`, `spool/` с перепиской и заданиями, `keys/`, `nodes.json`, `ssh_config`, файлы провайдеров в `.codex/`, локальный `AGENTS.md`, Python-окружение и служба systemd. Шаблоны из `templates/runtime/` повторно не разворачиваются. Свои моды и навыки сохраняются; если новый файл проекта столкнётся с твоим — обновление отменится.

Установщик записывает хеши установленных исходников после подстановки местных путей. Если и ты, и GitHub изменили один файл, обновлятор откажется писать поверх него. Местная правка сохраняется и тогда, когда на GitHub этот файл не менялся. Автоматического слияния, `git reset`, очистки папки и переустановки с новыми настройками нет.

### Порядок обновления

1. Загружается конкретный коммит `Sppqq/nook`, а не меняющаяся ветка во время установки. Проверяются состав архива, пути, хеши манифеста и синтаксис Python. Ссылки в архиве запрещены.
2. Изменения зависимостей, удаление старых исходников или несовместимый `update_api` требуют отдельной миграции. Автоматическое обновление их не выполняет.
3. Подтверждённая версия ждёт завершения активных задач и очереди. Проверка действительна час; потом её нужно повторить.
4. Отдельный пользовательский процесс systemd останавливает именно `nook.service` этой установки. Блокировка не позволяет менять исходники, пока новый бот с этой блокировкой работает.
5. До первой записи сохраняются старые файлы, права и журнал операции в `spool/updates/backups/<номер>`. Меняются только файлы программы; установщик не запускается.
6. Бот запускается. Обновлятор ждёт успешного обращения бота к Telegram, загрузки модов без ошибок, свежего сигнала от правильного процесса/версии и 20 секунд устойчивой работы. При неудаче восстанавливает старый код и запускает прежнюю службу. Результат приходит в Telegram.

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
```

Запрос подхватит включённый мод `self_update`. Для ручного отката последнего обновления сначала останови службу, затем восстанови код и запусти бот:

```bash
systemctl --user stop nook.service
bin/nook-update rollback-offline --yes
systemctl --user start nook.service
```

`apply-offline <id> --yes` — отдельный режим для администратора при остановленном боте; он не запускает службу и не проверяет её здоровье. Последующие местные правки блокируют откат, чтобы не потерять их. Резервные копии автоматически не удаляются.

Проверка запуска не доказывает исправность всех функций. Поломка после окна проверки, несовместимый сторонний мод, потеря питания или принудительное завершение независимого обновлятора могут потребовать ручного восстановления. Журнал записи позволяет восстановить и частично записанное обновление. Нужны собственные бэкапы; обещания «никогда ничего не сломается» здесь нет.

## English

Open **More → Mods → Обновить Nook**, inspect the proposed update, then confirm it. The optional `/update` Telegram command checks only. Never rerun the installer as an upgrade: configured directories are deliberately refused.

The updater downloads a pinned commit from `Sppqq/nook`, validates archive paths, the complete integrity manifest and Python syntax, and plans source changes against the installed hashes. Local changes are retained; a conflicting upstream change or a new upstream file colliding with a custom file cancels the update. Dependencies, source deletions, and incompatible update API versions require a separate migration.

Configuration, secrets, state, memory, conversations, jobs, SSH keys/nodes, provider configuration, `AGENTS.md`, the virtual environment, and systemd configuration are preserved. Runtime templates are never reseeded. Custom mods and skills are preserved or cause an explicit conflict. The installer records hashes after rendering local paths.

Once confirmed, the enabled `self_update` mod waits for active and queued agent work to finish and reserves both agent lanes. An independent user-systemd process stops this installation's `nook.service`, backs up every affected source file and its permissions, writes a persistent transaction journal, applies the source changes, and restarts the bot. The bot and updater share a process lock. The check expires after one hour.

The supervisor requires successful bot startup with Telegram, no failed mods, matching fresh process/version health signals, and 20 seconds of stable operation. Failed startup triggers source rollback and a restart of the previous service. The result is sent to the owner. Backups live in `spool/updates/backups/<id>` and are not automatically deleted.

Automatic service updates support the installer's user `nook.service`; PM2, containers, manual launches and the original private Domovoy need separate adaptation. Older Nook installations without the updater require a reviewed initial migration. Do not establish a clean baseline from arbitrary local files to bypass conflicts; older raw manifests cannot identify all rendered files as unchanged.

From the installed directory, use `bin/nook-update check`, then `bin/nook-update request <id> --yes`, and `bin/nook-update status`. For manual recovery, stop `nook.service`, run `bin/nook-update rollback-offline --yes`, and start it again. `apply-offline <id> --yes` is for administrators with a stopped bot and does not perform startup health checks. Rollback refuses to overwrite edits made after the update.

This is source rollback, not a guarantee that every feature works. Later failures, third-party incompatibilities, power loss or termination of the independent supervisor can require manual recovery using the persistent journal. Keep independent backups. Downloads trust GitHub and this repository's maintainers over HTTPS; the manifest is an integrity check, not an independent signature.

## Maintainer compatibility contract

`MANIFEST.json` uses `format: 1` and `update_api: 1`. Increment `update_api` when a release requires runtime data/configuration changes or a different update protocol. The current updater refuses other values. Do not add migrations with import/startup side effects under the same API. Dependency changes and source deletions are intentionally refused even under API 1. Test migrations separately before making them available as an automatic update.
