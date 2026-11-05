"""检查主题文本与背景的 WCAG 对比度。运行：python tools/check_design_tokens.py。"""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1]

def luminance(value):
    values = [int(value[i:i+2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
    return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))

def ratio(a, b):
    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + 0.05) / (lo + 0.05)

def main():
    text = (ROOT / 'frontend/design_tokens.css').read_text(encoding='utf-8')
    colors = dict(re.findall(r'--ds-([a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})', text))
    # Resolve these component-state mixtures from the actual tokens, not copied constants.
    for token in ('control-border', 'nav-selected', 'blue-hover'):
        expression = re.search(
            r'--ds-' + token + r':color-mix\(in srgb,var\(--ds-([a-z-]+)\) (\d+(?:\.\d+)?)%,var\(--ds-([a-z-]+)\)\)',
            text,
        )
        if expression is None:
            raise ValueError(f'Unsupported color token: {token}')
        first, percent, second = expression.groups()
        weight = float(percent) / 100
        colors[token] = '#' + ''.join(
            f'{round(int(colors[first][i:i+2], 16) * weight + int(colors[second][i:i+2], 16) * (1 - weight)):02x}'
            for i in (1, 3, 5)
        )
    pairs = [(f, b, 4.5, 'text') for b in ('bg', 'sidebar', 'surface', 'elevated')
             for f in ('text', 'muted', 'cyan', 'success', 'warning', 'error')]
    pairs += [('bg', b, 4.5, 'filled-button-text') for b in ('blue', 'cyan', 'success', 'warning', 'error')]
    # Accent blue is used as a fill or graphical indicator, not small text on elevated surfaces.
    pairs += [('blue', b, 3.0, 'graphical-indicator') for b in ('bg', 'sidebar', 'surface', 'elevated')]
    pairs += [('control-border', 'sidebar', 3.0, 'input-boundary'),
              ('text', 'nav-selected', 4.5, 'selected-navigation-text'),
              ('bg', 'blue-hover', 4.5, 'hover-button-text')]
    results = [{'foreground': f, 'background': b, 'usage': usage, 'ratio': round(ratio(colors[f], colors[b]), 2),
                'threshold': minimum, 'passed': ratio(colors[f], colors[b]) >= minimum}
               for f, b, minimum, usage in pairs]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item['passed'] for item in results) else 1

if __name__ == '__main__':
    raise SystemExit(main())
