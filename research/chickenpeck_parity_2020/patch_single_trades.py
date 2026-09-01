from pathlib import Path
import hashlib
import json

root = Path('research/chickenpeck_parity_2020')
src = root / 'single-trades.original.js'
dst = root / 'single-trades.patched.js'
text = src.read_text(encoding='utf-8')
old = '''    const header = allLines[0];
    const lines  = allLines.slice(1);'''
new = '''    // Header is optional.  Only a PARAM row is metadata; otherwise the first
    // row is a real action and must not be discarded.
    const hasHeader = /^PARAM(?:\\s|,|$)/i.test(allLines[0]);
    const header = hasHeader ? allLines[0] : null;
    const lines  = hasHeader ? allLines.slice(1) : allLines;'''
count = text.count(old)
if count != 1:
    raise RuntimeError(f'Expected exactly one parser block, found {count}')
patched = text.replace(old, new)
dst.write_text(patched, encoding='utf-8')
meta = {
    'source': str(src),
    'patched': str(dst),
    'source_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
    'patched_sha256': hashlib.sha256(patched.encode('utf-8')).hexdigest(),
    'change': 'Do not unconditionally drop the first action row; recognize only PARAM as header.',
}
(root / 'output').mkdir(parents=True, exist_ok=True)
(root / 'output' / 'single_trades_patch_manifest.json').write_text(
    json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8'
)
print(json.dumps(meta, ensure_ascii=False, indent=2))
