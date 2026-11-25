# Local extension lifecycle · Локальные расширения

Local extension drafts live under `spool/local-extensions`. Nook keeps each
version under its SHA-256 and does not import drafts into the Telegram
process. Existing `mods/*.py` retain the trusted `setup(m)` API.

## Create, test and activate

Create a new file outside `mods/`. Use `mod` or `tool` for Python, or
`skill` for UTF-8 text. An isolated Python extension uses this contract:

```python
NOOK_EXTENSION_API = 1

def run(payload):
    if payload == {"healthcheck": True}:
        return {"ok": True}
    return {"result": payload}
```

```bash
bin/nook-dev stage tool echo 1.0.0 /path/to/draft.py
bin/nook-dev test tool echo 1.0.0
bin/nook-dev activate tool echo 1.0.0
bin/nook-dev run tool echo --input '{"message":"hello"}'
bin/nook-dev status tool echo
bin/nook-dev rollback tool echo
```

`stage` prints the SHA-256 for owner review. To revise, stage a new SemVer.
A skill returns text as data. An operator or caller chooses whether to use
it as context. Staging and testing do not grant any production capability.

The owner installs a current system Bubblewrap through their OS package
manager. The owner places their policy in
`/etc/nook/extensions/<service-uid>.json`, in root-owned directories without
group/other write permission. The file is root-owned and readable by the
service account. Example:

```json
{
  "schema": 1,
  "allow": {
    "tool:echo": "isolated-v1"
  }
}
```

An exact SHA-256 approves only that content. `isolated-v1` allows the
owner-selected name to evolve without signing every revision, always with the
same sandbox. All unlisted names are denied. Remove an entry to revoke it.
Activation and each invocation re-read the external policy. Revocation
prevents subsequent invocations and does not terminate one already running.

If the owner adds a `legacy` object to this policy, the legacy registry only
loads modules whose names map to the exact approved SHA-256 of their
`mods/NAME.py`. This includes the built-in modules. Review and pin existing
trusted files before enabling that mode. An empty `legacy` object denies
legacy imports. Leaving the key absent preserves installed behavior. A
policy change takes effect for legacy modules at the next reload.

## Moving an existing trusted mod gradually

Keep the working `mods/NAME.py` active while you extract a function with
explicit JSON inputs and outputs. Pass only the fields it needs. Keep
Telegram handlers, scheduled dispatch, and permissions in the trusted wrapper.
Stage and test the isolated function using fixtures for the input shapes and
failure cases the legacy mod handles. Compare its output to the old function
using copies of data and read-only cases. Avoid invoking both implementations
on a live task that can change state.

After the owner approves the isolated version, have the trusted wrapper call
the active extension through `bin/nook-dev run`. Keep the old wrapper and its
data available until the owner accepts the switch. If the isolated function
cannot do a required job without host access, keep that part in the trusted
wrapper. A return to the previous wrapper or extension version affects future
requests. Reconcile any in-flight side effects before retrying.

## Runtime boundary

The Linux runner uses Bubblewrap namespaces, a read-only system Python
tree, scratch storage with a size limit, and a fresh, restricted
environment. It omits the host home, instance directory, SSH agent, sudo
policy, and host network. The worker has capped input/output, CPU time,
address space, and elapsed execution time. Bubblewrap setup failures stop the
request. Test and runtime launch use the same restrictions. Failed runtime
revisions revert to the last healthy authorized version or get disabled.
The invocation is never repeated on the fallback. History is retained with a
count of entries removed after the latest 2,000 events.

The "local extension" API does not offer SSH, service control, or arbitrary
access to instance data. Existing trusted mods and the explicitly enabled
root helper retain their administrator-selected capabilities. If the legacy
agent has permission to rewrite the host-side broker or act as root, it
already operates outside this sandbox. An owner requiring a boundary against
that account needs OS ownership/separation for the trusted broker and must
withhold general root access. Policies do not form part of the Nook backup
or updater. Review or reprovision them separately during a move.

## Управление из Telegram

В **Ещё → Моды → Локальные расширения** владелец может выбрать версию,
проверить её, активировать или откатить. Разрешения из Telegram не выдаются.
Для создания новой версии агент использует `bin/nook-dev stage`. Данные и
история локальных пакетов остаются в инстансе. Русский и английский
интерфейсы остальных частей бота пока имеют разный охват.

См. [документацию Bubblewrap](https://github.com/containers/bubblewrap/blob/main/README.md).
