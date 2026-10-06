"""Publication gate fixtures are invented; never load private configuration."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit_publication as audit


class PublicationTests(unittest.TestCase):
    def rules(self, data, name='scripts/example.py', mode='100644', private=()):
        return {v['rule'] for v in audit.scan(name, mode, data, private)}

    def test_blocks_private_data_and_compiled_outputs(self):
        for name in ('private/test.json', 'config.json', 'captures/test.txt',
                     'messages.jsonl', 'note.exe', 'state.db', '.env.local'):
            with self.subTest(name=name):
                self.assertIn('personal_data_or_build_output', self.rules(b'{}', name))

    def test_blocks_secrets_without_echoing_the_secret(self):
        secret = ('sk-' + 'Z' * 30).encode()
        result = audit.scan('scripts/example.py', '100644', b'key = ' + secret)
        self.assertTrue(result)
        self.assertNotIn(secret.decode(), str(result))
        self.assertIn('credential', {v['rule'] for v in result})

    def test_known_private_values_also_checked_in_binary_assets(self):
        name, digest = next(iter(audit.BINARY_HASHES.items()))
        self.assertIn('known_private_value', self.rules(b'private-fixture', name, private=(b'private-fixture',)))
        self.assertIn('unreviewed_binary_asset', self.rules(b'private-fixture', name))

    def test_rejects_unreviewed_binary_and_symlink(self):
        self.assertIn('unreviewed_binary', self.rules(b'\xff\x00'))
        self.assertIn('non_regular_file', self.rules(b'../private', mode='120000'))

    def test_public_placeholder_and_synthetic_key_are_allowed(self):
        self.assertEqual(self.rules(b'api_key = "synthetic-key"'), set())
        self.assertEqual(self.rules(('account = "wxid_' + 'YOURWXID"').encode()), set())
        self.assertEqual(self.rules(b'{}', 'config.example.json'), set())

    def test_private_path_and_account_candidates_are_blocked(self):
        value = ('C:/' + 'Users/' + 'fictional-person/data').encode()
        self.assertIn('private_absolute_path', self.rules(value))
        value = ('wxid_' + 'fictionalaccount').encode()
        self.assertIn('wechat_identifier', self.rules(value))

    def test_licenses_keep_public_author_attribution(self):
        address = ('author' + '@public-author.invalid').encode()
        self.assertEqual(self.rules(address, 'licenses/LICENSE.example'), set())
        self.assertIn('personal_email_candidate', self.rules(address))


if __name__ == '__main__':
    unittest.main()
