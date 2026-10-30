#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPOSITORY='Kinbas-validator/wbtc-trd-bot'
source_ref='main'
chat_id=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --chat-id) [[ $# -ge 2 ]] || exit 2; chat_id="$2"; shift 2 ;;
        --ref) [[ $# -ge 2 ]] || exit 2; source_ref="$2"; shift 2 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done
[[ "$source_ref" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'Некорректный ref'; exit 2; }
[[ -z "$chat_id" || "$chat_id" =~ ^-?[0-9]+$ ]] || { echo 'Некорректный chat ID'; exit 2; }
if [[ $EUID -ne 0 ]]; then
    echo 'Запустите команду установки через sudo bash (или от root).' >&2
    exit 1
fi
if [[ ! -r /etc/os-release ]]; then
    echo 'Требуется Ubuntu 24.04.' >&2
    exit 1
fi
. /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
    echo 'Этот установщик рассчитан на Ubuntu 24.04.' >&2
    exit 1
fi
[[ -d /run/systemd/system ]] || { echo 'На сервере должен работать systemd.'; exit 1; }
[[ -r /dev/tty ]] || { echo 'Нужен интерактивный SSH-сеанс для ввода токена.'; exit 1; }

install_work=$(mktemp -d /tmp/trd-install.XXXXXXXX)
trap 'rm -rf -- "$install_work"' EXIT
trap 'echo "Установка не завершена. Исправьте ошибку выше и повторите команду." >&2' ERR

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 ca-certificates curl
base_url="https://raw.githubusercontent.com/${REPOSITORY}/${source_ref}"
for name in bot.py configure.py trd-bot.service test_bot.py test_configure.py; do
    curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
        "${base_url}/${name}" -o "${install_work}/${name}"
done
python3 -m unittest discover -s "$install_work" -q
python3 "${install_work}/configure.py" --chat-id "$chat_id"

# Do not disturb an existing running service until downloads and validation pass.
if ! getent passwd trd-bot >/dev/null; then
    useradd --system --user-group --home-dir /var/lib/trd-bot --no-create-home \
        --shell /usr/sbin/nologin trd-bot
fi
systemctl stop trd-bot.service 2>/dev/null || true
install -d -m 755 /opt/trd-bot
install -d -m 700 -o trd-bot -g trd-bot /var/lib/trd-bot
install -m 644 "${install_work}/bot.py" /opt/trd-bot/bot.py
install -m 644 "${install_work}/trd-bot.service" /etc/systemd/system/trd-bot.service
systemctl daemon-reload
systemctl enable --now trd-bot.service
systemctl is-active --quiet trd-bot.service
printf '\nГотово: бот установлен, автозапуск включён.\n'
printf 'Пороги: −1,0%% → −0,5%% → 0%%; проверка каждые 60 секунд.\n'
printf 'Логи: sudo journalctl -u trd-bot -f\n'
printf 'Остановка: sudo systemctl disable --now trd-bot\n'
