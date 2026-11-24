"""Select provider-compatible session IDs and preserve bounded transfer context."""
import re

MERCURY_THREAD = re.compile(r'merc-[A-Za-z0-9_-]{1,64}')
TRANSFER_CONTEXT_LIMIT = 3000


def is_mercury_thread(ident):
    return isinstance(ident, str) and MERCURY_THREAD.fullmatch(ident) is not None


def session_engine(ident):
    if is_mercury_thread(ident):
        return 'mercury'
    if isinstance(ident, str) and ident.startswith('agy-'):
        return 'antigravity'
    return 'codex'


def resume_for_engine(prompt, thread, engine, state):
    if not thread or session_engine(thread) == engine:
        return prompt, thread
    twins = state.get('xthreads')
    twin = twins.get(thread) if isinstance(twins, dict) else None
    if isinstance(twin, str) and twin and session_engine(twin) == engine:
        return prompt, twin
    answers = state.get('last_answer')
    answer = answers.get(thread) if isinstance(answers, dict) else None
    if isinstance(answer, str) and answer:
        prompt = ('[контекст: твой прошлый ответ в этом разговоре был]\n'
                  + answer[:TRANSFER_CONTEXT_LIMIT] + '\n\n' + prompt)
    return prompt, None
