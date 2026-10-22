"""Compile README.md to self-contained HTML and a print-ready A4 PDF.

Requires Python Markdown and a local Chrome/Edge installation.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

import markdown


ROOT = Path(__file__).resolve().parents[1]
CSS = """
@page {
  size: A4; margin: 15mm 16mm 17mm;
  @bottom-right { content: counter(page); font: 9pt "Segoe UI", sans-serif; color: #667; }
}
* { box-sizing: border-box; }
body {
  max-width: 980px; margin: 0 auto; padding: 32px;
  color: #25343a; background: white;
  font: 15px/1.7 "Segoe UI", "Microsoft YaHei", "Segoe UI Emoji", sans-serif;
  overflow-wrap: anywhere;
}
h1, h2, h3 { color: #164e63; line-height: 1.4; break-after: avoid; }
h1 { font-size: 28px; margin: 0 0 24px; }
h2 { font-size: 21px; margin: 26px 0 12px; border-bottom: 1px solid #cddce1; padding-bottom: 6px; }
h3 { font-size: 17px; margin: 18px 0 8px; }
p, ul, ol { margin: 0 0 12px; }
ul, ol { padding-left: 1.6em; }
li { margin-bottom: 5px; }
li > p { margin-bottom: 4px; }
a { color: #126782; text-decoration: none; }
code { font-family: Consolas, "Microsoft YaHei", monospace; font-size: .9em; background: #f0f5f6; padding: 1px 3px; }
pre { padding: 12px; background: #f0f5f6; white-space: pre-wrap; }
blockquote { margin: 16px 0; padding: 12px 16px; border-left: 3px solid #4d8d8b; background: #f0f7f6; }
blockquote p:last-child { margin-bottom: 0; }
hr { border: 0; border-top: 1px solid #dbe4e7; margin: 22px 0; }
@media print {
  body { padding: 0; max-width: none; font-size: 10pt; line-height: 1.65; }
  h1 { font-size: 20pt; margin-bottom: 16px; }
  h2 { font-size: 14pt; margin-top: 18px; }
  h3 { font-size: 11.5pt; }
  p, li { orphans: 2; widows: 2; }
  li { break-inside: avoid; }
  blockquote, pre { break-inside: avoid; }
  a { color: inherit; }
}
"""


def find_browser():
    for candidate in (
        shutil.which("chrome"), shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("chromium"), shutil.which("chromium-browser"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("Chrome/Edge not found; pass --browser with its executable path.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Chrome/Edge executable")
    args = parser.parse_args()
    output = ROOT / "readme"
    output.mkdir(exist_ok=True)
    source = (ROOT / "README.md").read_text(encoding="utf-8-sig")
    # Root-relative documentation references need one level up in the export.
    source = source.replace("](docs/", "](../docs/")
    body = markdown.markdown(source, extensions=["extra", "sane_lists"])
    html_path = output / "README.html"
    pdf_path = output / "README.pdf"
    html_path.write_text(
        '<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Water-redstart v4.0 使用说明</title>'
        f"<style>{CSS}</style></head><body>\n{body}\n</body></html>\n",
        encoding="utf-8",
    )
    scratch = ROOT / "_local" / "build" / "readme-render"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="browser-", dir=scratch) as profile:
        staged_pdf = Path(profile) / "README.pdf"
        command = [
            args.browser or find_browser(), "--headless", "--disable-gpu",
            "--no-first-run", "--no-default-browser-check",
            "--no-pdf-header-footer", "--allow-file-access-from-files",
            f"--user-data-dir={profile}", f"--print-to-pdf={staged_pdf}",
            html_path.as_uri(),
        ]
        result = subprocess.run(
            command, capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not staged_pdf.exists():
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        # Copy bytes out of the browser profile; moving the browser-created
        # file can carry its restricted Windows ACL into the public export.
        shutil.copyfile(staged_pdf, pdf_path)
    print(f"HTML: {html_path}")
    print(f"PDF:  {pdf_path}")


if __name__ == "__main__":
    main()
