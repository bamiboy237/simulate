---
name: pdfp
description: Parse PDF files locally to markdown or structured JSON. Use when the user provides a PDF path or asks you to read/extract text, tables, or structure from a PDF file.
---

# pdfp — local PDF parsing

`pdfp` is an installed CLI (`~/.local/bin/pdfp`, global) that converts PDFs to
markdown or JSON. Local, offline, no API keys.

## When to use

- The user hands you a PDF path or asks to summarize/extract from one.
- You need text, table rows, or reading order from a PDF without external services.
- You must not use it for scanned image-only PDFs (no text layer → empty output).

## Commands

```bash
# markdown → stdout (default)
pdfp <file.pdf>

# structured JSON → stdout
pdfp <file.pdf> --format json

# specific pages, 1-based (e.g. 1-3,5)
pdfp <file.pdf> --pages 1-3,5

# write to a file instead of stdout
pdfp <file.pdf> -o out.md

# read PDF bytes from stdin
cat file.pdf | pdfp -
```

## Output contract

- Parse result goes to **stdout only**. Errors go to stderr with exit code 1.
- JSON shape:
  ```json
  {"source": "...", "pages": [{"page": 1, "blocks": [{"type": "text|table|image", "bbox": [x0,y0,x1,y1], "content": "..."}]}]}
  ```
  - `text` blocks: `content` is the extracted string.
  - `table` blocks: `content` is an array of rows, each an array of cell strings.
  - `image` blocks: `content` is `null` (images are not embedded).
- If a PDF has no extractable text, the output is empty — mention to the user that the PDF is scanned/image-only and OCR is not available.

## Best practice

- Default to `pdfp <file> --format json` when you need tables or structure; default to plain markdown for prose documents.
- Use `--pages` to limit huge PDFs and keep output within context budget.
- Do not chain or re-parse output; consume stdout directly.
