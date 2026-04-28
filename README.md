# WSL Daily News Mailer

WSL Daily News Mailer is a small automation that uses Codex as a search and
writing agent to generate Chinese daily news emails. It runs on a timer, asks
Codex to search for current AI news and conflict-situation news, rewrites the
raw results into a readable Markdown brief, and sends the result through SMTP.

The project is intentionally lightweight: the runner only depends on the Python
standard library plus an installed `codex` CLI.

## What It Does

- Runs unattended from systemd timers.
- Generates separate morning and evening reports.
- Searches for 10 AI news items and 10 conflict-situation news items.
- Assembles a raw report before asking Codex to rewrite it into a polished
  Chinese email.
- Sends the final Markdown report through Gmail SMTP or another SMTP server.
- Retries failed Codex runs.
- Sends a failure notification email when generation fails.
- Records send markers so the same report variant is not sent twice by accident.
- Keeps generated reports and logs local by default.

## Workflow

1. `systemd` starts `run_daily_news.py`.
2. The script loads SMTP and Codex settings from an env file.
3. Codex searches the web for AI news.
4. Codex searches the web for conflict-situation news.
5. The script combines both raw sections into one Markdown draft.
6. Codex rewrites the draft into a Chinese daily brief with:
   - `今日导读`
   - `AI 新闻`
   - `战争局势`
   - `AI 总结`
7. The script sends the result by email and records a local send marker.

## Repository Layout

```text
.
├── run_daily_news.py
├── scripts/
│   └── install.sh
├── systemd/
│   ├── wsl-daily-news-morning.service
│   ├── wsl-daily-news-morning.timer
│   ├── wsl-daily-news-evening.service
│   └── wsl-daily-news-evening.timer
├── .env.example
└── README.md
```

Generated files are ignored by Git:

- `logs/`
- `output/`
- `state/`

## Requirements

- WSL with systemd enabled
- Python 3.10+
- Codex CLI available on `PATH`
- SMTP account credentials

## Configure

Copy the example config and fill in real values:

```bash
cp .env.example ~/.config/wsl-daily-news/news_mail.env
chmod 600 ~/.config/wsl-daily-news/news_mail.env
```

Important variables:

```text
SMTP_SENDER=your_gmail@gmail.com
SMTP_RECIPIENT=your_mailbox@example.com
SMTP_APP_PASSWORD=replace_with_gmail_app_password
CODEX_BIN=codex
CODEX_MODEL=
```

Do not commit the real env file. It contains SMTP credentials and possibly model
provider settings.

## Run Manually

Generate a local sample without calling Codex or sending email:

```bash
python3 run_daily_news.py --dry-run --skip-email
```

Generate a real report but do not email it:

```bash
python3 run_daily_news.py --skip-email --variant evening
```

Generate and send the evening report:

```bash
python3 run_daily_news.py --variant evening
```

Force a resend for the same date and variant:

```bash
python3 run_daily_news.py --variant evening --force-send
```

## Install Timers

```bash
bash scripts/install.sh
```

The installer creates a local env file if needed, installs systemd units, and
enables the timer.

Check timer status:

```bash
systemctl list-timers 'wsl-daily-news*' --all
systemctl status wsl-daily-news-morning.timer --no-pager
systemctl status wsl-daily-news-evening.timer --no-pager
```

## Useful Paths

- Reports: `output/*.md`
- Codex attempt logs: `logs/*.codex.attempt*.log`
- Runner log: `logs/runner.log`
- Send markers: `state/sent-*.json`
