## Repo overlay: FSI demo presentation styling

When authoring the IB pitch deck for this repo, treat readability and contrast as hard requirements, not polish.

### Visual style requirements

- Use an enterprise palette with **dark text on light backgrounds**:
  - Title / headline text: deep navy (`#0F2740` or similar)
  - Body text: charcoal / slate (`#334155` or similar)
  - Backgrounds: white or very light blue-gray (`#F5F8FC`, `#E8EEF5`)
  - Accent color: one restrained blue / teal accent only
- **Do not use white or near-white text on pastel fills.**
- Avoid low-contrast combinations such as light gray text on white, white text on pale blue, or multiple washed-out fills on the same slide.
- Prefer clean whitespace, simple grids, and flat fills over decorative gradients or soft shadows.

### Layout guidance for the demo deck

- The strongest README-worthy slide is usually the **competitive-positioning matrix** or the **peer context table**, not a text-heavy summary cover.
- For two-panel summary slides, keep panel fills light but distinct and keep all body copy in dark text.
- If a slide would look crowded, reduce copy before reducing contrast.

### `python-pptx` implementation guidance

When writing the deck in Python, set colors explicitly instead of relying on theme defaults:

```python
from pptx.dml.color import RGBColor

NAVY = RGBColor(0x0F, 0x27, 0x40)
SLATE = RGBColor(0x33, 0x41, 0x55)
PANEL = RGBColor(0xF5, 0xF8, 0xFC)
ACCENT = RGBColor(0x2F, 0x6F, 0xB3)
```

- Explicitly set font color for every title, subtitle, and body text block.
- Explicitly set fill color for summary boxes and matrices.
- Keep chart / matrix labels dark enough to remain legible after PNG export at 1600x900.

The final `.pptx` must look presentation-ready when flattened to a static PNG in the README, not only when viewed interactively in PowerPoint.
