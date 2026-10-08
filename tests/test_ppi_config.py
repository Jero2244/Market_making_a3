import tempfile
import unittest
from pathlib import Path
from market_making.ppi.config import KEYS, SDK_CLIENT_DEFAULTS, ensure_env, load_credentials


class ConfigTests(unittest.TestCase):
    def test_append_preserves_original_and_never_exposes_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            original = b'# original\r\nPRIMARY_USER=untouched\r\nPPI_API_KEY=private-sentinel'
            path.write_bytes(original)
            result = ensure_env(path, {KEYS[1]: 'private-second'})
            self.assertTrue(path.read_bytes().startswith(original))
            self.assertEqual(result[KEYS[0]], 'present')
            self.assertEqual(result[KEYS[1]], 'present')
            self.assertEqual(result[KEYS[2]], 'present')
            contents = path.read_bytes()
            ensure_env(path, {})
            self.assertEqual(contents, path.read_bytes())
            self.assertNotIn('private', repr(load_credentials(path, {})))
            self.assertNotIn('private', str(result))

    def test_precedence_and_placeholders(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('PPI_API_KEY=your_key\nPPI_API_SECRET="local"\n')
            local = load_credentials(path, {})
            self.assertEqual(local.statuses()[KEYS[0]], 'placeholder')
            self.assertFalse(local.ready)
            self.assertEqual(load_credentials(path, {KEYS[1]: ''}).statuses()[KEYS[1]], 'missing')

    def test_environment_injection_not_written(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            ensure_env(path, {KEYS[0]: 'bad\nINJECTED=yes'})
            self.assertNotIn('INJECTED', path.read_text())

    def test_key_and_secret_use_sdk_defaults_without_writing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            original = 'PPI_API_KEY=private-key\nPPI_API_SECRET=private-secret\n'
            path.write_text(original)
            credentials = load_credentials(path, {})
            self.assertTrue(credentials.ready)
            for key, value in SDK_CLIENT_DEFAULTS.items():
                self.assertEqual(credentials.values[key], value)
            self.assertEqual(path.read_text(), original)

    def test_blank_client_ids_default_but_explicit_overrides_win(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('PPI_AUTHORIZED_CLIENT=\nPPI_CLIENT_KEY=" "\n')
            credentials = load_credentials(path, {})
            for key, value in SDK_CLIENT_DEFAULTS.items():
                self.assertEqual(credentials.values[key], value)
            overrides = {KEYS[2]: 'custom-client', KEYS[3]: 'custom-key'}
            credentials = load_credentials(path, overrides)
            for key, value in overrides.items():
                self.assertEqual(credentials.values[key], value)
            self.assertEqual(load_credentials(path, {KEYS[2]: 'your_client'}).statuses()[KEYS[2]], 'placeholder')
