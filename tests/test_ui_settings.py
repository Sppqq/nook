import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('custom_settings', ROOT / 'ui_settings.py')
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


def button(text, action, icon=None, style=None):
    return SimpleNamespace(text=text, callback_data=action)


def keyboard(*rows):
    return SimpleNamespace(inline_keyboard=[list(row) for row in rows if row])


class SettingsTests(unittest.TestCase):
    def actions(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []

    def filtered(self, state, actions, scope='answer'):
        return ui.filter_keyboard(state, keyboard([button(a, a) for a in actions]), scope, keyboard, button)

    def test_defaults_preserve_current_buttons(self):
        actions = ['th:continue', 'th:yes', 'th:no', 'menu', 'fav', 'mute', 'login']
        self.assertEqual(self.actions(self.filtered({}, actions)), actions)
        self.assertFalse(ui.option({}, 'answer_close'))

    def test_toggle_persists_and_rejects_unrelated_settings(self):
        state = {'watch': True}
        self.assertEqual(ui.apply_action(state, 'custom:toggle:answers:answer_continue'), 'answers')
        self.assertFalse(ui.option(json.loads(json.dumps(state)), 'answer_continue'))
        for action in ('custom:toggle:look:watch', 'custom:toggle:look:menu_jobs', 'custom:preset:unknown'):
            with self.assertRaises(ValueError):
                ui.apply_action(state, action)
        self.assertTrue(state['watch'])

    def test_minimal_preserves_safety_and_exit(self):
        state = {'watch': True, 'guard': True, 'model': 'example'}
        ui.apply_action(state, 'custom:preset:minimal')
        actions = ['status', 'graph', 'q:clean', 'jobs', 'mod:example:go', 'more', 'settings', 'stop']
        self.assertEqual(self.actions(self.filtered(state, actions, 'menu')), ['status', 'more', 'settings', 'stop'])
        self.assertEqual(self.actions(self.filtered(state, ['login', 'root:approve'])), ['login', 'root:approve', 'close'])
        self.assertTrue(state['watch'])
        self.assertTrue(state['guard'])
        self.assertEqual(state['model'], 'example')

    def test_single_column_and_close_not_duplicated(self):
        state = {'custom_ui': {'single_column': True, 'answer_close': True}}
        result = self.filtered(state, ['menu', 'close'])
        self.assertEqual([len(r) for r in result.inline_keyboard], [1, 1])
        self.assertEqual(self.actions(result).count('close'), 1)

    def test_all_hidden_returns_none(self):
        state = {'custom_ui': {key: False for key in ui.DEFAULTS}}
        self.assertIsNone(self.filtered(state, ['menu', 'fav', 'th:yes', 'mq:1:y']))

    def test_presets_and_bad_saved_values(self):
        state = {'custom_ui': []}
        self.assertTrue(ui.option(state, 'icons'))
        ui.apply_action(state, 'custom:preset:all')
        self.assertTrue(ui.option(state, 'answer_close'))
        ui.apply_action(state, 'custom:preset:default')
        self.assertEqual(state['custom_ui'], ui.DEFAULTS)

    def test_views_have_valid_unique_callback_payloads(self):
        for section in ('home', 'answers', 'menu', 'look'):
            text, markup = ui.settings_view({}, button, keyboard, section)
            self.assertTrue(text)
            actions = self.actions(markup)
            self.assertEqual(len(actions), len(set(actions)))
            self.assertTrue(all(len(a.encode()) <= 64 for a in actions))
            self.assertIn('menu', actions)

    def test_core_icon_and_color_controls(self):
        tree = ast.parse((ROOT / 'bot.py').read_text())
        definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'btn')
        state = {'custom_ui': {'icons': False, 'colors': False}}
        ns = {'STATE': state, 'ui_option': ui.option, 'plain_buttons': lambda: False,
              'EMOJI': {'home': ['123', 'H']}, 'InlineKeyboardButton': lambda **kw: kw}
        exec(compile(ast.Module(body=[definition], type_ignores=[]), '<btn>', 'exec'), ns)
        self.assertEqual(ns['btn']('Menu', 'menu', 'home', 'primary'), {'text': 'Menu', 'callback_data': 'menu'})
        state['custom_ui'] = {}
        self.assertEqual(ns['btn']('Menu', 'menu', 'home', 'primary')['style'], 'primary')
        self.assertEqual(ns['btn']('Menu', 'menu', 'home')['icon_custom_emoji_id'], '123')
        state['custom_ui'] = {'premium_icons': False}
        ordinary = ns['btn']('Menu', 'menu', 'home', 'primary')
        self.assertEqual(ordinary['text'], 'H Menu')
        self.assertNotIn('icon_custom_emoji_id', ordinary)
        self.assertEqual(ordinary['style'], 'primary')

