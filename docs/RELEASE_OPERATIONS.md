# Выпуски Nook · инструкция владельцу

## PR №7 и Actions

Проверено 24 ноября 2026 года. PR №7 открыт в `Sppqq/nook` из
`feature/road-to-10` в `dev`. GitHub показывает `action_required` и ноль
заданий для запуска PR 45432925443 и запуска push 45432882400. Оба запуска
созданы аккаунтом владельца. Публичный API не раскрывает причину блокировки.

1. Войди в GitHub владельцем и открой каждый запуск. Действуй по баннеру
   GitHub. Если это `Approve workflows to run`, проверь commit и нажми
   кнопку в PR. Если GitHub показывает ограничение Actions или биллинга,
   устрани названное ограничение в Settings → Actions → General или в
   Billing and licensing. Сохрани текст баннера для повторной диагностики,
   если запуск по-прежнему не создаёт ни одного задания.
2. Восстанови авторизацию `gh auth login --hostname github.com --web` и
   проверь `gh auth status`. Для токена с ограниченным доступом дай запись
   `Contents` и `Workflows` только для этого репозитория. Выполни
   `git push origin HEAD:feature/road-to-10` из сохранённой ветки Nook.
   Существующий PR обновится автоматически. При non-fast-forward
   проверь расхождение веток перед повторной попыткой.
3. Проверь Checks у новой вершины PR. Если доступен Re-run all jobs, можно
   также повторить исходный запуск. Нужен фактический запуск заданий,
   включая Python 3.11 и 3.12.

Ссылки: [PR №7](https://github.com/Sppqq/nook/pull/7),
[запуск PR](https://github.com/Sppqq/nook/actions/runs/45432925443),
[запуск push](https://github.com/Sppqq/nook/actions/runs/45432882400),
[настройки Actions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).

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

## Проверка Alpha и обновления

После слияния в `main` запусти **Verified release** с Publish off.
Проверь артефакты подготовки и SHA-256. После разрешения выпуска создай
тег `v0.1.2-alpha.1` на проверенном commit и запусти тот же workflow
с тегом и Publish on. На чистом Ubuntu или Debian проверь установку с
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
