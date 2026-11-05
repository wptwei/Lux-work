"""Theme layers: design tokens, legacy geometry, workspace components."""
from pathlib import Path
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
CSS_PATH = FRONTEND / "lux.css"
STYLE_PATHS = (FRONTEND / "design_tokens.css", CSS_PATH, FRONTEND / "workspace.css", FRONTEND / "home.css", FRONTEND / "assistant.css", FRONTEND / "library.css", FRONTEND / "delivery.css", FRONTEND / "precision.css", FRONTEND / "page_layouts.css")
LUX_ENTERPRISE_CSS = "\n".join(path.read_text(encoding="utf-8") for path in STYLE_PATHS)
CYBER_AURORA_CSS = LUX_ENTERPRISE_CSS
