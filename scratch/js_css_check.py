import glob
import os
import re

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
css_dir = os.path.join(base_dir, "static", "css")
js_dir = os.path.join(base_dir, "static", "js")

css_files = glob.glob(os.path.join(css_dir, "*.css"))
css_full = ""
for f in css_files:
    with open(f, 'r', encoding='utf-8', errors='ignore') as fp:
        css_full += "\n" + fp.read()

css_classes = set(re.findall(r'\.([a-zA-Z0-9_-]+)', css_full))
css_vars = set(re.findall(r'--([\w-]+)\s*:', css_full))

js_files = glob.glob(os.path.join(js_dir, "*.js"))

print("=== CHECKING JS DYNAMICALLY CREATED/REFERENCED CLASSES & VARS ===")

# Search for var(--...) in JS
for f in js_files:
    fname = os.path.basename(f)
    with open(f, 'r', encoding='utf-8', errors='ignore') as fp:
        content = fp.read()
    
    # Check var(--...)
    js_css_vars = re.findall(r'var\(\s*--([\w-]+)', content)
    for v in js_css_vars:
        if v not in css_vars:
            print(f"  [MISSING CSS VAR IN JS] File: {fname} | Var: '--{v}'")

    # Check classList.add('...') or classList.remove('...') or classList.toggle('...')
    cl_matches = re.findall(r'classList\.(?:add|remove|toggle|contains)\s*\(\s*["\']([^"\'\s]+)["\']\s*\)', content)
    for c in cl_matches:
        if c not in css_classes and not c.startswith('js-') and not c.startswith('active') and not c.startswith('hidden'):
            print(f"  [MISSING CLASS IN JS CLASSLIST] File: {fname} | Class: '{c}'")

    # Check className = '...' or querySelector('....')
    qs_matches = re.findall(r'querySelector(?:All)?\s*\(\s*["\']\.([^"\'\s>\.,:\[\]\(\)]+)["\']\s*\)', content)
    for c in qs_matches:
        if c not in css_classes:
            print(f"  [MISSING CLASS IN JS QUERYSELECTOR] File: {fname} | Class: '{c}'")

print("=== CHECK COMPLETED ===")
