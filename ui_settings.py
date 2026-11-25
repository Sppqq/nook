"""Personal button visibility and appearance, independent of provider settings."""


def translate(state, russian, english):
    """Use Russian for legacy installations and unknown saved preferences."""
    return english if state.get('ui_language') == 'en' else russian


GROUPS = {
    'answers': (
        ('answer_continue', 'Продолжить · Mercury', 'Continue · Mercury', True),
        ('answer_questions', 'Да / нет под вопросами', 'Yes / no under questions', True),
        ('answer_menu', 'Меню под ответом', 'Menu under replies', True),
        ('answer_fav', 'В избранное', 'Add to favorites', True),
        ('answer_mute', 'Не следить за этим', 'Mute this topic', True),
        ('answer_close', 'Закрыть ответ', 'Close reply', False),
    ),
    'menu': (
        ('menu_status', 'Статус', 'Status', True), ('menu_graph', 'Графики', 'Charts', True),
        ('menu_limits', 'Лимиты', 'Usage limits', True), ('menu_check', 'Проверь всё', 'Run checks', True),
        ('menu_clean', 'Уборка', 'Clean up', True), ('menu_jobs', 'Задачи', 'Tasks', True),
        ('menu_nodes', 'Ноды', 'Nodes', True), ('menu_favs', 'Избранное', 'Favorites', True),
        ('menu_digest', 'Сводка', 'Digest', True), ('menu_mods', 'Кнопки модов', 'Mod buttons', True),
    ),
    'look': (
        ('icons', 'Иконки на кнопках', 'Button icons', True),
        ('premium_icons', 'Премиум-эмодзи на кнопках', 'Premium emoji on buttons', True),
        ('colors', 'Цветные кнопки', 'Colored buttons', True),
        ('single_column', 'Кнопки в один столбик', 'One button per row', False),
    ),
}
DEFAULTS = {key: default for items in GROUPS.values() for key, _, _, default in items}
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
            rows.append([make_button(translate(state, 'Закрыть', 'Close'), 'close', 'cross')])
    if option(state, 'single_column'):
        rows = [[button] for row in rows for button in row]
    return make_keyboard(*rows) if rows else None


def apply_action(state, action):
    parts = action.split(':')
    if len(parts) == 3 and parts[:2] == ['custom', 'language'] and parts[2] in ('ru', 'en'):
        state['ui_language'] = parts[2]
        return 'home'
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
    def t(russian, english):
        return translate(state, russian, english)

    nav = [(button(t('Под ответами', 'Under replies'), 'settings_answers', 'say'),
            button(t('Главное меню', 'Main menu'), 'settings_menu', 'home')),
           (button(t('Оформление', 'Appearance'), 'settings_look', 'spark'),)]
    if section == 'home':
        text = t(
            '{{e:more}} **Настройки интерфейса**\n\nВыбирай, какие кнопки показывать. '
            'Каждое нажатие сохраняется сразу.\n\n'
            'Быстрые наборы заменяют только настройки интерфейса. '
            'Уведомления и модель настраиваются отдельно в «Ещё».',
            '{{e:more}} **Interface settings**\n\nChoose the buttons to display. Changes are saved immediately. '
            'Presets apply to the interface. Notifications and the model are configured under More.')
        english = state.get('ui_language') == 'en'
        rows = nav + [
            (button('Русский', 'custom:language:ru', 'ok' if not english else None),
             button('English', 'custom:language:en', 'ok' if english else None)),
            (button(t('Минимум', 'Minimal'), 'custom:preset:minimal', 'clean'),
             button(t('Стандарт', 'Default'), 'custom:preset:default', 'refresh'),
             button(t('Всё', 'All'), 'custom:preset:all', 'plus')),
        ]
    else:
        titles = {'answers': t('Кнопки под ответами', 'Buttons under replies'),
                  'menu': t('Кнопки главного меню', 'Main menu buttons'),
                  'look': t('Оформление кнопок', 'Button appearance')}
        text = '{{e:spark}} **' + titles[section] + '**\n\n' + t(
            'Нажми, чтобы включить или выключить.', 'Tap a button to toggle it.')
        if section == 'menu':
            text += t(
                '\n«Ещё», «Настройки» и остановка работающей задачи остаются доступны. Скрытые разделы остаются в «Ещё». '
                'Избранное и сводка появляются, когда в них есть записи.',
                '\nMore, Settings and Stop remain available. Open hidden sections from More. '
                'Favorites and Digest appear when they have entries.')
        elif section == 'answers':
            text += t(
                '\nПрименяется к следующим ответам. Кнопки подтверждения root-команд и входа в аккаунт сохраняются.',
                '\nApplies to future replies. Root approval and account sign-in buttons stay available.')
        else:
            text += t(
                '\nЦвет поддерживается в расширенном виде Telegram. Премиум-эмодзи доступны в расширенном виде; '
                'в обычном виде будут простые. Сначала включи «Иконки», затем выбери премиум или обычные. '
                'Иконки и цвет меняются сразу. Столбик применяется к главному меню и ответам.',
                '\nColors and premium emoji use the rich Telegram layout. The classic layout uses standard emoji. '
                'Enable Icons before choosing premium emoji. Icon and color changes apply immediately. '
                'The single-column layout applies to the main menu and replies.')
        rows = [(button((t('Вкл · ', 'On · ') if option(state, key) else t('Выкл · ', 'Off · '))
                        + t(russian, english), 'custom:toggle:' + section + ':' + key,
                        'ok' if option(state, key) else 'cross'),)
                for key, russian, english, _ in GROUPS[section]]
        rows += [(button(t('Назад к настройкам', 'Back to settings'), 'settings', 'back'),)]
    rows += [(button(t('Меню', 'Menu'), 'menu', 'home'), button(t('Ещё', 'More'), 'more', 'more'))]
    return text, keyboard(*rows)
