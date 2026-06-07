"""
test_cookie_parser.py - 验证 4 种 cookie 字符串格式都能被 textarea 解析器正确解析
"""
import re

# 复制 web_app.py INDEX_HTML 里的 parseCookieInput 函数（保持同步）
PARSE_JS = r"""
var text = (raw || '').trim();
var out = {};
if (!text) return out;
if (text[0] === '[' || text[0] === '{') {
    try {
        var arr = JSON.parse(text);
        var list = Array.isArray(arr) ? arr : [arr];
        for (var i = 0; i < list.length; i++) {
            var it = list[i];
            if (it && typeof it === 'object' && it.name && it.value != null) {
                out[String(it.name).trim()] = String(it.value);
            }
        }
    } catch (e) {}
    if (Object.keys(out).length) return out;
}
if (text.indexOf('\n') === -1 && text.indexOf(';') !== -1) {
    text.split(';').forEach(function (p) {
        p = p.replace(/^\s+|\s+$/g, '');
        if (!p) return;
        var eq = p.indexOf('=');
        if (eq > 0) out[p.slice(0, eq).trim()] = p.slice(eq + 1).trim();
    });
    return out;
}
var lines = text.split(/\r?\n/);
for (var li = 0; li < lines.length; li++) {
    var line = lines[li].trim();
    if (!line || line.charAt(0) === '#') continue;
    if (line.indexOf('\t') !== -1) {
        var parts = line.split('\t');
        if (parts.length >= 7 && parts[0].indexOf('.') !== -1) {
            out[parts[5]] = parts.slice(6).join('\t');
        } else if (parts.length >= 2) {
            out[parts[0].trim()] = parts.slice(1).join('\t');
        }
    } else {
        var eq = line.indexOf('=');
        if (eq > 0) {
            out[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
        }
    }
}
return out;
"""


def js_to_python_translate(js_func_body: str) -> str:
    """粗略把 JS 函数体翻译成 Python 等价实现用于测试"""
    py = js_func_body
    py = re.sub(r'var\s+(\w+)\s*=', r'\1 =', py)
    py = py.replace('===', '==')
    py = py.replace('!==', '!=')
    py = py.replace('&&', ' and ')
    py = py.replace('||', ' or ')
    py = re.sub(r'!\s*(\w+)', r'not \1', py)
    py = py.replace('Object.keys', 'list')
    py = py.replace('Array.isArray', 'isinstance')
    py = py.replace('.length', '')
    py = re.sub(r'\.indexOf\(', '.find(', py)
    py = re.sub(r'\.charAt\((\d+)\)', r'[\1]', py)
    py = re.sub(r"\.slice\(([^)]+)\)", r"[\\1]", py)
    py = py.replace('return', '    return')
    return f"def parse(raw):\n{py}"


def parse_py(raw: str) -> dict:
    """Python 翻译版（与 JS 保持一致）"""
    text = (raw or '').strip()
    out = {}
    if not text:
        return out
    if text[0] in ('[', '{'):
        import json
        try:
            arr = json.loads(text)
            lst = arr if isinstance(arr, list) else [arr]
            for it in lst:
                if it and isinstance(it, dict) and it.get('name') and it.get('value') is not None:
                    out[str(it['name']).strip()] = str(it['value'])
        except Exception:
            pass
        if out:
            return out
    # 单行 cookie 头
    if '\n' not in text and ';' in text:
        for part in text.split(';'):
            part = part.strip()
            if not part:
                continue
            eq = part.find('=')
            if eq > 0:
                out[part[:eq].strip()] = part[eq+1:].strip()
        return out
    # 行级
    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '\t' in line:
            parts = line.split('\t')
            if len(parts) >= 7 and '.' in parts[0]:
                # Netscape
                out[parts[5]] = '\t'.join(parts[6:])
            elif len(parts) >= 2:
                # 通用 Tab
                out[parts[0].strip()] = '\t'.join(parts[1:])
        else:
            eq = line.find('=')
            if eq > 0:
                out[line[:eq].strip()] = line[eq+1:].strip()
    return out


def expect(name, got, want):
    ok = got == want
    mark = '✓' if ok else '✗'
    print(f"  {mark} {name}")
    if not ok:
        print(f"    want={want}")
        print(f"    got ={got}")
    return ok


