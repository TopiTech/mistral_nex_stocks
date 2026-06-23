"""
Split index.js into individual JS files by #region markers.

This script reads an index.js file and splits it into separate
files based on #region comments. Each region group is mapped
to a specific output file.
"""

import os
import re
from pathlib import Path

base_dir = str(Path(__file__).resolve().parent / "static" / "js")
index_js_path = os.path.join(base_dir, "index.js")

with open(index_js_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

regions = []
current_region_name = None
current_region_lines = []

for line in lines:
    m = re.match(r"^\s*//\s*#region\s+(.*)", line)
    if m:
        if current_region_name:
            regions.append((current_region_name, current_region_lines))
        current_region_name = m.group(1).strip()
        current_region_lines = [line]
    else:
        if current_region_name:
            current_region_lines.append(line)
        else:
            # Lines before the first region (if any)
            pass

if current_region_name:
    regions.append((current_region_name, current_region_lines))

# Group regions into files
file_mappings = {
    "utils.js": ["Security Utilities", "Logger"],
    "state.js": ["UI Core Configuration", "Registry & Cache", "Cache Eviction"],
    "chart.js": ["Chart.js Plugins", "Stock History & Prefetch", "Stock Chart Rendering"],
    "ui.js": ["Detail Panel Management", "DOM Component Creation", "Main Stock List Rendering", "Portfolio Management", "Portfolio Logic"],
    "api.js": ["SSE & Real-time Integration", "News & Trends"],
    "index_main.js": ["Initialization"]
}

written_files = []

for filename, region_names in file_mappings.items():
    out_path = os.path.join(base_dir, filename)
    with open(out_path, "w", encoding="utf-8") as out_f:
        for r_name in region_names:
            # Find the region
            found = False
            for name, r_lines in regions:
                if name == r_name:
                    out_f.writelines(r_lines)
                    found = True
                    break
            if not found:
                print(f"Warning: Region '{r_name}' not found!")
    written_files.append(filename)
    print(f"Written {filename}")

print("Done splitting index.js.")
