"""Static UI audit. Run with: python tools/check_ui_styles.py (requires tinycss2)."""
from pathlib import Path
import json
import re
import sys

import tinycss2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.ui_theme import STYLE_PATHS


def audit():
    issues = []
    stats = []
    for path in STYLE_PATHS:
        source = path.read_text(encoding="utf-8")
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", source)
        if path.name != "design_tokens.css" and literals:
            issues.append(f"{path.name}: colors outside design tokens")
        rules = tinycss2.parse_stylesheet(source, skip_comments=True, skip_whitespace=True)
        count = 0

        def walk(items, scope="root"):
            nonlocal count
            seen = set()
            for item in items:
                if item.type == "error":
                    issues.append(f"{path.name}:{item.source_line}: {item.message}")
                elif item.type == "qualified-rule":
                    count += 1
                    selector = tinycss2.serialize(item.prelude).strip()
                    body = tinycss2.serialize(item.content).strip()
                    if (selector, body) in seen:
                        issues.append(f"{path.name}:{item.source_line}: duplicate rule ({scope})")
                    seen.add((selector, body))
                    for declaration in tinycss2.parse_declaration_list(item.content):
                        if declaration.type == "error":
                            issues.append(f"{path.name}:{declaration.source_line}: {declaration.message}")
                elif item.type == "at-rule" and item.content is not None and item.lower_at_keyword in {"media", "supports", "layer"}:
                    walk(tinycss2.parse_rule_list(item.content), item.lower_at_keyword)

        walk(rules)
        stats.append({"file": str(path.relative_to(ROOT)), "rules": count, "literal_colors": len(literals)})
    return {"passed": not issues, "stylesheets": stats, "issues": issues}


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
