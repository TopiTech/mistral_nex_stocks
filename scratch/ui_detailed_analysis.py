import glob
import os
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates_dir = os.path.join(base_dir, "templates")
css_dir = os.path.join(base_dir, "static", "css")
js_dir = os.path.join(base_dir, "static", "js")

print("=== DETAILED UI & JS INTERACTION ANALYSIS ===")

# Read all CSS contents
css_files = glob.glob(os.path.join(css_dir, "*.css"))
css_full = ""
css_file_map: dict[str, str] = {}
for f in css_files:
    fname = os.path.basename(f)
    with open(f, "r", encoding="utf-8", errors="ignore") as fp:
        content = fp.read()
        css_full += "\n" + content
        css_file_map[fname] = content

# Extract defined selectors and variables
css_vars = set(re.findall(r"--([\w-]+)\s*:", css_full))
css_classes = set(re.findall(r"\.([a-zA-Z0-9_-]+)", css_full))
css_ids = set(re.findall(r"#([a-zA-Z0-9_-]+)", css_full))

# Read all JS contents
js_files = glob.glob(os.path.join(js_dir, "*.js"))
js_contents: dict[str, str] = {}
for f in js_files:
    fname = os.path.basename(f)
    with open(f, "r", encoding="utf-8", errors="ignore") as fp:
        js_contents[fname] = fp.read()

# Analyze HTML elements and their JS handlers
html_files = glob.glob(os.path.join(templates_dir, "*.html"))

unhandled_elements: list[dict[str, Any]] = []
all_html_buttons: list[dict[str, Any]] = []

for f in html_files:
    fname = os.path.basename(f)
    with open(f, "r", encoding="utf-8", errors="ignore") as fp:
        content = fp.read()

    soup = BeautifulSoup(content, "html.parser")

    for tag in soup.find_all(["button", "a", "input", "div", "span"]):
        if not isinstance(tag, Tag):
            continue
        is_button_like = False
        raw_id = tag.get("id")
        tag_id = str(raw_id) if raw_id and isinstance(raw_id, str) else None

        raw_class = tag.get("class")
        classes: list[str] = []
        if isinstance(raw_class, list):
            classes = [c for c in raw_class if isinstance(c, str)]
        elif isinstance(raw_class, str):
            classes = raw_class.split()

        onclick = tag.get("onclick")
        href = tag.get("href")

        if (
            tag.name in ["button", "a"]
            or "btn" in classes
            or tag.get("role") == "button"
        ):
            is_button_like = True

        if is_button_like:
            has_js_handler = False
            if onclick:
                has_js_handler = True
            elif tag_id:
                for js_code in js_contents.values():
                    if tag_id in js_code:
                        has_js_handler = True
                        break

            if not has_js_handler and classes:
                for c in classes:
                    for js_code in js_contents.values():
                        if (
                            f".{c}" in js_code
                            or f"'{c}'" in js_code
                            or f'"{c}"' in js_code
                        ):
                            has_js_handler = True
                            break
                    if has_js_handler:
                        break

            all_html_buttons.append(
                {
                    "file": fname,
                    "tag": tag.name,
                    "id": tag_id,
                    "classes": classes,
                    "onclick": onclick,
                    "href": href,
                    "text": tag.get_text(strip=True)[:40],
                    "has_handler": has_js_handler,
                }
            )

            if not has_js_handler and tag.name != "a":
                unhandled_elements.append(
                    {
                        "file": fname,
                        "tag": tag.name,
                        "id": tag_id,
                        "classes": classes,
                        "text": tag.get_text(strip=True)[:40],
                    }
                )

print(f"Total Button-like Elements in HTML: {len(all_html_buttons)}")
print(
    f"Potentially Unhandled/Unresponsive Elements: {len(unhandled_elements)}"
)
for u in unhandled_elements:
    print(
        f"  [!] File: {u['file']} | Tag: <{u['tag']}> | ID: {u['id']} | Classes: {u['classes']} | Text: '{u['text']}'"
    )

# Check JS getElementById usages against HTML IDs
print("\n--- Checking all JS getElementById usages ---")
for js_name, js_code in js_contents.items():
    matches = re.findall(
        r'getElementById\s*\(\s*["\']([^"\'\$\{\}]+)["\']\s*\)', js_code
    )
    for element_id in matches:
        found_in_html = False
        for f in html_files:
            with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                if (
                    f'id="{element_id}"' in fp.read()
                    or f"id='{element_id}'" in fp.read()
                ):
                    found_in_html = True
                    break
        if not found_in_html:
            dyn_created = False
            for jcode in js_contents.values():
                if (
                    f'id="{element_id}"' in jcode
                    or f"id='{element_id}'" in jcode
                    or f'id = "{element_id}"' in jcode
                ):
                    dyn_created = True
                    break
            if not dyn_created:
                print(
                    f"  [MISSING ID] {js_name} references id='{element_id}', which is NOT in HTML or dynamic JS!"
                )

# Check undefined CSS classes in templates
print("\n--- Checking Missing CSS classes used in templates ---")
for f in html_files:
    fname = os.path.basename(f)
    with open(f, "r", encoding="utf-8", errors="ignore") as fp:
        content = fp.read()
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        raw_class = tag.get("class")
        classes_list: list[str] = []
        if isinstance(raw_class, list):
            classes_list = [c for c in raw_class if isinstance(c, str)]
        elif isinstance(raw_class, str):
            classes_list = [raw_class]

        for c in classes_list:
            if (
                c not in css_classes
                and not c.startswith("{")
                and c not in ["block", "endblock", "body_class"]
            ):
                print(
                    f"  [MISSING CLASS] File: {fname} | Class: '{c}' | Tag: <{tag.name} id='{tag.get('id')}'>"
                )

print("\n=== ANALYSIS COMPLETE ===")
