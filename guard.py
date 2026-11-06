#!/usr/bin/env python3
"""Охрана нод: снимок «точек закрепления» и признаков взлома, сравнение с эталоном.

Без LLM. Бот вызывает scan() раз в GUARD_INTERVAL, при изменениях пишет хозяину.
CLI для агента:  guard scan [нода|local]   — показать находки, ничего не меняя
                 guard accept [нода|local] — принять текущее состояние как норму
"""
import hashlib
import json
import os
import subprocess
import sys

HOME = os.path.dirname(os.path.abspath(__file__))
BASE_PATH = os.path.join(HOME, "spool", "guard.json")
SSH_CONFIG = os.path.join(HOME, "ssh_config")
NODES_PATH = os.path.join(HOME, "nodes.json")

# порты майнинг-пулов / monerod — исходящие соединения туда = почти наверняка майнер
MINER_PORTS = {3333, 4444, 5555, 7777, 9999, 14433, 14444, 18081, 18089, 45560, 45700}

# одной сессией: каждая строка «раздел<TAB>ключ<TAB>значение»
SCRIPT = r'''
export LC_ALL=C
h() { sha256sum 2>/dev/null | cut -c1-16; }
for f in /etc/crontab /etc/cron.d/* /var/spool/cron/crontabs/* /var/spool/cron/*; do
  [ -f "$f" ] && [ -r "$f" ] && printf 'cron\t%s\t%s\n' "$f" "$(grep -v '^\s*#' "$f" | grep . | h)"
done
[ "$(id -u)" != 0 ] && printf 'cron\tcrontab -l\t%s\n' "$(crontab -l 2>/dev/null | grep -v '^\s*#' | grep . | h)"
for f in /etc/systemd/system/*.service /etc/systemd/system/*.timer /etc/systemd/system/*/*.service \
         /home/*/.config/systemd/user/*.service /root/.config/systemd/user/*.service \
         /home/*/.config/systemd/user/*.timer; do
  [ -f "$f" ] && [ -r "$f" ] && printf 'unit\t%s\t%s\n' "$f" "$(cat "$f" | h)"
done
for f in /root/.ssh/authorized_keys /root/.ssh/authorized_keys2 /home/*/.ssh/authorized_keys; do
  [ -r "$f" ] || continue
  grep -v '^\s*#' "$f" | grep . | while read -r line; do
    printf 'key\t%s\t%s\n' "$f" "$(printf '%s' "$line" | awk '{for(i=1;i<=NF;i++) if ($i ~ /^(ssh-|ecdsa-|sk-)/) {print $(i+1) " " $(i+2); exit}}' | cut -c1-200)"
  done
done
awk -F: '$3==0 {print "uid0\t" $1 "\t" $7}' /etc/passwd
awk -F: '$7 !~ /(nologin|false|sync|shutdown|halt)$/ {print "shell\t" $1 "\t" $7}' /etc/passwd
[ -s /etc/ld.so.preload ] && printf 'preload\t/etc/ld.so.preload\t%s\n' "$(cat /etc/ld.so.preload | tr '\n' ' ' | cut -c1-200)"
ss -Htlnup 2>/dev/null | awk '{
  a=$5; n=split(a,p,":"); port=p[n]; host=substr(a,1,length(a)-length(port)-1);
  if (host ~ /^(127\.|\[::1\]|::1|\[::ffff:127)/) next;
  proc=$0; sub(/.*users:\(\("/,"",proc); sub(/".*/,"",proc); if (proc==$0) proc="?";
  print "port\t" $1 "/" port "\t" proc }' | sort -u
for d in /proc/[0-9]*; do
  pid=${d#/proc/}; exe=$(readlink "$d/exe" 2>/dev/null) || continue
  comm=$(cat "$d/comm" 2>/dev/null); args=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null | cut -c1-120)
  user=$(stat -c %U "$d" 2>/dev/null)
  case "$exe" in
    /usr/*|/bin/*|/sbin/*|/lib*|/opt/*|/snap/*|/nix/*|"/ (deleted)") ;;  # обновлённые пакеты, хелперы ядра
    /tmp/*|/var/tmp/*|/dev/shm/*|/run/shm/*|*" (deleted)") printf 'proc\t%s\t%s pid=%s user=%s args=%s\n' "$exe" "$comm" "$pid" "$user" "$args";;
  esac
  case "$args" in "["*"]"*) printf 'proc\t%s\tмаскировка под поток ядра: %s pid=%s user=%s\n' "$exe" "$args" "$pid" "$user";; esac
done
ss -Htnp state established 2>/dev/null | awk '{ a=$4; n=split(a,p,":"); proc=$0; sub(/.*users:\(\("/,"",proc); sub(/".*/,"",proc); print "conn\t" p[n] "\t" a " " proc }'
for d in /tmp /var/tmp /dev/shm; do
  find "$d" -xdev -maxdepth 3 -type f -perm /111 -size +20k ! -name '*.so*' 2>/dev/null | head -200 | while read -r f; do
    head -c4 "$f" 2>/dev/null | grep -q 'ELF' && printf 'elf\t%s\t%s\n' "$f" "$(stat -c '%s %U' "$f")"
  done
done
for f in /home/*/.config/qBittorrent/qBittorrent.conf /root/.config/qBittorrent/qBittorrent.conf \
         /var/lib/qbittorrent/.config/qBittorrent/qBittorrent.conf /home/*/.config/qBittorrent/*.conf; do
  [ -r "$f" ] && grep -iE '^(OnTorrent[A-Za-z]*\\Program|program)=.+' "$f" | grep -v '=$' | while read -r l; do
    printf 'autorun\t%s\t%s\n' "$f" "$(echo "$l" | cut -c1-200)"; done
done
printf 'now\t%s\t-\n' "$(date '+%Y-%m-%d %H:%M:%S')"
printf 'dpkg\t%s\t-\n' "$(stat -c %Y /var/log/dpkg.log 2>/dev/null || echo 0)"
journalctl -q --no-pager -o short-iso -u ssh -u sshd --since "$since" 2>/dev/null \
  | grep -E 'Accepted (password|publickey|keyboard)' \
  | sed -E 's/.*Accepted ([a-z-]+) for ([^ ]+) from ([^ ]+) port [0-9]+ [a-z0-9]+(: ([A-Za-z0-9-]+) ([^ ]+))?.*/login\t\2@\3\t\1 \5 \6/' | sort -u
for f in /root/.ssh/authorized_keys /root/.ssh/authorized_keys2 /home/*/.ssh/authorized_keys; do
  [ -r "$f" ] && ssh-keygen -lf "$f" 2>/dev/null | awk '{c=""; for (i=3; i<NF; i++) c = c (c ? " " : "") $i; print "kfp\t" $2 "\t" c}'
done
echo 'done	ok	ok'
'''

