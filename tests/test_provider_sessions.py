import unittest

from provider_sessions import is_mercury_thread, resume_for_engine, session_engine


class ProviderSessionTests(unittest.TestCase):
    def test_compatible_existing_and_twin_ids(self):
        self.assertEqual(resume_for_engine('task', None, 'codex', {}), ('task', None))
        self.assertEqual(resume_for_engine('task', 'agy-old', 'antigravity', {}), ('task', 'agy-old'))
        state = {'xthreads': {'agy-old': 'merc-one'}}
        self.assertEqual(resume_for_engine('task', 'agy-old', 'mercury', state), ('task', 'merc-one'))

    def test_mismatched_or_malformed_ids_only_transfer_bounded_context(self):
        state = {'xthreads': {'agy-old': 'merc-dir/secret'}, 'last_answer': {'agy-old': 'x' * 3100}}
        prompt, ident = resume_for_engine('task', 'agy-old', 'mercury', state)
        self.assertIsNone(ident)
        self.assertIn('x' * 3000, prompt)
        self.assertNotIn('x' * 3001, prompt)
        self.assertTrue(prompt.endswith('task'))
        self.assertEqual(resume_for_engine('task', 'agy-old', 'mercury', {'last_answer': []}), ('task', None))
        self.assertFalse(is_mercury_thread('merc-dir/secret'))
        self.assertEqual(session_engine('codex-thread'), 'codex')
