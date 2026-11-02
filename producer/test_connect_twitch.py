"""Device authorization contract without network access or real credentials."""
import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from connect_twitch import DEFAULT_SCOPES, OAUTH, connect, main, validate_identity


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'new' / 'token.json'
        self.identity = dict(client_id='publicapp', user_id='12345', login='another_streamer',
                             scopes=list(DEFAULT_SCOPES), expires_in=3600)
        self.device = dict(verification_uri='https://www.twitch.tv/activate?public=true',
                           user_code='DEMO', device_code='opaque-device-secret', expires_in=300,
                           interval=5)
        self.tokens = dict(access_token='opaque-access-secret', refresh_token='opaque-refresh-secret')
        self.output = io.StringIO()

    def response(self, status, body):
        return Mock(status_code=status, json=Mock(return_value=body))

    def run_connection(self, *, responses=None, identity=None, device=None, **kwargs):
        posts = [self.response(200, device or self.device), *(responses or [self.response(200, self.tokens)])]
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with patch('connect_twitch.requests.post', side_effect=posts) as post, \
             patch('connect_twitch.requests.get', return_value=self.response(200, identity or self.identity)) as get, \
             patch('connect_twitch.time.monotonic', side_effect=lambda: now[0]), \
             patch('connect_twitch.time.sleep', side_effect=sleep) as slept, \
             contextlib.redirect_stdout(self.output):
            connect('publicapp', 'Another_Streamer', token_file=self.path, **kwargs)
        return post, get, slept

    def test_first_connection_needs_no_old_credential_and_saves_private_file(self):
        post, get, _ = self.run_connection()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['login'], 'another_streamer')
        self.assertEqual(saved['user_id'], '12345')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs['data']['scopes'], 'user:write:chat')
        self.assertEqual(get.call_args.args[0], OAUTH + 'validate')
        for call in [*post.call_args_list, *get.call_args_list]:
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertNotIn(self.tokens['access_token'], self.output.getvalue())
        self.assertNotIn(self.tokens['refresh_token'], self.output.getvalue())
        self.assertNotIn(self.device['device_code'], self.output.getvalue())
        self.assertIn('"public_actions": 0', self.output.getvalue())

    def test_explicit_scope_selection_is_exact(self):
        scopes = ['user:write:chat', 'channel:manage:broadcast']
        post, _, _ = self.run_connection(scopes=scopes, identity={**self.identity, 'scopes': scopes})
        self.assertEqual(post.call_args_list[0].kwargs['data']['scopes'], 'channel:manage:broadcast user:write:chat')
        self.assertEqual(set(json.loads(self.path.read_text())['scopes']), set(scopes))

    def test_rejects_wrong_identity_app_scope_and_expiry(self):
        for change in [dict(client_id='other'), dict(user_id=''), dict(login='other'),
                       dict(scopes=[]), dict(scopes=[*DEFAULT_SCOPES, 'channel:manage:polls']),
                       dict(expires_in=1), dict(expires_in=True)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_identity({**self.identity, **change}, 'publicapp', 'another_streamer', DEFAULT_SCOPES)
        with self.assertRaises(ValueError):
            validate_identity(self.identity, 'publicapp', 'another_streamer', DEFAULT_SCOPES, user_id='old-id')

    def test_account_mismatch_preserves_prior_credential(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({**self.tokens, **self.identity}))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.run_connection(identity={**self.identity, 'user_id': 'different'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_different_existing_account_is_rejected_before_network(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({**self.identity, 'login': 'someone_else'}))
        with patch('connect_twitch.requests.post') as post, self.assertRaises(ValueError):
            connect('publicapp', 'another_streamer', token_file=self.path)
        post.assert_not_called()

    def test_failed_identity_does_not_save_new_credential(self):
        with self.assertRaises(ValueError):
            self.run_connection(identity={**self.identity, 'login': 'someone_else'})
        self.assertFalse(self.path.exists())

    def test_pending_and_rate_limit_slow_polling(self):
        _, _, slept = self.run_connection(responses=[
            self.response(400, {'message': 'authorization_pending'}),
            self.response(429, {'message': 'slow_down'}), self.response(200, self.tokens)])
        self.assertEqual([x.args[0] for x in slept.call_args_list], [5, 5, 10])

    def test_expired_activation_makes_no_token_request(self):
        with self.assertRaises(ValueError):
            self.run_connection(device={**self.device, 'expires_in': 1})
        self.assertFalse(self.path.exists())

    def test_rejects_unexpected_activation_url(self):
        for uri in ['https://evil.example/activate', 'http://twitch.tv/activate',
                    'https://twitch.tv:444/activate', 'https://user@twitch.tv/activate']:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                self.run_connection(device={**self.device, 'verification_uri': uri})
        self.assertFalse(self.path.exists())

    def test_symlink_output_rejected_without_network(self):
        self.path.parent.mkdir()
        other = self.path.parent / 'other.json'
        other.write_text('untouched')
        self.path.symlink_to(other)
        with patch('connect_twitch.requests.post') as post, self.assertRaises(ValueError):
            connect('publicapp', 'another_streamer', token_file=self.path)
        post.assert_not_called()
        self.assertEqual(other.read_text(), 'untouched')

    def test_invalid_arguments_make_no_network_request(self):
        for args in [('', 'streamer', DEFAULT_SCOPES), ('app', '', DEFAULT_SCOPES),
                     ('app', 'streamer', {'unsupported:scope'})]:
            with self.subTest(args=args), patch('connect_twitch.requests.post') as post, self.assertRaises(ValueError):
                connect(args[0], args[1], token_file=self.path, scopes=args[2])
            post.assert_not_called()

    def test_cli_selects_account_file_and_minimal_default_scopes(self):
        with patch('connect_twitch.connect') as run:
            main(['--client-id', 'publicapp', '--login', 'another_streamer', '--token-file', str(self.path)])
        run.assert_called_once_with('publicapp', 'another_streamer', token_file=self.path, scopes=DEFAULT_SCOPES)

    def test_cli_errors_do_not_leak_provider_details(self):
        stderr = io.StringIO()
        with patch('connect_twitch.connect', side_effect=ValueError('opaque-access-secret')), \
             contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(['--client-id', 'app', '--login', 'streamer'])
        self.assertNotIn('opaque-access-secret', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
