import glob
import os
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates_dir = os.path.join(base_dir, "templates")
css_dir = os.path.join(base_dir, "static", "css")
js_dir = os.path.join(base_dir, "static", "js")

print("=== START UI STATIC ANALYSIS ===")

# 1. Collect CSS selectors & Variables
css_classes: set[str] = set()
css_ids: set[str] = set()
css_vars_defined: set[str] = set()
css_vars_used: set[str] = set()

css_files = glob.glob(os.path.join(css_dir, "*.css"))
for filepath in css_files:
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Extract defined CSS variables (--var-name: ...)
    defined_vars = re.findall(r"--([\w-]+)\s*:", content)
    for v in defined_vars:
        css_vars_defined.add(v)

    # Extract used CSS variables var(--var-name)
    used_vars = re.findall(r"var\(\s*--([\w-]+)", content)
    for v in used_vars:
        css_vars_used.add(v)

    # Extract classes (.class-name)
    classes = re.findall(r"\.([a-zA-Z0-9_-]+)", content)
    for c in classes:
        if not c.isdigit() and not c.endswith("%"):
            css_classes.add(c)

    # Extract IDs (#id-name)
    ids = re.findall(r"#([a-zA-Z0-9_-]+)", content)
    for i in ids:
        css_ids.add(i)

print(f"CSS Defined Classes: {len(css_classes)}")
print(f"CSS Defined IDs: {len(css_ids)}")
print(f"CSS Defined Variables: {len(css_vars_defined)}")

# Check undefined CSS variables in CSS files
undefined_css_vars = css_vars_used - css_vars_defined
print(f"\n[?] CSS Variables used but not defined: {undefined_css_vars}")

# 2. Collect HTML IDs, Classes, Buttons/Interactive Elements
html_ids: set[str] = set()
html_classes: set[str] = set()
html_interactive_elements: list[dict[str, Any]] = []

html_files = glob.glob(os.path.join(templates_dir, "*.html"))
for filepath in html_files:
    filename = os.path.basename(filepath)
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    soup = BeautifulSoup(content, "html.parser")

    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        raw_id = tag.get("id")
        if raw_id and isinstance(raw_id, str):
            html_ids.add(raw_id)

        raw_class = tag.get("class")
        tag_classes: list[str] = []
        if isinstance(raw_class, list):
            tag_classes = [c for c in raw_class if isinstance(c, str)]
        elif isinstance(raw_class, str):
            tag_classes = raw_class.split()

        for c in tag_classes:
            html_classes.add(c)

        # Interactive elements check
        tag_name = str(tag.name)
        onclick_val = tag.get("onclick")
        href_val = tag.get("href")

        is_interactive = (
            tag_name in ["button", "a", "input", "select", "textarea"]
            or "btn" in tag_classes
            or "clickable" in tag_classes
            or bool(onclick_val)
            or bool(href_val)
        )

        if is_interactive:
            html_interactive_elements.append(
                {
                    "file": filename,
                    "tag": tag_name,
                    "id": raw_id,
                    "class": tag_classes,
                    "type": tag.get("type"),
                    "onclick": onclick_val,
                    "href": href_val,
                    "text": tag.get_text(strip=True)[:30],
                }
            )

print(f"HTML Unique IDs: {len(html_ids)}")
print(f"HTML Unique Classes: {len(html_classes)}")
print(f"HTML Interactive Elements: {len(html_interactive_elements)}")

# 3. Collect JS ID references, Class references, and event bindings
js_ids_referenced: set[tuple[str, str]] = set()
js_classes_referenced: set[tuple[str, str]] = set()

js_files = glob.glob(os.path.join(js_dir, "*.js"))
js_contents: dict[str, str] = {}
for filepath in js_files:
    filename = os.path.basename(filepath)
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
        js_contents[filename] = content

    ids_get = re.findall(
        r'getElementById\s*\(\s*["\']([^"\'\$\{\}]+)["\']\s*\)', content
    )
    for i in ids_get:
        js_ids_referenced.add((filename, i))

    ids_qs = re.findall(
        r'querySelector(?:All)?\s*\(\s*["\']#([^"\'\s>\.,:\[\]\(\)]+)["\']\s*\)',
        content,
    )
    for i in ids_qs:
        js_ids_referenced.add((filename, i))

    classes_cl = re.findall(
        r'classList\.(?:add|remove|contains|toggle)\s*\(\s*["\']([^"\'\s]+)["\']\s*\)',
        content,
    )
    for c in classes_cl:
        js_classes_referenced.add((filename, c))

    classes_qs = re.findall(
        r'querySelector(?:All)?\s*\(\s*["\']\.([^"\'\s>\.,:\[\]\(\)]+)["\']\s*\)',
        content,
    )
    for c in classes_qs:
        js_classes_referenced.add((filename, c))

# 4. Check for JS references to non-existent HTML IDs
missing_ids_in_html: list[tuple[str, str]] = []
all_html_ids_clean = set(html_ids)
for file_name, js_id in js_ids_referenced:
    if js_id not in all_html_ids_clean:
        missing_ids_in_html.append((file_name, js_id))

print(
    f"\n[?] JS Referencing IDs not found in static HTML templates (Total {len(missing_ids_in_html)}):"
)
for fname, i in missing_ids_in_html[:20]:
    print(f"  - {fname}: '{i}'")

# 5. Check HTML/JS classes not defined in CSS
missing_classes_in_css: set[str] = set()
for c in html_classes:
    if "{" not in c and "}" not in c and c not in css_classes:
        missing_classes_in_css.add(c)

print(
    f"\n[?] HTML Classes NOT defined in CSS (Total {len(missing_classes_in_css)}):"
)
for c in sorted(missing_classes_in_css):
    print(f"  - {c}")

print("\n=== END STATIC ANALYSIS ===")
