---
name: firecrawl
description: |
  Firecrawl gives AI agents and apps fast, reliable web context with
  strong search, scraping, interaction, document parsing, research,
  and monitoring tools. Use when scraping dynamic or JS-heavy websites,
  extracting structured data, searching the web, or crawling documentation.
---

# Firecrawl

Firecrawl helps agents search first, scrape clean content, interact
with live pages when plain extraction is not enough, parse local
documents into markdown, search scientific papers and GitHub history
through the research index, monitor pages for changes, and produce
finished deliverables from web data.

## Available Tools

- **`firecrawl_scrape`**: Retrieve and extract content from a URL (markdown, HTML, JSON schema, screenshot, links).
- **`firecrawl_search`**: Search the web and return full markdown content from results.
- **`firecrawl_parse`**: Parse local documents (PDF, DOCX, XLSX, etc.) into clean markdown.

## Common Operations

### Scrape a Page
Extract clean markdown from a single URL with JavaScript rendering:
```json
{
  "url": "https://example.com",
  "formats": ["markdown"]
}
```

### Search the Web
Search for a query and get structured/markdown results:
```json
{
  "query": "best practices for postgres connection pooling"
}
```

### Extract Structured JSON
Extract specific fields matching a schema from a URL:
```json
{
  "url": "https://example.com/pricing",
  "formats": ["json"],
  "jsonOptions": {
    "prompt": "Extract the plan names, prices, and features"
  }
}
```
