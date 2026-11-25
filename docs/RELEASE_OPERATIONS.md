# Выпуски Nook · инструкция владельцу

## Текущая серия

Изменения PR №7 и №8 вошли в `main`. Следующие исправления проходят CI
в PR №9. Перед выпуском проверь задания Python 3.11 и 3.12 для commit,
который войдёт в артефакт.

Опубликованный `v0.1.2-alpha.1` относится к commit `beef3a4`.
Клиент Nook скачал ZIP и успешно проверил SHA-256 и Sigstore-аттестацию.
Проверка на отдельном VPS и последующие ступени ещё предстоят.

В PR №9 подготовлен `v0.1.2-nightly.20261125`. После проверки и разрешения
на публикацию он станет первым звеном для `v0.1.2-alpha.2`, затем Beta и RC
с теми же файлами программы. Опубликованный `v0.1.2-alpha.1` остаётся доступен.

В пределах серии более высокая ступень имеет приоритет. Поэтому после
публикации Nightly на время теста выбери канал **Nightly**, затем
**Выбрать версию / закрепить → 0.1.2-nightly.20261125**. Проверь
закреплённую версию и отдельно подтверди установку. Сними закрепление,
когда решишь перейти на следующий выпуск.

[PR №9](https://github.com/Sppqq/nook/pull/9) ·
[Alpha](https://github.com/Sppqq/nook/releases/tag/v0.1.2-alpha.1)

## Rulesets и релизные окружения

- Settings → Rules → Rulesets → New branch ruleset. Создай активное
  правило для `main` и `dev`. Требуй PR, независимое ревью, успешные
  проверки обеих версий Python. Запрети обычному пользователю удалять
  ветки и делать force-push. Имена требуемых проверок выбери после
  первого успешного запуска.
- Settings → Rules → Rulesets → New tag ruleset. Для тегов `v*`
  ограничь создание ролью выпускающего и запрети обычное перемещение
  или удаление тегов.
- Settings → Environments. Создай `release-prerelease` и
  `release-stable` и разреши развёртывание только из `main`. Для
  Stable включи Required reviewers и Prevent self-review. Секрет
  `STABLE_RELEASE_ENABLED=true` добавляй только в `release-stable`,
  когда пройдены критерии выпуска. До этого Stable остаётся закрыт.

## Проверка архива и обновления

Подготовка выпуска проверяет именно ZIP кандидата. На Linux user systemd
она прогоняет переход с `v0.1.0`, старую кнопку `v0.1.1`, обновление
зависимостей и откат. Telegram и источники загрузки в этих сценариях
заменены тестовыми транспортами. Отдельная проверка с реальными сервисами
описана ниже.

После слияния в `main` запусти **Verified release** с Publish off.
Проверь артефакты подготовки и SHA-256. После разрешения выпуска создай
тег для следующего согласованного номера на проверенном commit и
запусти тот же workflow с этим тегом и Publish on. На чистом Ubuntu или Debian проверь установку с
`--channel alpha --version 0.1.2-alpha.1`, переключение канала в
Telegram на отдельном тестовом боте и отказ при подмене ZIP или bundle.
Для проверки происхождения `gh` должен поддерживать `attestation verify`
с флагами `--bundle`, `--signer-workflow`, `--source-ref` и
`--deny-self-hosted-runners`. Старый CLI получит отдельное сообщение об
обновлении, проверка без поддержки этих флагов не запускается.

Если после Alpha, Beta или RC обнаружена ошибка, исправление идёт
новым Nightly в той же серии. Затем выпусти следующие номера Alpha,
Beta и RC с указанием предыдущего проверенного релиза каждой ступени.
Так исправленный снимок проходит все проверки заново, а опубликованные
теги и артефакты остаются неизменными. Для Stable нужны совпадающий RC,
пройденные проверки установки, обновления и отката на отдельных
инстансах и одобрение защищённого окружения.

## Live acceptance checklist

Use a separate Linux VPS, a disposable Telegram bot and two disposable
Codex accounts. Record the candidate version, commit, archive checksum,
OS and start and finish times. Keep the test data and keep credentials
out of the evidence.

1. Install the exact public v0.1.0. Exercise the bot in Telegram and seed
   a test schedule, conversation history, owner settings and a legacy
   module. Apply the attested bootstrap documented in [Updates](UPDATES.md).
   Check those records and the owner session after the new service starts.
2. Open the official-channel preview in Telegram and verify that legacy
   main remains selected. Confirm the channel and check that the automatic
   update policy still has its original value. Repeat the upgrade with a
   forced candidate startup failure, then verify the old bot responds.
3. Opt in to a supported legacy function's isolated version. Compare its
   outputs with the original and roll it back. Verify its data survives
   both transitions.
4. Exercise real Codex quota exhaustion with an eligible alternate account,
   token refresh, device sign-in cancellation and session recovery. Use a
   disposable action ledger to detect duplicate side effects.
5. Run the normal test workload through at least one full quota-reset window
   and an intentional service restart. Record CPU, memory, disk growth,
   missed jobs, duplicate actions and recovery time. Attach the independent
   security review separately from CI and local maintainer results.
