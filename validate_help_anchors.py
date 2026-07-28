#!/usr/bin/env python3
"""
validate_help_anchors.py
========================
Validates that all anchor tags mapped in `help_browser.py` exist in the
generated HTML from `IsoWorks_pyLIMS_User_Manual.md`.

Usage:
    python validate_help_anchors.py
"""
import os
import sys
import ast
import re

try:
    import markdown
except ImportError:
    print("Error: The 'markdown' package is required. Run: pip install markdown")
    sys.exit(1)

HELP_BROWSER_PATH = "help_browser.py"
MANUAL_PATH = os.path.join("Manuals", "IsoWorks_pyLIMS_User_Manual.md")

def get_help_topics(filepath: str) -> dict:
    """Safely extracts the HELP_TOPICS dictionary using the AST."""
    if not os.path.exists(filepath):
        print(f"Error: Could not find {filepath}")
        sys.exit(1)
        
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    topics = {}
    for node in tree.body:
        # Handle annotated assignments: HELP_TOPICS: dict[str, str] = {...}
        if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', '') == 'HELP_TOPICS':
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    topics[k.value] = v.value
        # Handle standard assignments: HELP_TOPICS = {...}
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, 'id', '') == 'HELP_TOPICS':
                    for k, v in zip(node.value.keys, node.value.values):
                        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                            topics[k.value] = v.value
    return topics

def main():
    print(f"Parsing {HELP_BROWSER_PATH} for registered help topics...")
    help_topics = get_help_topics(HELP_BROWSER_PATH)
    
    if not help_topics:
        print("Error: Could not extract HELP_TOPICS dictionary.")
        sys.exit(1)

    if not os.path.exists(MANUAL_PATH):
        print(f"Error: Manual not found at {MANUAL_PATH}")
        sys.exit(1)

    with open(MANUAL_PATH, "r", encoding="utf-8") as f:
        md_text = f.read()

    # Render HTML using the exact same configuration as help_browser.py
    html = markdown.markdown(md_text, extensions=["toc", "tables", "fenced_code"])
    generated_anchors = set(re.findall(r'<h[1-6][^>]*id="([^"]+)"', html))

    errors = 0
    print(f"\nValidating {len(help_topics)} topics against the Markdown manual...\n")
    for key, expected_anchor in help_topics.items():
        if expected_anchor in generated_anchors:
            print(f"✅ {key:30} -> #{expected_anchor}")
        else:
            print(f"❌ {key:30} -> #{expected_anchor}  (ANCHOR NOT FOUND IN MANUAL)")
            errors += 1

    print("\n" + "=" * 60)
    if errors == 0:
        print("SUCCESS: All help topics map to valid markdown headers!")
        sys.exit(0)
    else:
        print(f"FAILED: {errors} broken links found. Please fix HELP_TOPICS or the Markdown file.")
        sys.exit(1)

if __name__ == '__main__':
    main()