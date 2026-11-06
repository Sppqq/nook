#!/usr/bin/env bash
# Необязательно. Запуск ПОСЛЕ install.sh: sudo bash setup/install-root.sh
set -euo pipefail
[[ $EUID -eq 0 && -n ${SUDO_USER:-} && $SUDO_USER != root ]] || { echo 'Запусти от обычного пользователя через sudo.'; exit 1; }
D=$(dirname "$(readlink -f "$0")")
[[ $(id -u "$SUDO_USER") -eq __NOOK_UID__ ]] || { echo 'Сначала выполни основной установщик под владельцем бота.'; exit 1; }
[[ ! -e /etc/sudoers.d/nook && ! -e /usr/local/sbin/nook-root ]] || { echo 'Root-помощник уже существует; ничего не перезаписано.'; exit 1; }
echo 'Будет установлен root-помощник и разрешено обслуживание служб/apt через sudo без пароля.'
echo 'Остальные root-команды выполняются после одобрения в Telegram. Установи только на своём сервере.'
read -r -p 'Установить? [yes/NO] ' answer
[[ $answer == yes ]] || exit 0
visudo -cf "$D/sudoers-nook"
install -m 755 -o root -g root "$D/nook-root" /usr/local/sbin/nook-root
install -m 440 -o root -g root "$D/sudoers-nook" /etc/sudoers.d/nook
touch /var/log/nook-root.log
chmod 600 /var/log/nook-root.log
install -d -m 700 -o "$SUDO_USER" -g "$(id -gn "$SUDO_USER")" "$D/../spool/root"
echo 'Root-помощник установлен.'