LABELS = {
    "cron": "cron", "unit": "systemd-юнит", "key": "SSH-ключ", "uid0": "пользователь с UID 0",
    "shell": "пользователь с шеллом", "preload": "ld.so.preload", "port": "открытый порт",
    "elf": "бинарник во временной папке", "autorun": "AutoRun qBittorrent",
}
# что сравнивается с эталоном (новое/изменилось/исчезло)
TRACKED = ("cron", "unit", "key", "uid0", "shell", "preload", "port", "elf", "autorun")
# что тревожно само по себе, без эталона
CRITICAL = ("proc", "preload", "autorun")


def nodes():
    try:
        return json.load(open(NODES_PATH))
    except Exception:
        return []


def run_script(target, since="-15min", timeout=60):
    """Снять снимок. target: имя ноды из ssh_config или 'local'. Возвращает (ok, rows)."""
    script = f"since='{since}'\n" + SCRIPT
    if target == "local":
        cmd = ["bash", "-s"]
    else:
        cmd = ["ssh", "-F", SSH_CONFIG, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, "bash -s"]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False, []
    rows = [tuple(l.split("\t", 2)) for l in p.stdout.splitlines() if l.count("\t") >= 2]
    if not rows or rows[-1][0] != "done":
        return False, []
    return True, rows[:-1]


