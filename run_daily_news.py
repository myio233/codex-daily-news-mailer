#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
import pathlib
import shlex
import shutil
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage


BASE_DIR = pathlib.Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
USER_ENV_FILE = pathlib.Path.home() / ".config" / "wsl-daily-news" / "news_mail.env"
SYSTEM_ENV_FILE = pathlib.Path("/etc/wsl-daily-news/news_mail.env")
DEFAULT_CODEX_BIN = shutil.which("codex") or "codex"
DEFAULT_CODEX_MODEL = ""


def default_env_file() -> pathlib.Path:
    if USER_ENV_FILE.exists():
        return USER_ENV_FILE
    if SYSTEM_ENV_FILE.exists():
        return SYSTEM_ENV_FILE
    return USER_ENV_FILE


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch daily AI/war news via Codex and email it.")
    parser.add_argument("--dry-run", action="store_true", help="Generate a local sample report instead of calling Codex/SMTP.")
    parser.add_argument("--skip-email", action="store_true", help="Build the report but do not send email.")
    parser.add_argument("--force-send", action="store_true", help="Send even if this report date was already sent before.")
    parser.add_argument("--env-file", default=str(default_env_file()), help="Path to KEY=VALUE config.")
    parser.add_argument("--report-date", help="Date in YYYY-MM-DD; defaults to today in local timezone.")
    parser.add_argument(
        "--variant",
        choices=("morning", "evening", "manual"),
        default="evening",
        help="Report variant. Used in filenames, subjects, and duplicate-send markers.",
    )
    return parser.parse_args()


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except PermissionError:
        # systemd may have already injected EnvironmentFile variables into the process.
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def variant_label(variant: str) -> str:
    labels = {
        "morning": "AI早报",
        "evening": "AI晚报",
        "manual": "AI简报",
    }
    return labels.get(variant, "AI简报")


def build_ai_prompt(target_date: dt.date, variant: str) -> str:
    label = variant_label(variant)
    return (
        f"搜索 {target_date.isoformat()} 的 10 条热门 AI 新闻，用于{label}。"
        "优先使用真实新闻源和原始报道；早报优先今天截至目前和最近24小时，晚报优先今天全天和最近48小时。"
        "只输出 Markdown 编号列表。"
        "每条格式固定为：标题｜一句话摘要｜实际日期｜来源｜链接。"
        "不要写前言，不要写结尾，不要代码块。"
    )


def build_war_prompt(target_date: dt.date, variant: str) -> str:
    label = variant_label(variant)
    return (
        f"搜索 {target_date.isoformat()} 的 10 条热门战争局势新闻，用于{label}。"
        "优先使用真实新闻源和原始报道；早报优先今天截至目前和最近24小时，晚报优先今天全天和最近48小时。"
        "只输出 Markdown 编号列表。"
        "每条格式固定为：标题｜一句话摘要｜实际日期｜来源｜链接。"
        "不要写前言，不要写结尾，不要代码块。"
    )


def build_polish_prompt(target_date: dt.date, raw_report: str, variant: str) -> str:
    label = variant_label(variant)
    return (
        f"请把下面这份 {target_date.isoformat()} 的新闻原始合并稿，改写成一封适合直接发送的中文{label}邮件。"
        "要求如下："
        "1. 全文必须是自然、优美、准确的中文，不要夹杂英文句式，标题也尽量翻成中文。"
        "2. 保留所有原始新闻条目，不要删减，不要新增未经原稿提供的事实。"
        "3. 每一条新闻都必须保留链接，并明确写出日期、来源、链接。"
        "4. 在开头增加一个“今日导读”小节，用2到3段概括当天最值得关注的变化。"
        "5. 保留两个主体板块：“AI 新闻”和“战争局势”。"
        "6. 每条新闻改成更适合阅读的短段落格式，建议结构为：小标题、摘要、日期、来源、链接。"
        "7. 在文末新增“AI 总结”小节，用4到6条要点总结当天 AI 领域的主线、趋势、竞争格局和值得跟踪的方向。"
        "8. 输出必须是 Markdown。不要写代码块，不要解释你的做法，不要输出原稿以外的额外说明。"
        "9. 文章标题使用“# 每日新闻简报”，并保留“生成时间”和“日期基准”两行。"
        "\n\n下面是原始合并稿：\n\n"
        f"{raw_report.strip()}\n"
    )


