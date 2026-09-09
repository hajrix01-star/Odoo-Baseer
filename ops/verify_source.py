"""Verify accepted source integrity; no imports of Odoo or business data."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'release-source.json').read_text(encoding='utf8'))
expected = manifest['files']
assert expected and manifest['odoo_major'] == 19
actual = {p.relative_to(root).as_posix() for folder in ('custom_addons', 'third_party_addons')
          for p in (root / folder).rglob('*') if p.is_file()
          and '__pycache__' not in p.parts and p.suffix != '.pyc'}
assert actual == set(expected), 'Source file inventory differs'
for rel, digest in expected.items():
    parts = PurePosixPath(rel)
    assert not parts.is_absolute() and '..' not in parts.parts and parts.parts[0] in ('custom_addons', 'third_party_addons')
    path = root.joinpath(*parts.parts)
    assert not path.is_symlink() and path.resolve().is_relative_to(root)
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, 'Hash differs: ' + rel
    if path.suffix == '.py':
        ast.parse(data.decode('utf-8-sig'), filename=rel)
    elif path.suffix == '.xml':
        ET.fromstring(data)
print('PASS: %s accepted source files; Python/XML syntax valid' % len(expected))
