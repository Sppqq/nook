"""Opt-in routing across saved ChatGPT logins without switching the global login."""
import asyncio
import base64
from dataclasses import dataclass
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time

STRATEGIES = {
    "active": "Основной первым",
    "reserve": "Основной в запасе",
    "balance": "Больше остатка первым",
    "least": "Меньше остатка первым",
}
SHARED = ("config.toml", "sessions", "archived_sessions", "thread-writer-locks",
          "session_index.jsonl", "plugins", "skills", "rules", "installation_id", "memories")
QUOTA_ERROR = re.compile(
    r"\busage[_ -]limit(?:[_ -]reached)?\b|\bhit your limit\b|\bquota (?:exceeded|exhausted)\b|\binsufficient_quota\b",
    re.I,
)
AUTH_ERROR = re.compile(r"\b401\b|\brefresh[_ -]?token\b|слетела авторизация Codex", re.I)
REFRESH = 60
MAX_PARALLEL_CHECKS = 4


@dataclass(frozen=True)
class Account:
    ident: str
    name: str
    active: bool
    workspace: str
    auth: Path


@dataclass(frozen=True)
class Window:
    key: tuple
    used: float
    minutes: int
    reset: float


@dataclass(frozen=True)
class Quota:
    windows: tuple
    blocked: bool


def saved_accounts(root):
    folder = root / "accounts"
    try:
        owned_path(folder, directory=True)
        path = folder / "registry.json"
        owned_path(path)
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        entries = data.get("accounts", [])
        if not isinstance(entries, list):
            return []
    except (OSError, ValueError, AttributeError):
        return []
    accounts = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("auth_mode") not in ("chatgpt", "chatgptAuthTokens"):
            continue
        key = entry.get("account_key")
        workspace = entry.get("chatgpt_account_id")
        if not isinstance(key, str) or not 0 < len(key.encode()) <= 180 or not isinstance(workspace, str) or not workspace:
            continue
        ident = hashlib.sha256(key.encode()).hexdigest()[:16]
        name = entry.get("alias") or entry.get("email")
        name = " ".join(name.replace("`", "").split())[:56] if isinstance(name, str) else ""
        encoded = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
        active = key == data.get("active_account_key")
        auth = root / "auth.json" if active else folder / (encoded + ".auth.json")
        accounts.append(Account(ident, name or f"Аккаунт {len(accounts) + 1}", active, workspace, auth))
    workspaces = {}
    for account in sorted(accounts, key=lambda item: not item.active):
        workspaces.setdefault(account.workspace, account)
    return list(workspaces.values())


def quota_for(result, workspace, model):
    # A global account switch or a provider mismatch must never lend one
    # account's quota to another account.
    if not isinstance(result, dict) or result.get("accountId") != workspace:
        raise ValueError("Codex quota account mismatch")
    buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits")}
    if not isinstance(buckets, dict):
        raise ValueError("Invalid quota buckets")
    windows = []
    allowed = result.get("ordinaryUsageAllowed")
    if type(allowed) is not bool:
        raise ValueError("Codex quota availability is unknown")
    blocked = not allowed
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        if key != "codex" and bucket.get("normalModelSlug") != model and len(buckets) != 1:
            continue
        blocked |= bool(bucket.get("rateLimitReachedType") or bucket.get("spendControlReached"))
        for kind in ("primary", "secondary"):
            window = bucket.get(kind)
            if not isinstance(window, dict):
                continue
            used, minutes, reset = (window.get(field) for field in ("usedPercent", "windowDurationMins", "resetsAt"))
            if (type(used) in (int, float) and 0 <= used <= 100 and
                    type(minutes) is int and minutes > 0 and
                    (reset is None or (type(reset) in (int, float) and 0 <= reset < 253402300800))):
                windows.append(Window((key, kind), used, minutes, reset or 0))
    if not windows:
        raise ValueError("No applicable Codex quota windows")
    return Quota(tuple(windows), blocked)