cases = [
    # 格式 1: 标准 cookie 字符串（一行）
    ("标准单行",
     "__client_id=abc123; _uid=42; C3VK=xyz999",
     {'__client_id': 'abc123', '_uid': '42', 'C3VK': 'xyz999'}),
    # 格式 2: 标准 cookie 字符串（多行）
    ("标准多行",
     "__client_id=abc123\n_uid=42\nC3VK=xyz999",
     {'__client_id': 'abc123', '_uid': '42', 'C3VK': 'xyz999'}),
    # 格式 3: DevTools Tab 复制 (name\tvalue)
    ("Tab 双列",
     "__client_id\tabc123\n_uid\t42\nC3VK\txyz999",
     {'__client_id': 'abc123', '_uid': '42', 'C3VK': 'xyz999'}),
    # 格式 4: Netscape 导出
    ("Netscape 导出",
     "# Netscape HTTP Cookie File\n"
     "# https://curl.haxx.se/rfc/cookie-spec.html\n"
     "# This is a generated file! Do not edit.\n"
     "\n"
     ".luogu.com.cn\tTRUE\t/\tFALSE\t0\t__client_id\tabc123\n"
     ".luogu.com.cn\tTRUE\t/\tFALSE\t0\t_uid\t42\n"
     ".luogu.com.cn\tTRUE\t/\tFALSE\t0\tC3VK\txyz999\n",
     {'__client_id': 'abc123', '_uid': '42', 'C3VK': 'xyz999'}),
    # 格式 5: JSON 数组
    ("JSON 数组",
     '[{"name":"__client_id","value":"abc123"},{"name":"_uid","value":"42"},{"name":"C3VK","value":"xyz999"}]',
     {'__client_id': 'abc123', '_uid': '42', 'C3VK': 'xyz999'}),
    # 格式 6: 字符串中含特殊字符（=, ;）
    # 注：若 value 含未编码 ;，按 ; 切分会被切碎。RFC 6265 要求 cookie value
    # 编码后再 set，所以 DevTools 显示的 value 不会有裸 ;。本用例模拟"单行 +
    # 有 ; 分隔 + value 含 ="的常见情况，不含裸 ;。
    ("value 含 =",
     "__client_id=abc=def; _uid=42; C3VK=xyz999",
     {'__client_id': 'abc=def', '_uid': '42', 'C3VK': 'xyz999'}),
    # 格式 7: 混合杂质行（其他 cookie 也粘进来了）
    ("杂质行",
     "other_cookie=garbage\n__client_id=abc123\n_uid=42\nC3VK=xyz999",
     {'other_cookie': 'garbage', '__client_id': 'abc123', '_uid': '42', 'C3VK': 'xyz999'}),
    # 格式 8: 空
    ("空字符串", "", {}),
    # 格式 9: 含 C3VK value 带 Tab（Netscape 截断时）
    ("Tab value 不可拆",
     "__client_id\tfoo\tbar",
     {'__client_id': 'foo\tbar'}),
]

print("=" * 60)
print("test_cookie_parser.py")
print("=" * 60)
all_ok = True
for name, raw, want in cases:
    got = parse_py(raw)
    if not expect(name, got, want):
        all_ok = False

# 同步：JS 端跑同样的 case（用 dict 比较，避免 JSON 顺序/repr 差异）
print("\n--- 用 Node 跑同一份 JS 验证一致性 ---")
import subprocess, json as _json
for name, raw, want in cases:
    js = f"function parse(raw) {{ {PARSE_JS} }}; console.log(JSON.stringify(parse({raw!r})))"
    r = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=5)
    try:
        js_dict = _json.loads(r.stdout.strip())
    except Exception:
        js_dict = {'_raw': r.stdout, '_err': r.stderr[:200]}
    py_dict = parse_py(raw)
    ok = js_dict == py_dict
    mark = '✓' if ok else '✗'
    print(f"  {mark} {name}: js={js_dict} py={py_dict}")
    if not ok:
        all_ok = False

print("\n" + ("ALL OK ✓" if all_ok else "FAILED ✗"))
exit(0 if all_ok else 1)