def snapshot(rows):
    snap = {k: {} for k in TRACKED}
    extra = {"proc": [], "conn": [], "login": [], "now": None, "kfp": {}}
    for sec, key, val in rows:
        if sec in snap:
            snap[sec][key if sec != "key" else f"{key} {val}"] = val
        elif sec == "conn":
            try:
                if int(key) in MINER_PORTS:
                    extra["conn"].append(val)
            except ValueError:
                pass
        elif sec == "now":
            extra["now"] = key
        elif sec == "dpkg":
            extra["dpkg"] = int(key) if key.isdigit() else 0
        elif sec == "kfp":
            extra["kfp"][key] = val
        elif sec in extra:
            extra[sec].append(f"{key} {val}".strip())
    return snap, extra


def load_base():
    try:
        return json.load(open(BASE_PATH))
    except Exception:
        return {}


def save_base(base):
    os.makedirs(os.path.dirname(BASE_PATH), exist_ok=True)
    tmp = BASE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(base, f, ensure_ascii=False, indent=1)
    os.replace(tmp, BASE_PATH)


def diff(old, new):
    """Изменения между эталоном и снимком: список (раздел, что, ключ, значение)."""
    out = []
    for sec in TRACKED:
        a, b = old.get(sec, {}), new.get(sec, {})
        for k in b:
            if k not in a:
                out.append((sec, "new", k, b[k]))
            elif a[k] != b[k]:
                out.append((sec, "changed", k, b[k]))
        for k in a:
            if k not in b and sec not in ("elf", "port"):  # исчезнувшее важно только для замков (ключи, cron…)
                out.append((sec, "gone", k, a[k]))
    return out


GEO = {}


def geo(ip):
    """Страна, город, провайдер по IP (ip-api.com, без ключа). Пусто — не узнали."""
    if ip in GEO:
        return GEO[ip]
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://ip-api.com/json/{ip}?fields=status,country,city,isp,org&lang=ru",
                                    timeout=4) as r:
            d = json.loads(r.read())
        g = ", ".join(x for x in (d.get("country"), d.get("city"), d.get("isp") or d.get("org")) if x) \
            if d.get("status") == "success" else ""
    except Exception:
        g = ""
    GEO[ip] = g
    return g


def parse_login(line, kfp):
    """'root@192.0.2.1 publickey ED25519 SHA256:..' → (user@ip, ip, токен ключа, «как вошли»)."""
    who, _, rest = line.partition(" ")
    method, ktype, fp = (rest.split() + ["", "", ""])[:3]
    ip = who.rsplit("@", 1)[-1]
    if method == "publickey":
        name = kfp.get(fp, "").strip()
        how = (f"ключом «{name}»" if name and name != "no comment" else f"ключом без имени `{fp[7:19]}…`") \
            + (f" ({ktype})" if ktype else "")
        token = fp or "key?"
    elif method == "password":
        how, token = "⚠️ **паролем**", "password"
    else:
        how, token = method, method
    return who, ip, token, how


def describe(changes, extra, known_ips=(), known_keys=()):
    """Человеческий список находок. Возвращает (critical: list[str], notable: list[str])."""
    crit, note = [], []
    verb = {"new": "новый", "changed": "изменён", "gone": "пропал"}
    for sec, what, key, val in changes:
        line = f"{LABELS[sec]} {verb[what]}: `{key}`"
        if sec == "key":
            line = f"SSH-ключ {verb[what]}: `{key.split(' ', 1)[0]}` — `{val[-60:]}`"
        elif sec == "port":
            line = f"открылся порт наружу: `{key}` ({val})"
        elif sec == "elf":
            line = f"новый бинарник во временной папке: `{key}` ({val})"
        elif sec in ("uid0", "shell"):
            line = f"{LABELS[sec]} {verb[what]}: `{key}` ({val})"
        (crit if sec in ("uid0", "preload", "autorun") and what != "gone" else note).append(line)
    for p in extra["proc"]:
        crit.append(f"подозрительный процесс: `{p[:200]}`")
    for c in extra["conn"]:
        crit.append(f"соединение на порт майнинг-пула: `{c[:120]}`")
    for l in extra["login"]:
        who, ip, token, how = parse_login(l, extra["kfp"])
        new_ip, new_key = ip not in known_ips, token not in known_keys
        if new_ip or new_key:
            what = "с нового адреса" if new_ip and not new_key else "новым ключом" if new_key and not new_ip \
                else "с нового адреса и новым ключом"
            g = geo(ip)
            # знакомый разрешённый ключ — просто к сведению; пароль или ключ не из authorized_keys — «❗» (сразу хозяину)
            mark = "" if token in extra["kfp"] else "❗ "
            note.append(f"{mark}вход по SSH {what}: `{who}` — {how}" + (f" · {g}" if g else ""))
    return crit, note