def run_codex(prompt: str, output_stem: str) -> tuple[str, pathlib.Path, pathlib.Path]:
    codex_bin = os.environ.get("CODEX_BIN", DEFAULT_CODEX_BIN)
    codex_model = os.environ.get("CODEX_MODEL", DEFAULT_CODEX_MODEL).strip()
    timeout_seconds = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1800"))
    max_attempts = int(os.environ.get("CODEX_MAX_ATTEMPTS", "3"))
    retry_delay_seconds = int(os.environ.get("CODEX_RETRY_DELAY_SECONDS", "20"))
    report_path = OUTPUT_DIR / f"{output_stem}.md"
    last_log_path: pathlib.Path | None = None
    last_error: str | None = None

    codex_path = pathlib.Path(codex_bin)
    if (codex_path.is_absolute() and not os.access(codex_path, os.X_OK)) or (
        not codex_path.is_absolute() and shutil.which(codex_bin) is None
    ):
        raise RuntimeError(
            f"Cannot find executable codex binary: {codex_bin}. Set CODEX_BIN or add codex to PATH."
        )

    child_env = os.environ.copy()
    if codex_path.is_absolute():
        codex_dir = str(codex_path.parent)
        current_path = child_env.get("PATH", "")
        child_env["PATH"] = codex_dir if not current_path else f"{codex_dir}:{current_path}"

    for attempt in range(1, max_attempts + 1):
        raw_log_path = LOG_DIR / f"{output_stem}.codex.attempt{attempt}.log"
        last_log_path = raw_log_path
        if report_path.exists():
            report_path.unlink()
        cmd = [
            codex_bin,
            "--search",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--cd",
            str(BASE_DIR),
            "-o",
            str(report_path),
        ]
        if codex_model:
            cmd.extend(["--model", codex_model])
        cmd.append(prompt)
        with raw_log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"# attempt {attempt}/{max_attempts}\n")
            log_file.write("$ " + " ".join(shlex.quote(part) for part in cmd) + "\n\n")
            log_file.flush()
            proc = subprocess.run(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=child_env,
                timeout=timeout_seconds,
                check=False,
            )
        if proc.returncode == 0 and report_path.exists():
            content = report_path.read_text(encoding="utf-8").strip()
            if content:
                return content, report_path, raw_log_path
            last_error = f"Codex produced an empty report: {report_path}"
        else:
            last_error = f"Codex exited with code {proc.returncode}; see {raw_log_path}"
        if attempt < max_attempts:
            write_run_log(f"{output_stem} attempt {attempt} failed; retrying in {retry_delay_seconds}s")
            time.sleep(retry_delay_seconds)

    raise RuntimeError(last_error or f"Codex failed; see {last_log_path}")


def assemble_raw_report(target_date: dt.date, variant: str, ai_section: str, war_section: str) -> str:
    generated_at = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "# 每日新闻原始合并稿\n"
        f"生成时间：{generated_at}\n"
        f"日期基准：{target_date.isoformat()}\n"
        f"简报类型：{variant_label(variant)}\n\n"
        "## AI 新闻（10条）\n"
        f"{ai_section.strip()}\n\n"
        "## 战争局势（10条）\n"
        f"{war_section.strip()}\n"
    )


def build_sample_report(target_date: dt.date, variant: str) -> str:
    generated_at = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")
    ai_lines = "\n\n".join(
        (
            f"### {idx}. AI 示例新闻 {idx}\n"
            f"这是一段用于 dry-run 的中文示例摘要，用来展示润色后的正文排版效果，方便直接检查邮件观感。\n"
            f"- 日期：{target_date.isoformat()}\n"
            f"- 来源：Example\n"
            f"- 链接：https://example.com/ai/{idx}"
        )
        for idx in range(1, 11)
    )
    war_lines = "\n\n".join(
        (
            f"### {idx}. 战争示例新闻 {idx}\n"
            f"这是一段用于 dry-run 的中文示例摘要，用来展示战争板块在最终邮件中的段落样式与链接保留方式。\n"
            f"- 日期：{target_date.isoformat()}\n"
            f"- 来源：Example\n"
            f"- 链接：https://example.com/war/{idx}"
        )
        for idx in range(1, 11)
    )
    return (
        "# 每日新闻简报\n"
        f"生成时间：{generated_at}\n"
        f"日期基准：{target_date.isoformat()}\n"
        f"简报类型：{variant_label(variant)}\n\n"
        "## 今日导读\n"
        "AI 产业新闻继续围绕模型能力、基础设施与企业落地展开，头部公司在模型、算力与平台层的竞争进一步加速。\n\n"
        "国际局势板块则更强调停火、谈判与局部冲突反复之间的拉扯，适合用一封邮件快速把握高风险区域的最新温度。\n\n"
        "## AI 新闻（10条）\n"
        f"{ai_lines}\n\n"
        "## 战争局势（10条）\n"
        f"{war_lines}\n\n"
        "## AI 总结\n"
        "- 模型能力、企业平台和算力基础设施仍然是 AI 新闻的三条主线。\n"
        "- 大厂竞争正从“单个模型发布”转向“模型、平台、数据、算力”一体化布局。\n"
        "- 企业用户更关注可治理、可集成、可观测的 AI 能力，而不只是参数规模。\n"
        "- 后续最值得跟踪的，是模型实际可用性、成本变化以及企业场景中的渗透速度。\n"
    )