def owned_path(path, *, directory=False, private=False, allow_user_group=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    forbidden = 0o077 if private else 0o022
    if directory and allow_user_group and not private and info.st_mode & 0o020:
        try:
            user = pwd.getpwuid(os.getuid())
            group = grp.getgrgid(info.st_gid)
            if (group.gr_gid == user.pw_gid == user.pw_uid and group.gr_name == user.pw_name
                    and set(group.gr_mem) <= {user.pw_name}
                    and all(entry.pw_uid == user.pw_uid for entry in pwd.getpwall()
                            if entry.pw_gid == group.gr_gid)):
                forbidden = 0o002
        except KeyError:
            pass
    if (not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & forbidden
            or (not directory and info.st_nlink != 1)):
        raise OSError("Unsafe Codex pool path")


class Pool:
    def __init__(self, root, settings, read_limits, login_lock):
        self.root = Path(root)
        self.settings = settings
        self.read_limits = read_limits
        self.login_lock = login_lock
        self.cache = {}
        self.cooldowns = {}
        self.busy = set()
        self.refresh_lock = asyncio.Lock()
        self.changed = asyncio.Event()

    def accounts(self):
        return saved_accounts(self.root)

    def enabled(self):
        return self.settings.get("enabled", False) is True

    def cap(self, account):
        caps = self.settings.get("caps", {})
        value = caps.get(account.ident) if isinstance(caps, dict) else None
        return value if type(value) is int and 0 <= value <= 100 else 100

    def environment(self, account, base):
        env = {key: value for key, value in base.items()
               if key not in ("CODEX_HOME", "CODEX_SQLITE_HOME", "OPENAI_API_KEY", "CODEX_API_KEY")}
        owned_path(self.root, directory=True, allow_user_group=True)
        if not account.active:
            owned_path(account.auth.parent, directory=True)
        owned_path(account.auth, private=True)
        if account.active:
            env.update(CODEX_HOME=str(self.root), CODEX_SQLITE_HOME=str(self.root))
            return env
        pool = self.root / "accounts" / ".nook-pool"
        home = pool / account.ident
        for folder in (pool, home):
            folder.mkdir(mode=0o700, exist_ok=True)
            owned_path(folder, directory=True, private=True)
        links = {"auth.json": account.auth, **{name: self.root / name for name in SHARED}}
        for name, target in links.items():
            link = home / name
            try:
                link.symlink_to(target)
            except FileExistsError:
                if not link.is_symlink() or link.readlink() != target:
                    raise OSError("Unexpected Codex pool link")
        env.update(CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(self.root))
        return env

    async def refresh(self, accounts, model, *, force=False):
        async with self.refresh_lock:
            if self.login_lock.locked():
                return
            active = set(self.accounts())
            self.cache = {key: value for key, value in self.cache.items() if key[0] in active}
            now = time.time()
            self.cooldowns = {key: value for key, value in self.cooldowns.items() if value[0] > now}
            slots = asyncio.Semaphore(MAX_PARALLEL_CHECKS)

            async def sample(account):
                key = (account, model)
                cached = self.cache.get(key)
                if account.ident in self.busy or (cached and not force and time.monotonic() - cached[0] < REFRESH):
                    return
                async with slots:
                    if self.login_lock.locked() or account.ident in self.busy:
                        return
                    self.busy.add(account.ident)
                    try:
                        self.cache.pop(key, None)
                        result = await self.read_limits(self.environment(account, {}))
                        if account in self.accounts() and not self.login_lock.locked():
                            self.cache[key] = time.monotonic(), quota_for(result, account.workspace, model)
                    except (OSError, RuntimeError, ValueError, KeyError, TypeError, asyncio.TimeoutError):
                        self.cache.pop(key, None)
                    finally:
                        self.busy.discard(account.ident)
                        self.changed.set()
            await asyncio.gather(*(sample(account) for account in accounts if self.cap(account)))

    def status(self, account, model, now):
        cap = self.cap(account)
        cached = self.cache.get((account, model))
        if not cap:
            return "excluded", None, None
        cool = self.cooldowns.get((account.ident, model))
        if cool and cool[0] > now:
            return cool[1], None, cool[0]
        if not cached or time.monotonic() - cached[0] >= REFRESH:
            return "unknown", None, None
        quota = cached[1]
        if any(w.reset and w.reset <= now for w in quota.windows):
            return "unknown", None, None
        headroom = min(cap - w.used for w in quota.windows)
        if quota.blocked or headroom <= 0:
            resets = [w.reset for w in quota.windows if w.used >= cap and w.reset > now]
            return "quota", 0, max(resets, default=now + REFRESH)
        return "ready", headroom, None

    def score(self, account, left):
        strategy = self.settings.get("strategy", "active")
        preferred = account.active if strategy == "active" else not account.active if strategy == "reserve" else False
        return preferred, -left if strategy == "least" else left

    async def run(self, execute, prompt, thread_id, images, on_step, run, timeout, model):
        deadline = time.monotonic() + timeout
        attempted = set()
        last = (thread_id, "", None)
        while not run.cancelled and time.monotonic() < deadline:
            accounts = self.accounts()
            if not accounts and not attempted:
                return await execute(prompt, thread_id, images, on_step, run, timeout)
            if self.login_lock.locked():
                await asyncio.sleep(min(0.1, deadline - time.monotonic()))
                continue
            remaining = [a for a in accounts if a.ident not in attempted and self.cap(a)]
            try:
                await asyncio.wait_for(self.refresh(remaining, model), deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if run.cancelled or time.monotonic() >= deadline:
                break
            if not self.enabled() and not attempted:
                return await execute(prompt, thread_id, images, on_step, run, deadline - time.monotonic())
            now = time.time()
            readiness = [(account, *self.status(account, model, now)) for account in remaining]
            ready = [(account, left) for account, status, left, _ in readiness if status == "ready"]
            free = [(account, left) for account, left in ready if account.ident not in self.busy]
            if not free:
                if ready:
                    self.changed.clear()
                    try:
                        await asyncio.wait_for(self.changed.wait(), min(1, deadline - time.monotonic()))
                    except asyncio.TimeoutError:
                        pass
                    continue
                if last[2] and AUTH_ERROR.search(last[2]):
                    return last
                if attempted or (readiness and all(status == "quota" for _, status, _, _ in readiness)):
                    return thread_id, last[1], "Codex usage limit: в пуле сейчас нет доступной квоты."
                return thread_id, last[1], "Пул Codex: проверь бюджеты, авторизацию и свежесть данных."
            account, _ = max(free, key=lambda item: self.score(*item))
            if self.login_lock.locked() or account not in self.accounts():
                continue
            self.busy.add(account.ident)
            attempted.add(account.ident)
            try:
                try:
                    env = self.environment(account, {})
                except OSError:
                    self.cooldowns[(account.ident, model)] = time.time() + REFRESH, "auth"
                    last = thread_id, last[1], "Слетела авторизация Codex."
                    continue
                result = await execute(prompt, thread_id, images, on_step, run,
                                       max(0.001, deadline - time.monotonic()), account_env=env)
                returned_id, final, error = result
                if thread_id and returned_id and thread_id != returned_id:
                    run.effects_possible = True
                    return thread_id, "", "Codex returned a different session. Automatic retry was stopped."
                thread_id = returned_id or thread_id
                if not error or run.cancelled:
                    return result
                if AUTH_ERROR.search(error):
                    reason, cooldown = "auth", 600
                elif QUOTA_ERROR.search(error):
                    reason, cooldown = "quota", REFRESH
                else:
                    return result
                self.cooldowns[(account.ident, model)] = time.time() + cooldown, reason
                if final:
                    run.effects_possible = True
                if run.effects_possible:
                    return result
                last = thread_id, final or last[1], error
            finally:
                self.cache.pop((account, model), None)
                self.busy.discard(account.ident)
                self.changed.set()
        return last[0], last[1], "остановлено" if run.cancelled else "таймаут Codex"
