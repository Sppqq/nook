"""Personal button visibility and appearance, independent of provider settings."""
GROUPS = {
    'answers': (
        ('answer_continue', 'Продолжить · Mercury', True),
        ('answer_questions', 'Да / нет под вопросами', True),
        ('answer_menu', 'Меню под ответом', True),
        ('answer_fav', 'В избранное', True),
        ('answer_mute', 'Не следить за этим', True),
        ('answer_close', 'Закрыть ответ', False),
    ),
    'menu': (
        ('menu_status', 'Статус', True), ('menu_graph', 'Графики', True),
        ('menu_limits', 'Лимиты', True), ('menu_check', 'Проверь всё', True),
        ('menu_clean', 'Уборка', True), ('menu_jobs', 'Задачи', True),
        ('menu_nodes', 'Ноды', True), ('menu_favs', 'Избранное', True),
        ('menu_digest', 'Сводка', True), ('menu_mods', 'Кнопки модов', True),
    ),
    'look': (
        ('icons', 'Иконки на кнопках', True),
        ('premium_icons', 'Премиум-эмодзи на кнопках', True),
        ('colors', 'Цветные кнопки', True),
        ('single_column', 'Кнопки в один столбик', False),
    ),
}
DEFAULTS = {key: default for items in GROUPS.values() for key, _, default in items}
MENU_ACTIONS = {action: 'menu_' + name for action, name in (
    ('status', 'status'), ('graph', 'graph'), ('limits', 'limits'),
    ('q:check', 'check'), ('q:clean', 'clean'), ('jobs', 'jobs'),
    ('nodes', 'nodes'), ('favs', 'favs'), ('digest', 'digest'))}
ANSWER_ACTIONS = {'th:continue': 'answer_continue', 'th:yes': 'answer_questions',
                  'th:no': 'answer_questions', 'menu': 'answer_menu',
                  'fav': 'answer_fav', 'mute': 'answer_mute'}


def option(state, key):
    config = state.get('custom_ui', {})
    value = config.get(key) if isinstance(config, dict) else None
    return value if isinstance(value, bool) else DEFAULTS.get(key, True)


def visible(state, action, scope):
    if scope == 'menu':
        key = 'menu_mods' if action.startswith('mod:') else MENU_ACTIONS.get(action)
    else:
        key = 'answer_questions' if action.startswith('mq:') else ANSWER_ACTIONS.get(action)
    return option(state, key) if key else True


def filter_keyboard(state, markup, scope, make_keyboard, make_button):
    rows = [[button for button in row if visible(state, button.callback_data or '', scope)]
            for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    if scope == 'answer' and option(state, 'answer_close'):
        if not any(b.callback_data == 'close' for row in rows for b in row):
            rows.append([make_button('Закрыть', 'close', 'cross')])
    if option(state, 'single_column'):
        rows = [[button] for row in rows for button in row]
    return make_keyboard(*rows) if rows else None


def apply_action(state, action):
    parts = action.split(':')
    if len(parts) == 4 and parts[:2] == ['custom', 'toggle']:
        section, key = parts[2:]
        if key not in {item[0] for item in GROUPS.get(section, ())}:
            raise ValueError('Unknown setting')
        value = not option(state, key)
        if not isinstance(state.get('custom_ui'), dict):
            state['custom_ui'] = {}
        state['custom_ui'][key] = value
        return section
    if len(parts) == 3 and parts[:2] == ['custom', 'preset'] and parts[2] in ('default', 'minimal', 'all'):
        preset = parts[2]
        config = dict(DEFAULTS)
        if preset == 'minimal':
            for key in config:
                if key.startswith('menu_') or key.startswith('answer_'):
                    config[key] = False
            for key in ('answer_continue', 'answer_questions', 'answer_close', 'menu_status', 'menu_limits'):
                config[key] = True
            config['icons'] = config['colors'] = False
        elif preset == 'all':
            config.update({key: True for key in config if key.startswith(('menu_', 'answer_'))})
        state['custom_ui'] = config
        return 'home'
    raise ValueError('Unknown action')


def settings_view(state, button, keyboard, section='home'):
    nav = [(button('Под ответами', 'settings_answers', 'say'), button('Главное меню', 'settings_menu', 'home')),
           (button('Оформление', 'settings_look', 'spark'),)]
    if section == 'home':
        text = ('{{e:more}} **Настройки интерфейса**\n\nВыбирай, какие кнопки показывать. '
                'Каждое нажатие сохраняется сразу.\n\n'
                'Быстрые наборы заменяют только настройки интерфейса. '
                'Уведомления и модель настраиваются отдельно в «Ещё».')
        rows = nav + [(button('Минимум', 'custom:preset:minimal', 'clean'), button('Стандарт', 'custom:preset:default', 'refresh'),
                       button('Всё', 'custom:preset:all', 'plus'))]
    else:
        titles = {'answers': 'Кнопки под ответами', 'menu': 'Кнопки главного меню', 'look': 'Оформление кнопок'}
        text = '{{e:spark}} **' + titles[section] + '**\n\nНажми, чтобы включить или выключить.'
        if section == 'menu':
            text += '\n«Ещё», «Настройки» и остановка работающей задачи остаются доступны. Скрытые разделы остаются в «Ещё». '
            text += 'Избранное и сводка появляются, когда в них есть записи.'
        elif section == 'answers':
            text += '\nПрименяется к следующим ответам. Кнопки подтверждения root-команд и входа в аккаунт сохраняются.'
        else:
            text += '\nЦвет поддерживается в расширенном виде Telegram. Премиум-эмодзи доступны в расширенном виде; в обычном виде будут простые. Сначала включи «Иконки», затем выбери премиум или обычные. Иконки и цвет меняются сразу. Столбик применяется к главному меню и ответам.'
        rows = [(button(('Вкл · ' if option(state, key) else 'Выкл · ') + label,
                        'custom:toggle:' + section + ':' + key,
                        'ok' if option(state, key) else 'cross'),) for key, label, _ in GROUPS[section]]
        rows += [(button('Назад к настройкам', 'settings', 'back'),)]
    rows += [(button('Меню', 'menu', 'home'), button('Ещё', 'more', 'more'))]
    return text, keyboard(*rows)
