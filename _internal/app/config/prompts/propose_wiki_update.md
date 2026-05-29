Create a reviewable Markdown wiki update proposal from a saved research chat turn.

Use only the user question, assistant answer, and cited sources.
Do not invent new scientific claims.

Return Markdown with this exact structure:

# Wiki Update Proposal

## Target Page Recommendation

Recommend exactly one target page path. Use lowercase kebab-case and include the `.md` extension. Prefer:

- `wiki/topics/...`
- `wiki/methods/...`
- `wiki/surveys/...`
- `wiki/simulations/...`
- `wiki/concepts/...`

## Proposed Update

Write concise Markdown bullets or paragraphs that could be added to the target page.
Every scientific claim must cite a source path or paper ID from the provided sources.

## Source Evidence

List the cited source paths.

## Review Notes

Mention what a human reviewer should verify before applying this proposal.
