#!/usr/bin/env python3
"""Validate html-prd outputs with only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_:-]+\}\}")
PAGE_ID_RE = re.compile(r"\bP\d{2,}\b", re.IGNORECASE)
QUESTION_ID_RE = re.compile(r"\bQ\d{2,}\b", re.IGNORECASE)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_files(source: Path, excluded: set[Path] | None = None) -> list[Path]:
    excluded = {item.resolve() for item in (excluded or set())}
    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == item or item in resolved.parents for item in excluded)
    if source.is_file():
        return [] if is_excluded(source) else [source]
    return sorted(
        item for item in source.rglob("*")
        if item.is_file() and not is_excluded(item)
    )


def relative_key(path: Path, source: Path) -> str:
    if source.is_file():
        return path.name
    return path.relative_to(source).as_posix()


def snapshot(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    manifest = args.manifest.resolve()
    if not source.exists():
        print(f"ERROR: 输入路径不存在：{source}")
        return 1

    exclusions = {
        (source if source.is_dir() else source.parent) / item
        if not item.is_absolute() else item
        for item in args.exclude
    }
    exclusions.add(manifest)
    files = source_files(source, exclusions)
    if not files:
        print("ERROR: 排除后没有可校验的源文件")
        return 1
    payload = {
        "source": str(source),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "excluded": sorted(str(item.resolve()) for item in exclusions if item.resolve() != manifest),
        "files": [
            {
                "path": relative_key(path, source),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files
        ],
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: 已记录 {len(files)} 个源文件哈希：{manifest}")
    if source.is_file():
        print("WARN: 单文件输入只记录该文件；相邻 CSS、JavaScript、图片等依赖未纳入哈希")
    return 0


class ReviewHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.iframes: list[dict[str, str]] = []
        self.nav_pages: list[str] = []
        self.note_pages: list[str] = []
        self.note_sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key: value or "" for key, value in attrs}
        if tag == "iframe":
            self.iframes.append(data)
        if tag == "button" and data.get("data-page"):
            self.nav_pages.append(data["data-page"].upper())
        if tag in {"article", "section"} and data.get("data-page"):
            self.note_pages.append(data["data-page"].upper())
            if data.get("data-source"):
                self.note_sources.append(data["data-source"])


def local_target(review_path: Path, value: str) -> Path | None:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https", "data", "javascript"}:
        return None
    if parsed.scheme == "file":
        return Path(unquote(parsed.path.lstrip("/")))
    clean = unquote(parsed.path)
    return (review_path.parent / clean).resolve()


def verify_manifest(manifest_path: Path, source: Path, errors: list[str]) -> set[Path]:
    tracked: set[Path] = set()
    try:
        payload = json.loads(read_text(manifest_path))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"无法读取源文件哈希清单：{exc}")
        return tracked

    manifest_source = Path(payload.get("source", "")).resolve()
    if manifest_source != source.resolve():
        errors.append(f"哈希清单输入路径不匹配：{manifest_source}")
        return tracked

    for item in payload.get("files", []):
        path = source if source.is_file() else source / item["path"]
        if source.is_file() and path.name != item["path"]:
            errors.append(f"哈希清单文件名不匹配：{item['path']}")
            continue
        tracked.add(path.resolve())
        if not path.exists():
            errors.append(f"源文件已缺失：{path}")
            continue
        current = sha256(path)
        if current != item.get("sha256"):
            errors.append(f"源文件发生变化：{path}")
    return tracked


def validate_text_file(path: Path, errors: list[str]) -> str:
    try:
        text = read_text(path)
    except OSError as exc:
        errors.append(f"无法读取文件 {path}：{exc}")
        return ""
    placeholders = sorted(set(PLACEHOLDER_RE.findall(text)))
    if placeholders:
        errors.append(f"{path.name} 仍有模板占位符：{', '.join(placeholders)}")
    return text


def check(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    output_dir = args.output_dir.resolve()
    prd_path = output_dir / args.prd_name
    review_path = output_dir / args.review_name
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    if not source.exists():
        errors.append(f"输入路径不存在：{source}")

    protected_source_files: set[Path] = set()
    if args.manifest:
        error_count = len(errors)
        protected_source_files = verify_manifest(args.manifest.resolve(), source, errors)
        if len(errors) == error_count:
            checks.append("受保护文件哈希与生成前记录一致")
    else:
        warnings.append("未提供源文件哈希清单，无法核对生成前后是否变化")
        if source.is_file() and source.exists():
            protected_source_files = {source.resolve()}
        elif source.is_dir() and not output_dir.is_relative_to(source):
            protected_source_files = {path.resolve() for path in source_files(source)}

    if source.is_file():
        warnings.append("单文件输入只检查该文件；本地依赖未纳入源文件哈希对比")

    for output_path in (prd_path, review_path):
        if output_path.resolve() in protected_source_files:
            errors.append(f"输出文件覆盖了源文件：{output_path}")

    md_text = ""
    html_text = ""
    md_pages: set[str] = set()
    html_pages: set[str] = set()

    if args.mode in {"md", "both"}:
        if not prd_path.exists():
            errors.append(f"缺少 Markdown 产物：{prd_path}")
        else:
            md_text = validate_text_file(prd_path, errors)
            md_pages = {item.upper() for item in PAGE_ID_RE.findall(md_text)}
            if not md_pages:
                errors.append(f"{prd_path.name} 未找到 P 编号页面")
            else:
                checks.append(f"Markdown 包含 {len(md_pages)} 个页面编号")

    if args.mode in {"html", "both"}:
        if not review_path.exists():
            errors.append(f"缺少 HTML 评审版：{review_path}")
        else:
            html_text = validate_text_file(review_path, errors)
            parser = ReviewHTMLParser()
            parser.feed(html_text)
            nav_pages = set(parser.nav_pages)
            note_pages = set(parser.note_pages)
            html_pages = nav_pages | note_pages

            if not parser.iframes:
                errors.append("HTML 评审版缺少 iframe 原页面区域")
            if not nav_pages:
                errors.append("HTML 评审版缺少 data-page 页面切换按钮")
            if not note_pages:
                errors.append("HTML 评审版缺少带 data-page 的页面说明区")
            if nav_pages != note_pages:
                errors.append(
                    "页面目录与说明区编号不一致："
                    f"目录={sorted(nav_pages)}，说明={sorted(note_pages)}"
                )
            if "activatePage" not in html_text and "showPage" not in html_text:
                errors.append("HTML 评审版缺少页面切换函数")

            sources = list(parser.note_sources)
            sources.extend(item.get("src", "") for item in parser.iframes if item.get("src"))
            for value in sorted(set(sources)):
                target = local_target(review_path, value)
                if target is not None and not target.exists():
                    errors.append(f"HTML 评审版引用的本地页面不存在：{value}")
            if html_pages:
                checks.append(f"HTML 评审版包含 {len(html_pages)} 个页面编号")

    if args.mode == "both" and md_text and html_text:
        if md_pages != html_pages:
            errors.append(
                "Markdown 与 HTML 页面编号不一致："
                f"Markdown={sorted(md_pages)}，HTML={sorted(html_pages)}"
            )
        else:
            checks.append("Markdown 与 HTML 页面编号一致")

        md_questions = {item.upper() for item in QUESTION_ID_RE.findall(md_text)}
        html_questions = {item.upper() for item in QUESTION_ID_RE.findall(html_text)}
        if md_questions != html_questions:
            errors.append(
                "Markdown 与 HTML 待确认编号不一致："
                f"Markdown={sorted(md_questions)}，HTML={sorted(html_questions)}"
            )
        else:
            checks.append("Markdown 与 HTML 待确认编号一致")

    report = {
        "ok": not errors,
        "mode": args.mode,
        "source": str(source),
        "output_dir": str(output_dir),
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for item in checks:
        print(f"PASS: {item}")
    for item in warnings:
        print(f"WARN: {item}")
    for item in errors:
        print(f"ERROR: {item}")
    print("RESULT: PASS" if not errors else "RESULT: FAIL")
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验 html-prd 输出文件")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="记录源文件 SHA-256")
    snapshot_parser.add_argument("--source", type=Path, required=True)
    snapshot_parser.add_argument("--manifest", type=Path, required=True)
    snapshot_parser.add_argument("--exclude", type=Path, action="append", default=[],
                                 help="排除产物、过程记录或其他非输入路径；可重复使用")
    snapshot_parser.set_defaults(func=snapshot)

    check_parser = subparsers.add_parser("check", help="检查 PRD 输出和源文件完整性")
    check_parser.add_argument("--source", type=Path, required=True)
    check_parser.add_argument("--output-dir", type=Path, required=True)
    check_parser.add_argument("--mode", choices=("md", "html", "both"), required=True)
    check_parser.add_argument("--manifest", type=Path)
    check_parser.add_argument("--prd-name", default="PRD.md")
    check_parser.add_argument("--review-name", default="PRD评审版.html")
    check_parser.add_argument("--report", type=Path)
    check_parser.set_defaults(func=check)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
