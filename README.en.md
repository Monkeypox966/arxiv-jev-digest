# arXiv Jev Digest

English | [简体中文](README.md)

Screen new arXiv papers, generate summaries and deliver them by email. Runs on GitHub Actions without a server.
The default setup uses Jev for screening and DeepSeek for Chinese summaries, tracks cs.RO robotics, and selects up to 10 papers per batch. Follow the steps below to use your own research interests and language.

Emails prioritize contribution summaries, brief author/institution overviews and paper/project links. Ranking uses the paper's sourced institution list without individual author mappings. Full institution lists and source evidence remain in JSON reports.

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
| `selection.top_k` | Maximum papers per batch; default: 10 |
| `selection.min_score` | Relevance threshold; default: 0.60 |
| `ranking` | Institution filtering and ranking after relevance screening; default weight: 35%; see [ranking configuration](docs/ranking.md) |
| `deepseek.language` | Summary language; change `"简体中文"` to `"English"` for English summaries |
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

The primary run is scheduled for **09:30 Beijing time (01:30 UTC)**, with hourly fallback checks at **09:47–17:47 Beijing time**. Automatic runs check each recipient's acknowledged daily delivery, including empty digests. Already-delivered recipients are skipped before fetching papers or calling models. Manual `send` deliveries count; `delivery_test` messages do not. Manual `send` itself remains an explicit additional run.

To change the target time, update the primary cron, fallback window and `--daily-after 09:30` in [`daily.yml`](.github/workflows/daily.yml). Cron uses UTC; the daily guard uses `timezone` from `config.toml`.

**An empty digest is sent even when there are no new matching papers** (`email.send_empty = true`), explaining that no matches are available. Sending an empty digest adds no model calls; any new candidates still require decision model screening. Previously delivered papers are not resent, and weekends may reuse the latest announcement batch. Set this option to `false` to skip empty digests. GitHub scheduled runs can be delayed or dropped, and public repositories with no activity for 60 days may have schedules disabled. [GitHub documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

<br>

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

The workflow creates a `digest-state` branch for persistent history and caches. Keep that branch and the workflow's `contents: write` permission. Reports and a recovery database are uploaded as Actions Artifacts.

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