def scan(target, base=None, accept=False):
    """Снимок + сравнение. Первый запуск — молча берём как эталон.
    Возвращает dict(ok, first, critical, notable, snap, extra)."""
    base = load_base() if base is None else base
    entry = base.get(target)
    ok, rows = run_script(target, (entry or {}).get("since") or "-15min")
    if not ok:
        return {"ok": False}
    snap, extra = snapshot(rows)
    first = entry is None
    entry = entry or {"snap": snap, "ips": []}
    changes = diff(entry["snap"], snap)
    # пакеты ставились с прошлой проверки: системные юниты, которые поменял apt, — норма; cron/ключи/порты/пользователи — нет
    pkg = extra.get("dpkg", 0) and extra.get("dpkg", 0) != entry.get("dpkg", 0) and not first
    if extra.get("dpkg"):
        entry["dpkg"] = extra["dpkg"]
    if pkg:
        benign = [c for c in changes if c[0] == "unit" and c[2].startswith("/etc/systemd/system/")]
        if benign:
            changes = [c for c in changes if c not in benign]
            for sec, what, key, val in benign:  # сразу в эталон
                if what == "gone":
                    entry["snap"][sec].pop(key, None)
                else:
                    entry["snap"][sec][key] = val
            extra["pkg_note"] = f"обновлялись пакеты — изменения системных юнитов приняты ({len(benign)})"
    known = set(entry.get("ips", []))
    if "keys" not in entry:  # эталон до учёта ключей: разрешённые в authorized_keys считаем знакомыми
        entry["keys"] = sorted(extra["kfp"])
    keys = set(entry.get("keys", []))
    crit, note = describe(changes, extra, known, keys)
    if extra.get("pkg_note") and not first:
        note.append(extra["pkg_note"])
    if first:
        note = []  # первый снимок — это эталон; прошлые входы и всё текущее считаем нормой
    # адреса входов запоминаем сразу: об одном IP пишем один раз
    for l in extra["login"]:
        _, ip, token, _ = parse_login(l, extra["kfp"])
        known.add(ip)
        keys.add(token)
    entry["ips"] = sorted(known)[-200:]
    entry["keys"] = sorted(keys)[-200:]
    if first or accept:
        entry["snap"] = snap
    entry["pending"] = snap  # что принять по кнопке «это я»
    if extra["now"]:
        entry["since"] = extra["now"]
    base[target] = entry
    return {"ok": True, "first": first, "critical": crit, "notable": note, "snap": snap, "extra": extra}


def accept(target, base=None):
    """Принять последний снимок как норму."""
    base = load_base() if base is None else base
    e = base.get(target)
    if e and e.get("pending"):
        e["snap"] = e["pending"]
        save_base(base)
        return True
    return False


def targets():
    return ["local"] + [n["name"] for n in nodes()]


def main(argv):
    if len(argv) < 2 or argv[1] not in ("scan", "accept"):
        print(__doc__)
        return 1
    which = argv[2:] or targets()
    base = load_base()
    for t in which:
        if argv[1] == "accept":
            r = scan(t, base, accept=True)
            print(f"{t}: {'эталон обновлён' if r['ok'] else 'нет связи'}")
            continue
        r = scan(t, base)
        if not r["ok"]:
            print(f"## {t}: нет связи")
            continue
        print(f"## {t}" + (" (первый снимок — взят как эталон)" if r["first"] else ""))
        for c in r["critical"]:
            print("!! " + c)
        for n in r["notable"]:
            print("-  " + n)
        if not r["critical"] and not r["notable"]:
            print("ничего нового")
        ports = ", ".join(sorted(r["snap"]["port"]))
        print(f"   порты наружу: {ports or '—'}")
    if argv[1] == "accept":
        save_base(base)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
