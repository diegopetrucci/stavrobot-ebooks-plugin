# stavrobot-ebooks-plugin

`stavrobot-ebooks-plugin` is the `ebooks` plugin for [Stavrobot](https://github.com/skorokithakis/stavrobot). It searches Bookshelf ebook metadata, requests an explicitly selected ebook, and reports read-only acquisition status. The plugin is ebook-only: audiobook acquisition needs a second Bookshelf instance; see the local diagnosis note for context.

## Install

Install this repository through Stavrobot's git-URL flow:

1. Use Stavrobot's `manage_plugins` action `install` with this repository URL: `https://github.com/diegopetrucci/stavrobot-ebooks-plugin.git`.
2. Configure the installed plugin named `ebooks` with the flat settings below, using Stavrobot's `manage_plugins` `configure` action or the plugin settings page.
3. Run `books_status` to check the connection and named Bookshelf settings before requesting a book.

## Configuration

The plugin runner normally runs in Docker. When Bookshelf is reachable on the host at port 8787, use `http://host.docker.internal:8787`; `localhost` and `127.0.0.1` from the plugin runner point back to the runner container instead of the host service.

Configuration is a flat JSON object. `config.example.json` contains placeholders only:

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `bookshelf_url` | yes | — | Bookshelf base URL. |
| `bookshelf_api_key` | yes | — | API key from Bookshelf's Settings > General page. Treat it as a secret. |
| `root_folder_name` | no | `Bookshelf Sandbox` | Exact Bookshelf root-folder name used for new authors. |
| `quality_profile_name` | no | `eBook` | Exact Bookshelf quality-profile name used for new authors. |
| `metadata_profile_name` | no | `Standard` | Exact Bookshelf metadata-profile name used for new authors. |
| `http_timeout_seconds` | no | `15` | Per-request timeout greater than 0 and at most 120 seconds. JSON numbers and plain decimal strings from the settings page are accepted. |

Keep the API key in Stavrobot configuration only; do not put it in this repository or in messages, logs, or tool output.

### Runner deadline

The plugin runner hard-kills a tool after 30 seconds. Each tool invocation therefore
uses one internal 25-second monotonic Bookshelf budget, shared by all GET and
mutation calls, leaving time for the final JSON response. `http_timeout_seconds`
remains the per-request cap; each call uses whichever limit is smaller. The
internal budget is not a caller-configurable setting, and an exhausted budget
returns a sanitized retryable timeout without opening another request.

## Tools

All four tools operate on Bookshelf. Search, health checks, and status checks are read-only; only `request_book` changes the Bookshelf request state.

### `books_status`

- Optional `metadata_check` boolean (default `false`).
- Checks reachability/authentication and resolves the configured root folder, quality profile, and metadata profile by name. Each name must resolve to exactly one record.
- Returns `ok`, `degraded` (reachable but misconfigured), or `unavailable` (unreachable or authentication failure).
- With `metadata_check`, a successful empty metadata response is reported as ambiguous: it can mean no match or a temporarily unavailable metadata service.

### `search_books`

- Required `query` string, trimmed and limited to 200 characters; optional `limit` from 1 through 10 (default `5`).
- Returns bounded, selectable candidates with `candidate_id` (display/debug identity), `request_token`, title, author, and year; disambiguation and series information may also be present. Public title, author, disambiguation, and series-title text is printable and limited to 200 characters per field.
- `request_token` is a short-lived (24-hour) signed token bound to the original query and exact candidate. Pass that token to `request_book`; never substitute `candidate_id`, a title, or an author.
- The tool never chooses a candidate or mutates Bookshelf. Empty results return `state: no_results_or_metadata_unavailable`; that ambiguity should be retried after a few minutes.

### `request_book`

- Required `request_token` from the user's explicit `search_books` selection; optional `search_now` boolean (default `true`).
- Verifies the signed token before making API calls, revalidates the exact candidate, and converges on an existing Bookshelf record before creating or monitoring anything. Repeating the same request is idempotent.
- Returns a durable Bookshelf `request_id`, `status` (`requested` or `searching`), `terminal: false`, and a suggested check after 300 seconds, plus bounded title/author text. A best-effort immediate search can be skipped with `search_now: false`.

### `check_book_request`

- Required positive-integer `request_id` returned by `request_book`.
- Read-only. It checks the Bookshelf book, queue, history, and search-command evidence and maps it to the stable state model below.
- `terminal: true` is returned only for `imported` or an explicit `failed` state. Import means Bookshelf has both a file and book-scoped import history; a file count alone is not enough.

## Stable state model

| State | Terminal | Meaning | Suggested next check |
| --- | --- | --- | --- |
| `requested` | no | No active search/download is visible, or a previous search found no grab. | 30 minutes |
| `searching` | no | Bookshelf is searching, or a release was recently grabbed. | 5 minutes |
| `queued` | no | The request is waiting in the download queue. | 15 minutes |
| `downloading` | no | An active download is visible in the queue. | 15 minutes |
| `grabbed_stalled` | no | A release was grabbed more than two hours ago but has not reached import. | 30 minutes; manual attention may be needed |
| `imported` | yes | A file exists and Bookshelf history confirms the book-scoped import. | Stop checking |
| `failed` | yes | Bookshelf reported an acquisition or import failure. | Stop checking; notify the user |

## Follow-up

After `request_book`, offer exactly one recurring `manage_cron` entry (for example, every 15 minutes) that calls `ebooks/check_book_request` with the returned `request_id`. Cron replies are not automatically delivered: the agent must explicitly call `send_telegram_message` with the outcome. When `terminal` is `true`, or when 48 hours have elapsed, notify the user and delete that cron entry. Do not chain one-shot cron entries.

## Limitations

- A metadata search with no results is ambiguous: the metadata service may be unavailable, so retry rather than claiming that no book exists.
- Adding a new author can import that author's whole bibliography as unmonitored Bookshelf records; only the explicitly selected book is monitored.
- Bookshelf applies one selected format per author. The plugin does not promise multiple formats for the same author.

The implementation and tests are local and do not make live API calls as part of normal unit-test execution.