def send_email(body_markdown: str, target_date: dt.date, variant: str) -> None:
    sender = require_env("SMTP_SENDER")
    recipient = require_env("SMTP_RECIPIENT")
    password = require_env("SMTP_APP_PASSWORD")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    subject_prefix = os.environ.get("MAIL_SUBJECT_PREFIX", "[WSL Daily News]")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Subject"] = f"{subject_prefix} {variant_label(variant)} {target_date.isoformat()}"
    msg.set_content(body_markdown, subtype="plain", charset="utf-8")

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def send_failure_email(error_message: str, target_date: dt.date, variant: str) -> None:
    subject_prefix = os.environ.get("MAIL_SUBJECT_PREFIX", "[WSL Daily News]")
    body = (
        "# 每日新闻任务失败\n\n"
        f"- 日期：{target_date.isoformat()}\n"
        f"- 简报类型：{variant_label(variant)}\n"
        f"- 错误：{error_message}\n"
        f"- 运行日志：{LOG_DIR / 'runner.log'}\n"
        f"- 输出目录：{OUTPUT_DIR}\n"
    )
    sender = require_env("SMTP_SENDER")
    recipient = require_env("SMTP_RECIPIENT")
    password = require_env("SMTP_APP_PASSWORD")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Subject"] = f"{subject_prefix} FAIL {variant_label(variant)} {target_date.isoformat()}"
    msg.set_content(body, subtype="plain", charset="utf-8")

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def sent_marker_path(target_date: dt.date, variant: str) -> pathlib.Path:
    return STATE_DIR / f"sent-{target_date.isoformat()}-{variant}.json"


def was_report_sent(target_date: dt.date, variant: str) -> bool:
    return sent_marker_path(target_date, variant).exists()


def record_successful_send(target_date: dt.date, variant: str, report_path: pathlib.Path) -> None:
    subject_prefix = os.environ.get("MAIL_SUBJECT_PREFIX", "[WSL Daily News]")
    payload = {
        "report_date": target_date.isoformat(),
        "variant": variant,
        "variant_label": variant_label(variant),
        "sent_at": now_local().isoformat(),
        "subject": f"{subject_prefix} {variant_label(variant)} {target_date.isoformat()}",
        "report_path": str(report_path),
    }
    sent_marker_path(target_date, variant).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def resolve_report_date(raw_date: str | None) -> dt.date:
    if raw_date:
        return dt.date.fromisoformat(raw_date)
    return now_local().date()


def write_run_log(message: str) -> None:
    stamp = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")
    with (LOG_DIR / "runner.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def main() -> int:
    args = parse_args()
    ensure_dirs()
    load_env_file(pathlib.Path(args.env_file))
    target_date = resolve_report_date(args.report_date)
    report_stem = f"daily-news-{target_date.isoformat()}-{args.variant}"
    report_path = OUTPUT_DIR / f"{report_stem}.md"
    try:
        if not args.skip_email and not args.force_send and was_report_sent(target_date, args.variant):
            write_run_log(f"email already sent for {report_stem}; skipping duplicate send")
            print(f"report already sent: {target_date.isoformat()} {args.variant}")
            return 0
        if args.dry_run:
            body = build_sample_report(target_date, args.variant)
            report_path.write_text(body, encoding="utf-8")
            write_run_log(f"dry-run generated {report_path}")
        else:
            ai_section, ai_path, ai_log_path = run_codex(build_ai_prompt(target_date, args.variant), f"{report_stem}-ai")
            write_run_log(f"ai section generated at {ai_path}; raw log {ai_log_path}")
            war_section, war_path, war_log_path = run_codex(build_war_prompt(target_date, args.variant), f"{report_stem}-war")
            write_run_log(f"war section generated at {war_path}; raw log {war_log_path}")
            raw_body = assemble_raw_report(target_date, args.variant, ai_section, war_section)
            raw_report_path = OUTPUT_DIR / f"{report_stem}-raw.md"
            raw_report_path.write_text(raw_body, encoding="utf-8")
            write_run_log(f"raw combined report generated at {raw_report_path}")
            body, report_path, polish_log_path = run_codex(build_polish_prompt(target_date, raw_body, args.variant), report_stem)
            write_run_log(f"polished report generated at {report_path}; raw log {polish_log_path}")
        if args.skip_email:
            write_run_log(f"email skipped for {report_stem}")
            print(f"report ready: {report_path}")
            return 0
        send_email(body, target_date, args.variant)
        record_successful_send(target_date, args.variant, report_path)
        write_run_log(f"email sent for {report_stem}")
        print(f"report sent: {report_path}")
        return 0
    except Exception as exc:
        write_run_log(f"failed: {exc}")
        if not args.skip_email and os.environ.get("SEND_FAILURE_EMAIL", "1").lower() not in {"0", "false", "no"}:
            try:
                send_failure_email(str(exc), target_date, args.variant)
                write_run_log(f"failure email sent for {report_stem}")
            except Exception as email_exc:
                write_run_log(f"failure email also failed: {email_exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
