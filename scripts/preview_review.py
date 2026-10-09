"""Serve a review file and its source prototype from one local origin.

Only the explicitly supplied source and output trees are readable. This is
needed because browsers restrict reading iframe navigation under file://.
"""

import argparse
import os
import sys
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class ReviewHandler(SimpleHTTPRequestHandler):
    allowed_roots: tuple[Path, Path] = ()
    common_root: Path = Path.cwd()

    def send_head(self):
        relative = unquote(urlsplit(self.path).path).lstrip("/")
        target = (self.common_root / relative).resolve()
        if not any(inside(target, root) for root in self.allowed_roots):
            self.send_error(403, "This path is outside the review and source folders")
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Open an HTML PRD review with page-note sync")
    parser.add_argument("--source-root", required=True, type=Path,
                        help="smallest directory containing the source HTML and local dependencies")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--review-name", default="PRD评审版.html")
    parser.add_argument("--no-open", action="store_true", help="do not launch a browser")
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    output_dir = args.output_dir.resolve()
    review_file = (output_dir / args.review_name).resolve()
    if not source_root.is_dir() or not output_dir.is_dir():
        parser.error("source-root and output-dir must be existing directories")
    if not inside(review_file, output_dir) or not review_file.is_file() or review_file.suffix.lower() != ".html":
        parser.error("review-name must identify an HTML file inside output-dir")
    try:
        common_root = Path(os.path.commonpath([source_root, output_dir])).resolve()
    except ValueError:
        parser.error("source-root and output-dir must be on the same drive for this preview")

    handler = type("ScopedReviewHandler", (ReviewHandler,), {
        "allowed_roots": (source_root, output_dir), "common_root": common_root,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(common_root)))
    relative_url = review_file.relative_to(common_root).as_posix()
    url = f"http://127.0.0.1:{server.server_port}/{quote(relative_url)}"
    print(f"评审版地址：{url}", flush=True)
    print("仅本机可访问；按 Ctrl+C 结束预览。", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
