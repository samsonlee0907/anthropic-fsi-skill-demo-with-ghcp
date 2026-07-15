## Repo overlay: readability and visual contrast gate

For this repo's IB demo deck, treat poor contrast as a substantive QC issue.

### Additional visual QC checks

- Flag any slide where text is difficult to read at normal zoom because of low contrast.
- Flag any use of white / near-white text on pale fills unless the background is genuinely dark.
- Flag washed-out slides that rely on multiple similar low-saturation fills without a clear visual hierarchy.
- Flag slides whose key chart labels, matrix labels, or table headers become hard to read after export to PNG.

### Severity guidance

- **Critical**: a slide's main message or data cannot be read reliably because of contrast or styling choices.
- **Important**: the slide is technically readable but not client-ready because the palette is too faint, inconsistent, or visually muddy.

When these issues appear, say explicitly that the deck should be restyled before delivery.
