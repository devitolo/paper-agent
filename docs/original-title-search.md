# Searching original article titles

Review Queue title search matches the displayed title and any verified
`original_title` stored in a paper source's metadata. Matching is case-insensitive
and treats `%` and `_` literally. Saved, Excluded, source, and pagination filters
continue to apply. An alternate title returns the existing article, not another
copy.

ZenML imports retain an explicit `original_title` when provided. The upstream
ZenML title is still the displayed title. Original titles are not inferred from
summaries or URL slugs, and this change does not fetch publisher pages. Existing
records need a verified title added before searches for that title can match.
Provider refreshes preserve a previously recorded original title when new
metadata omits it.

For a verified title, use the exact stored source ID:

```sh
python scripts/set_original_title.py \
  --db /app/data/paper_agent.db \
  --source zenml \
  --source-id 'https://www.infoq.com/presentations/linkedin-context-engineering/?utm_campaign=infoq_content&utm_source=infoq&utm_medium=feed&utm_term=AI%2C+ML+%26+Data+Engineering-presentations' \
  --title 'Context Engineering at LinkedIn: How We Built an Organizational Context Layer for AI Agents with MCP'
```

This example was verified against the [InfoQ presentation](https://www.infoq.com/presentations/linkedin-context-engineering/).
The command preserves other metadata and review state, refuses to create a
missing database, and fails if the source ID does not exist. Run it after the
approved application release to enable this title for existing production data.
The change needs no schema migration. Older application images can read the
additional metadata; they simply do not search it.
