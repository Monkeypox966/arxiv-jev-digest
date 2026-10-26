# Architecture

## Pipeline

`arxiv.py` discovers daily announcements exclusively from `/list/<category>/new`, validates its dated new/cross/replacement sections and complete pagination, then uses the API for complete metadata and explicit versions. Discovery date and section survive metadata resolution. Cross-category duplicates are merged. API requests are serialized with at least three seconds between attempts. Pagination is complete or fails visibly. Explicit API/RSS inputs and the old `latest` reader remain compatibility paths; the default configuration never uses `/recent`. `/new` cannot reconstruct past batches; historical API recovery is explicit and has submission-window limitations.

`clients.py` constructs one Noul per interest and exclusion. All questions for one paper share a Jev call. It validates finite numeric probabilities in [0, 1], composes weighted scores in Python, and records the actual model name. A bounded worker pool and request limiter control spend and throughput.

With institution ranking enabled, all relevance-qualified candidates get sourced affiliation enrichment before the institution gate and Top K; metadata extraction may call DeepSeek. `ranking.py` matches configurable local aliases against verified author affiliations, uses the maximum matching institution preference, and combines it with relevance. Unknown metadata has an explicit keep/exclude policy, distinct from verified but unlisted institutions. A ranking audit includes all relevance-qualified candidates. Ranking changes affect review history but preserve Jev decision cache keys and recipient-wide sent deduplication.

`enrichment.py` reconciles abs abstract/comments, HTML, PDF and one identified project page. Institution extraction does not request individual author mappings or reconcile numeric footnotes. PDF header extraction is supplemented by bounded body-link scanning, excluding references. Project pages with multiple article cards retain their main document. Source-backed institution names are merged and deduplicated; ranking uses this list directly. External requests check public destinations, redirects and response limits. The v7 extraction cache invalidates older partial-source results. Partial PDF failures use a short TTL and can enter a bounded retry queue.

Only the selected union across recipients is summarized by DeepSeek. Summaries must contain all six nonempty string fields and finish normally. Invalid JSON, incomplete output, and selected transient service errors receive bounded retries. Authentication failures do not retry. Text from papers is treated as untrusted source data.

`store.py` uses SQLite transactions. A cache key includes paper content and relevant model/prompt/profile settings. Reviewed IDs are scoped by recipient hash and profile. Original/cross-list delivery uses paper-level deduplication; replacements use acknowledged paper-version pairs independently of profile. A new version can be sent once even if the original paper was already delivered. Version records, recipient history, retries and delivery acknowledgement are committed together. Unknown versions cannot be treated as replacements.

`report.py` renders escaped HTML, plain text, and structured JSON from the same picks, grouped as new submissions, cross-lists and replacements. Top K is a global cap of 15, not a quota per section; relevance and institution gates remain mandatory. The email template is included in the wheel. It constructs version-pinned paper links from normalized arXiv IDs. Replacement summaries describe the supplied abstract, not an inferred version diff.

## Delivery state machine

```text
SMTP login → prepared → persist prepared → pending → persist pending → SMTP → acknowledgement
                      │                             │
                      └─ definitely unsent → retry  └─ uncertain acceptance → manual resolution
```

Each recipient gets a separate message, transaction, and history. If a later recipient fails, earlier accepted deliveries stay complete. SMTP acceptance is not final inbox delivery. The trusted Actions state helper saves the pending outbox before SMTP DATA so runner loss does not erase the guard. Source checkpoints are per category/recipient and advance atomically with acknowledged delivery or a successful no-match review. Preview and uncertain attempts never advance them. A database transaction cannot be atomic with SMTP, so unresolved attempts block resends.

Schema v4 migrates v1/v2/v3 in place, retaining history and legacy pending records while adding sent-version tracking. Prepared attempts are provably unsent and safely discarded; ambiguous pending attempts remain protected. Explicit SMTP rejection is safe to retry, unlike a lost acceptance response. Unsent metadata/institution assessments can be queued beyond the latest batch; recovered selections keep their retry records until delivery is acknowledged. `usage.py` reserves every real API attempt, reconciles provider usage and fails closed on exhaustion. `health.json` and `usage.json` are written even on failure. Diagnostics use the same extraction/budget stack without sending or advancing paper history.

## GitHub Actions

Daily / manual workflow: checkout trusted code → locked uv install → config check → restore separate state checkout → process → persist database even on failure → upload reports and database backup. State writes use short-lived GITHUB_TOKEN authentication supplied to git through environment configuration; credentials are not written into the state checkout. Branch writes never force-push.

Pull-request CI has read-only repository permissions and uses mock transports. It does not load API credentials or production state. Dependencies and third-party Action revisions are pinned and updated by Dependabot.
