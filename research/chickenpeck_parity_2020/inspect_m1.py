from pathlib import Path
import hashlib

p = Path('bundle/data/M1.txt')
b = p.read_bytes()
print('size', len(b))
print('sha256', hashlib.sha256(b).hexdigest())
print('head_hex', b[:200].hex())
for enc in ('utf-8-sig','utf-8','cp950','big5','latin1'):
    try:
        s=b.decode(enc)
        print('encoding',enc,'OK')
        break
    except Exception as e:
        print('encoding',enc,'FAIL',repr(e))
else:
    raise RuntimeError('decode failed')
print('counts', {'\\n':s.count('\n'),'\\r':s.count('\r'),'comma':s.count(','),'tab':s.count('\t'),'space':s.count(' ')})
# normalize all likely record separators
norm=s.replace('\r\n','\n').replace('\r','\n')
lines=[x for x in norm.split('\n') if x.strip()]
print('lines',len(lines))
for i,line in enumerate(lines[:30]):
    print(f'LINE{i+1}:',repr(line[:500]))
Path('research/chickenpeck_parity_2020/inspection.txt').write_text('\n'.join(lines[:200]),encoding='utf-8')
