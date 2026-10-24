# Architecture

## Pipeline

`arxiv.py` reads official API / RSS metadata, normalizes versioned IDs, and deduplicates cross-lists. API requests are serialized with at least three seconds between attempts. Pagination is complete or fails visibly.

`clients.py` constructs one Noul per interest and exclusion. All questions for one paper share a Jev call. It validates finite numeric probabilities in [0, 1], composes weighted scores in Python, and records the actual model name. A bounded worker pool and request limiter control spend and throughput.

With institution ranking enabled, all relevance-qualified candidates get sourced affiliation enrichment before the institution gate and Top K; metadata extraction may call DeepSeek. `ranking.py` matches configurable local aliases against verified author affiliations, uses the maximum matching institution preference, and combines it with relevance. Unknown metadata has an explicit keep/exclude policy, distinct from verified but unlisted institutions. A ranking audit includes all relevance-qualified candidates. Ranking changes affect review history but preserve Jev decision cache keys and recipient-wide sent deduplication.

`enrichment.py` checks abs abstract/comments and HTML independently, then falls back to PDF when a project or author mapping is missing or HTML conversion loses affiliation markers. PDF header extraction is supplemented by bounded body-link scanning, excluding the reference section. A confirmed project page may fill missing author mappings after an identity check. Field-level provenance is retained; project metadata cannot overwrite known paper mappings. External page requests check public destinations and each redirect and enforce response limits. The versioned metadata cache key includes abstract content and extraction settings.

Only the selected union across recipients is summarized by DeepSeek. Summaries must contain all six nonempty string fields and finish normally. Invalid JSON, incomplete output, and selected transient service errors receive bounded retries. Authentication failures do not retry. Text from papers is treated as untrusted source data.

`store.py` uses SQLite transactions. A cache key includes paper content and the relevant model / prompt / profile settings. Reviewed IDs are scoped by recipient hash and profile; sent IDs are scoped only by recipient hash, so changing interests does not resend delivered papers.

`report.py` renders escaped HTML, plain text, and structured JSON from the same picks. The email template is included in the wheel. It only constructs paper links from normalized arXiv IDs.

## Delivery state machine

```text
SMTP login → prepare pending → SMTP DATA accepted → sent + reviewed
                       │
                       └─ interruption / failure → keep pending → manual sent or retry
```

Each recipient gets a separate message, transaction, and history. If a later recipient fails, earlier accepted deliveries stay complete. SMTP acceptance is not final inbox delivery. A local transaction cannot be atomic with a remote SMTP server, so unresolved attempts block automatic resends.

## GitHub Actions

Daily / manual workflow: checkout trusted code → locked uv install → config check → restore separate state checkout → process → persist database even on failure → upload reports and database backup. State writes use short-lived GITHUB_TOKEN authentication supplied to git through environment configuration; credentials are not written into the state checkout. Branch writes never force-push.

Pull-request CI has read-only repository permissions and uses mock transports. It does not load API credentials or production state. Dependencies and third-party Action revisions are pinned and updated by Dependabot.
