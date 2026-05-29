Create a production Markdown wiki body for one astronomy paper.

Use only:

- the paper metadata,
- the provided source-extract JSON from this same paper.

Do not use other papers. Do not add broad synthesis. Do not invent missing
details. If evidence is missing, say it is not confirmed in the extracted
context.

Return Markdown only. Do not include YAML frontmatter and do not include a
top-level title.

Use these sections exactly:

## Scientific Question

## Data

## Method

## Main Results

## Limitations

## Follow-up Questions

Writing rules:

- Prefer source-backed bullets over fluent but unsupported prose.
- The response is incomplete and invalid unless all six required sections are present in this exact order.
- Finish with the `## Follow-up Questions` section. If output space is tight, shorten earlier bullets rather than omitting the final sections.
- Preserve important numerical values, sample sizes, redshift ranges, surveys, simulations, instruments, and named methods.
- Cite local evidence using only the provided `citation_label` values in parentheses, for example `(section 4.2)` or `(paragraph 7)`.
- Do not cite internal chunk numbers such as `(chunk 3)`.
- Do not invent source labels such as `(paper opening)`, `(paper title)`, or `(source passage 1)`.
- Do not cite the current paper as a whole using a paper-wide identifier; use the nearest `citation_label` instead.
- Do not cite `existing wiki excerpt`; it is context only, not source evidence.
- If a claim only appears in the abstract, mark it as abstract-level evidence.
- Keep the page concise enough to review manually.
