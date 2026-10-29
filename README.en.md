# arXiv Jev Digest

English | [简体中文](README.md)

Screen new arXiv papers, generate summaries and deliver them by email. Runs on GitHub Actions without a server.
The default setup discovers papers from [cs.RO `/new`](https://arxiv.org/list/cs.RO/new), screens them with Jev and generates Chinese summaries with DeepSeek. Emails have three sections: **new submissions, cross-lists and replacements**, with **up to 15 papers total**. Fewer qualifying papers means a shorter digest; thresholds are never lowered to fill the quota.

Emails show faithful, complete translations of the original abstracts, without abridgment or a target word count, plus brief author/institution overviews and paper/project links. Ranking uses the paper's sourced institution list without individual author mappings. Full institution lists and source evidence remain in JSON reports.

<br>

## Step 1: Fork and enable Actions

Click **Fork** to create your own copy, then open **Actions** in your repository and enable the workflows.
Make the following changes in your own copy. Keep the configuration and workflow on its default branch.

<br>

## Step 2: Configure your interests

Edit the existing fields in [`config.toml`](config.toml):

| Setting | Purpose |
| --- | --- |
| `arxiv.categories` | arXiv categories, e.g. `["cs.RO"]` or `["cs.AI", "cs.LG"]` |
| `[[interests]]` | Replace the example interests with your research topics |
| `[[exclusions]]` | Topics to exclude; review these when changing research areas |
| `selection.top_k` | Total limit across all three sections; default: 15, with no minimum quota |
| `selection.min_score` | Relevance threshold; default: 0.60 |
| `ranking` | Institution ranking after relevance screening (40%); verified new institutions and aliases are learned persistently; see [configuration](docs/ranking.md) |
| `deepseek.language` | Translation language; use `"English"` to retain English abstracts in their original language |
| `email` | SMTP host, port, security mode and subject prefix |

Write each interest as a clear statement that can be judged from a title and abstract. For example, replace an existing interest with:

```toml
[[interests]]
id = "robot_learning"
label = "Robot imitation learning"
text = "Proposes a method for learning robot manipulation policies from demonstrations."
weight = 1.0
```

Save and commit your changes to the default branch. Edit existing TOML tables rather than duplicating them. Model endpoints, names and per-run budgets are also configurable. Defaults use the official TypeSafe Jev and DeepSeek APIs. API calls incur usage charges, including metadata extraction and retries.

<br>

## Step 3: Add API keys and email credentials

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
| --- | --- |
| Decision service key | Default: `TYPESAFE_API_KEY` ([get a key](https://console.typesafe.ai/settings/keys)); see “Change the decision service” below for other backends. Only the selected service's key is required |
| `DEEPSEEK_API_KEY` | [DeepSeek API key](https://platform.deepseek.com/api_keys) |
| `SMTP_USERNAME` | Full sender email address |
| `SMTP_PASSWORD` | SMTP authorization code or app password |
| `EMAIL_FROM` | Sender address, usually the same as `SMTP_USERNAME` |
| `EMAIL_TO` | Recipient address; separate multiple addresses with commas |

**QQ Mail:** enable SMTP and generate an authorization code under the account security settings, then save it as `SMTP_PASSWORD`. The default SMTP settings are `smtp.qq.com`, port `465`, `security = "ssl"`. Use the authorization code rather than your QQ login password.

For another provider, update `[email]` with its SMTP settings. SSL and STARTTLS are supported, commonly on ports 465 and 587 respectively. Keep credentials in Secrets and out of committed files.

<br>

## Step 4: Test and send your first digest

Open **Actions → Daily arXiv Digest → Run workflow**:

1. Run `delivery_test` to send one test email without calling any models. Confirm that your email settings work.
2. Run `preview` to fetch papers and call the models without sending email. Download the report from **Artifacts** and open `digest.html` to check the selection and summaries.
3. Run `send` for normal screening and delivery. The `daily` mode uses the automatic delivery guard and skips recipients already sent a digest today.

<br>

## Step 5: Check the daily schedule

The server dispatches `daily` **hourly from 10:00 through 23:00 Beijing time**. GitHub cron remains a backup over the same hours. Actions first checks today's acknowledged delivery, then verifies that every `/new` listing is dated today. Already-delivered or stale-batch runs skip models and email; a fresh batch with matching papers sends once.

To change the hours, update the cron and `--daily-after 10:00` in [`daily.yml`](.github/workflows/daily.yml), plus the timer in `deploy/`. Cron uses UTC; batch dates and daily deduplication use `timezone` from `config.toml`.

To cover missed GitHub cron events, install the [independent hourly server trigger](deploy/README.en.md). The server only dispatches; Actions owns freshness and delivery decisions. No model or SMTP credentials are needed on the server.

Institutions contribute **40%** of the ranking. S/A/B tiers score **100/80/60**, with a minimum of **80** for verified institutions, excluding B-tier papers. Unknown institutions use separate `unknown_policy` / `unlisted_policy` settings. See the [list and configuration](docs/ranking.md). Motion imitation also includes `mimic`, requiring a humanoid or bipedal control contribution.

**Empty digests are never sent**, including stale batches, weekends without announcements and fresh batches without matching papers. Stale listings skip before metadata API or model calls and wait for the next hourly check. All subscribed categories must be up to date. New submissions and cross-lists are deduplicated by paper, replacements by version. GitHub scheduled runs can be delayed or dropped; see the [GitHub documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

<br>

Daily discovery uses `arxiv.source = "new"`. `/new` has no historical batches. After missed days, use explicit API-window recovery in the [operations guide](docs/operations.md); it cannot reconstruct all historical cross-list and replacement announcements. Replacement summaries describe the current abstract, not a version diff.

## Optional: Change the decision service

TypeSafe Jev is the default. Skip this section if you are using the default service. OpenRouter and other services strictly compatible with the existing System One protocol are supported, with one backend used per run. Edit the existing `[jev]` configuration to switch services:

```toml
[jev]
backend = "my-provider"
endpoint = "https://decision.example.com/v1/systemone"
model = "provider-model-id"
api_key_env = "DECISION_API_KEY"
```

Replace the example endpoint and model with your provider's actual values. Backend names must start with a lowercase letter and may contain lowercase letters, digits, hyphens and underscores. Custom backends require an explicit `endpoint` and `model`. The service must use HTTPS with Bearer authentication, accept `state + questions` objects, and return `answers.<question>.type = "noul"` with a `noul` probability in [0, 1]. Other protocols need an adapter. All interests and exclusions for a paper are submitted together and must fit the provider's question limit.

| Backend | Default key variable when `api_key_env` is omitted | Endpoint and model |
| --- | --- | --- |
| `typesafe` | `TYPESAFE_API_KEY` | Official Jev defaults |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/alpha/decisions`; `typesafe/jev-1.14` |
| Custom name | `DECISION_API_KEY` | Provider's full decision endpoint and model ID |

GitHub Actions passes all three Secret names above; use `DECISION_API_KEY` for another provider. Locally, `api_key_env` may name another environment variable. In Actions, a different variable also needs a corresponding Secret mapping in the workflow's `env`. Run `preview` after switching to check results and thresholds. Protocol compatibility tests do not mean every provider has been tested with real credentials.

<br>

## Missing an email?

Check **Actions → latest run → Summary** for its trigger, outcome, selection count and delivery count:

- **No run:** check that the workflow is enabled and on the default branch; run `send` manually to catch up.
- **Successful, zero deliveries:** check whether you used `preview` or disabled `email.send_empty`; normal send mode includes empty digests by default.
- **Failed:** inspect the error and `health.json`; check API balance, Secrets and SMTP credentials.
- **Pending delivery:** check the inbox and spam folder, then follow the [recovery guide](docs/operations.md) before retrying.

The workflow creates a `digest-state` branch for persistent history, caches and learned institutions. Keep that branch and the workflow's `contents: write` permission. Reports and a recovery database are uploaded as Actions Artifacts.

<br>

## Local use (optional)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.11+, clone the repository, then run:

```bash
git clone https://github.com/Monkeypox966/arxiv-jev-digest.git
cd arxiv-jev-digest
uv sync --locked
uv run arxiv-digest check
uv run arxiv-digest demo
```

`demo` creates a fictional report offline without credentials. For real runs, copy [`.env.example`](.env.example) to `.env`, fill in credentials, then run:

```bash
uv run arxiv-digest check --credentials
uv run arxiv-digest run --dry-run
uv run arxiv-digest run
```

<br>

## Further documentation

[Operations and recovery](docs/operations.md) · [Institution ranking](docs/ranking.md) · [Project links and affiliations](docs/enrichment.md) · [Architecture](docs/architecture.md) · [Validation](docs/reliability-validation.md) · [Contributing](CONTRIBUTING.md)

Some linked guides are in Chinese. Institution preferences are a reading preference rather than an objective quality assessment. Automated screening, summaries and affiliation extraction can contain errors; consult the original paper.

Inspired by [JEV-Paper-Radar](https://github.com/Eliot5566/JEV-Paper-Radar), [daily-arXiv-ai-enhanced](https://github.com/dw-dengwei/daily-arXiv-ai-enhanced), [arXiv-Daily-Summarizer](https://github.com/RunRiotComeOn/arXiv-Daily-Summarizer) and [Arxiv-tracker](https://github.com/colorfulandcjy0806/Arxiv-tracker).

[MIT License](LICENSE) · [Security](SECURITY.md)
