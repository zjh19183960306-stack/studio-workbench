"""「固定提示词」覆盖：/root/autodl-tmp/prompt_overrides.json。默认规则文本不改，只在存在覆盖时替换。
结构：{"recipes": {"character:standard": ["第1张完整提示词", ...]}, "locks": {"character": ["锁定(正面)", "负面"], "item": [...]}}"""
import json, os
from pathlib import Path
from aiohttp import web
FILE = Path('/root/autodl-tmp/prompt_overrides.json')
KEYS = [f'{k}:{m}' for k in ('character', 'item') for m in ('standard', 'advanced')]

def load():
    try: d = json.loads(FILE.read_text())
    except Exception: return {}
    return d if isinstance(d, dict) else {}

def recipe_prompts(kind, mode):
    v = load().get('recipes', {}).get(f'{kind}:{mode}')
    return v if isinstance(v, list) else None

def lock(kind):
    v = load().get('locks', {}).get(kind)
    return v if isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v) else None

async def get_rules(request):
    import recipes_cloud, sys; server = sys.modules["__main__"] if hasattr(sys.modules["__main__"], "LOCKS") else __import__("server")
    out = {'recipes': {}, 'locks': {}, 'has_overrides': FILE.exists()}
    for key in KEYS:
        k, m = key.split(':')
        defaults = recipes_cloud.base_recipes(m, k); ov = recipe_prompts(k, m)
        out['recipes'][key] = [{'label': r['label'], 'default': r['prompt'],
                                'prompt': ov[i] if ov and i < len(ov) and isinstance(ov[i], str) and ov[i].strip() else r['prompt']}
                               for i, r in enumerate(defaults)]
    for k in ('character', 'item'):
        d = list(server.LOCKS[k]); ov = lock(k)
        out['locks'][k] = {'default': d, 'value': ov or d}
    return web.json_response(out)

async def save_rules(request):
    import recipes_cloud, sys; server = sys.modules["__main__"] if hasattr(sys.modules["__main__"], "LOCKS") else __import__("server")
    data = await request.json(); new = {'recipes': {}, 'locks': {}}
    for key, vals in (data.get('recipes') or {}).items():
        if key not in KEYS or not isinstance(vals, list): raise ValueError('提示词分组无效')
        k, m = key.split(':'); n = len(recipes_cloud.base_recipes(m, k))
        if len(vals) != n or not all(isinstance(x, str) and len(x) <= 4000 for x in vals): raise ValueError(f'{key} 条数或内容无效')
        defaults = [r['prompt'] for r in recipes_cloud.base_recipes(m, k)]
        if [x.strip() for x in vals] != [x.strip() for x in defaults]: new['recipes'][key] = vals
    for k, v in (data.get('locks') or {}).items():
        if k not in ('character', 'item') or not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) and len(x) <= 4000 for x in v)):
            raise ValueError('锁定规则格式无效')
        if list(v) != list(server.LOCKS[k]): new['locks'][k] = v
    if not new['recipes'] and not new['locks']:
        FILE.unlink(missing_ok=True); return web.json_response({'ok': True, 'saved': 0})
    tmp = FILE.with_suffix('.tmp'); tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1)); os.replace(tmp, FILE)
    return web.json_response({'ok': True, 'saved': len(new['recipes']) + len(new['locks'])})

async def reset_rules(request):
    FILE.unlink(missing_ok=True)
    return web.json_response({'ok': True})

def setup(app):
    app.add_routes([web.get('/api/rules', get_rules), web.post('/api/rules', save_rules), web.post('/api/rules/reset', reset_rules)])
