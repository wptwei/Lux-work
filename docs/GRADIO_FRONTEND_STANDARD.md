# LUX 的 Gradio 前端工程规范

## 1. 采用方式

当前工作台继续使用 Gradio 6.x `Blocks` 作为运行框架，`app.py` 管理页面结构和业务事件，`frontend/lux.css` 管理视觉样式，`core/ui_theme.py` 负责加载 CSS。

```python
from core.ui_theme import LUX_ENTERPRISE_CSS

# 现有应用仍在 launch 阶段注入主题、CSS 与 JS
# theme=THEME, css=LUX_ENTERPRISE_CSS, js=UI_ENHANCEMENTS_JS
```

Gradio 官方文档将主题系统作为基础样式入口，并支持通过 `css=` 或 `css_paths=` 增加自定义样式。官方也提醒：依赖 Gradio 内部 HTML DOM 的选择器可能随版本变化，因此项目优先使用自定义 `elem_classes` / `elem_id` 和业务 HTML 类名，并在升级后重新测试。

参考：
- <https://gradio.app/guides/custom-CSS-and-JS>
- <https://gradio.app/guides/theming-guide>

## 2. 文件职责

| 文件 | 职责 | 禁止事项 |
|---|---|---|
| `app.py` | Gradio 页面、事件绑定、业务流程 | 不再内嵌第二份 CSS |
| `core/ui_theme.py` | 读取 CSS 文件、提供主题常量 | 不复制/拼接大量样式 |
| `frontend/lux.css` | 设计令牌、组件、状态、响应式与打印样式 | 不写业务逻辑 |
| `FRONTEND_DESIGN_SKILL.md` | 设计与验收标准 | 不用“看起来不错”代替测试证据 |
| `tests/test_frontend_design.py` | 检查样式源、令牌和验收钩子 | 不替代浏览器视觉测试 |

## 3. CSS 选择器策略

优先级从高到低：

1. 业务 HTML 的语义类名，如 `.incident-result-head`、`.incident-flow`。
2. Gradio 组件显式设置的 `elem_classes` / `elem_id`。
3. Gradio 稳定的公共根类，如 `.gradio-container`。
4. `role` / 内部 DOM 层级选择器仅用于必要场景，且升级版本后必须回归验证。

禁止依赖自动生成的类名或复杂 DOM 层级来承载关键布局。业务布局应由 `Row` / `Column` 与具名类名协作完成。

## 4. 样式组织顺序

`frontend/lux.css` 按以下顺序维护：

1. 设计令牌与全局基线
2. 通用表面、导航与排版
3. 表单、按钮、焦点与禁用状态
4. 业务组件（先异常事件处置，再其他模块）
5. 响应式断点
6. 打印与减弱动效

如果新规则只能通过持续追加高特异性覆盖才能生效，应先回到原组件规则整理样式，而不是继续堆叠 `!important`。

## 5. 发布前检查

- 记录实际 Python / Gradio 版本。
- CSS 解析无语法错误。
- 全量单元测试通过。
- 本机启动返回 HTTP 200。
- 浏览器中完成样板页截图、交互、溢出与控制台检查。
- 视觉验收失败时先修复滚动、遮挡、输入和主操作，再调整装饰细节。
