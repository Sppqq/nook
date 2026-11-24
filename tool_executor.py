"""Execute one owner-enabled Mercury shell tool with bounded output and time."""
import asyncio
from dataclasses import dataclass
import json

from agent_process import stop_process_group

HEAD_BYTES = 12_000
TAIL_BYTES = 20_000


async def excerpt(stream):
    head = bytearray()
    tail = b''
    skipped = False
    while chunk := await stream.read(65536):
        available = HEAD_BYTES - len(head)
        head.extend(chunk[:available])
        remainder = chunk[available:]
        if remainder:
            skipped |= len(tail) + len(remainder) > TAIL_BYTES
            tail = (tail + remainder)[-TAIL_BYTES:]
    output = (bytes(head) + tail).decode(errors='replace')
    if skipped or len(output) > 8000:
        return output[:3000] + '\n…[обрезано]…\n' + output[-5000:]
    return output


@dataclass(frozen=True)
class Result:
    code: int
    output: str
    uncertain: bool = False


class ShellCalls:
    def __init__(self):
        self.results = {}
        self.uncertain_commands = set()

    async def execute(self, call, *, cwd, env, run, deadline, on_step=None):
        try:
            identifier = call['id']
            function = call['function']
            args = json.loads(function.get('arguments') or '{}')
            command = args.get('cmd')
            timeout = args.get('timeout') or 120
            if (not isinstance(identifier, str) or not identifier
                    or function['name'] != 'shell' or not isinstance(command, str)
                    or not command.strip() or '\0' in command
                    or type(timeout) is not int or timeout <= 0):
                raise ValueError
        except (AttributeError, TypeError, KeyError, ValueError):
            return Result(-1, 'Некорректные параметры инструмента shell.')
        if identifier in self.results:
            saved_command, saved_result = self.results[identifier]
            if command == saved_command:
                return saved_result
            return Result(-1, 'Повторный ID инструмента с другой командой отклонён.')
        command_key = command.strip()
        if command_key in self.uncertain_commands:
            return Result(-1, 'Исход предыдущего запуска неизвестен. Сначала проверь результат.')
        loop = asyncio.get_running_loop()
        budget = min(timeout, 900, deadline - loop.time())
        if run.cancelled or budget <= 0:
            return Result(-1, 'Запуск отменён: задача остановлена или истёк её срок.')
        label = command.replace('\n', ' ')[:120]
        if on_step:
            await on_step('▸ ' + label)
        if run.cancelled or deadline <= loop.time():
            return Result(-1, 'Запуск отменён: задача остановлена или истёк её срок.')
        proc = await asyncio.create_subprocess_exec(
            'bash', '-lc', command, cwd=cwd, env=env, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        run.proc = proc
        try:
            async with asyncio.timeout(min(budget, deadline - loop.time())):
                if run.cancelled:
                    run.cancel()
                output = await excerpt(proc.stdout)
                result = Result(await proc.wait(), output, uncertain=run.cancelled)
        except TimeoutError:
            result = Result(-1, 'Таймаут команды. Исход операции неизвестен.', uncertain=True)
        finally:
            await stop_process_group(proc)
            run.proc = None
        self.results[identifier] = command, result
        if result.uncertain:
            self.uncertain_commands.add(command_key)
        if on_step and not run.cancelled:
            await on_step(('✓ ' if result.code == 0 else '✗ ') + label)
        return result
