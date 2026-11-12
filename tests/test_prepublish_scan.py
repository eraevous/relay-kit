"""Synthetic CLI regressions: fixture values are assembled, never real credentials."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCAN = pathlib.Path(__file__).resolve().parents[1] / 'tools' / 'prepublish_scan.py'


class ScanTests(unittest.TestCase):
    def scan(self, files, names=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            for name, text in files.items():
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding='utf-8')
            subprocess.run(['git', '-C', tmp, 'add', '.'], check=True)
            if names:
                (root / '.private-names').write_text(names, encoding='utf-8')
            return subprocess.run([sys.executable, '-I', str(SCAN), '--root', tmp],
                                  capture_output=True, text=True, timeout=10)

    def test_redacts_every_category(self):
        """prepublish: findings never echo credential, email, name or chat values"""
        values = ['sk-' + 'a' * 32, 'ghp_' + 'b' * 36, 'eyJ' + 'c' * 40,
                  'someone' + '@' + 'example.net',
                  'https://' + 'chatgpt.com/c/' + '12345678-abcd', 'SyntheticPrivateName']
        result = self.scan({'fixture.txt': '\n'.join(values)}, names=values[-1])
        self.assertEqual(result.returncode, 1)
        self.assertIn('6 finding(s)', result.stdout)
        for value in values:
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertIn('[redacted]', result.stdout)

    def test_new_credential_shapes(self):
        """prepublish: recognizes fine-grained PATs, GitHub tokens, AWS IDs and PEM keys"""
        values = ['github_' + 'pat_' + 'a' * 82,
                  *['gh' + kind + '_' + 'b' * 36 for kind in 'ousr'],
                  *[prefix + 'A' * 16 for prefix in ('AK' + 'IA', 'AS' + 'IA')],
                  *['-----BEGIN ' + kind + 'PRIVATE KEY-----' for kind in
                    ('', 'RSA ', 'EC ', 'DSA ', 'OPENSSH ', 'ENCRYPTED ')]]
        for value in values:
            with self.subTest(kind=values.index(value)):
                result = self.scan({'fixture.txt': value})
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(value, result.stdout + result.stderr)
                self.assertIn('1 finding(s)', result.stdout)

    def test_space_and_control_character_paths(self):
        """prepublish: scans whitespace filenames and escapes log-control characters"""
        result = self.scan({'a file\nforged.txt': 'sk-' + 'a' * 32})
        self.assertEqual(result.returncode, 1)
        self.assertIn('a file\\nforged.txt', result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_exemptions_and_benign_strings(self):
        """prepublish: retains public repo, trailer and license-name exemptions"""
        result = self.scan({
            'pyproject.toml': 'Homepage = "https://github.com/example/relay-kit"',
            'README.md': 'https://github.com/example/relay-kit' + '@' + 'v1.2.3\n'
                         + 'noreply' + '@anthropic.com\n'
                         + 'github_pat_short AKIAshort -----BEGIN PUBLIC KEY-----',
            'LICENSE': 'SyntheticPrivateName', 'CLA.md': 'SyntheticPrivateName',
        }, names='SyntheticPrivateName')
        self.assertEqual(result.returncode, 0)
        self.assertIn('0 finding(s)', result.stdout)


if __name__ == '__main__':
    unittest.main()
