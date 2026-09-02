# -*- coding: utf-8 -*-
"""--onedir 빌드를 ZIP 하나로 묶는다 (설치 프로그램을 못 쓰는 경우의 대비책).

    python ci/package_zip.py dist/youtube-score-pdf out

최상위 폴더 이름과 exe 이름은 ASCII 로 고정한다. 탐색기가 ZIP 안의 한글 이름을
못 읽는 경우가 아직 있고, 하필 그게 실행 파일이면 그대로 사고이기 때문이다.
안내문은 이름이 깨져도 열리기만 하면 되므로 한글 이름을 쓴다.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ytscore import config  # noqa: E402


def build(dist_dir: Path, out_dir: Path) -> Path:
    exe = dist_dir / f"{config.APP_SLUG}.exe"
    if not exe.is_file():
        raise SystemExit(f"exe not found: {exe}")
    top = f"{config.APP_SLUG}-{config.APP_VERSION}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{top}.zip"
    if zip_path.exists():
        zip_path.unlink()
    files = sorted(p for p in dist_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            zf.write(p, f"{top}/{p.relative_to(dist_dir).as_posix()}")
        readme = ROOT / "readme-ko.txt"
        if readme.is_file():
            zf.writestr(f"{top}/사용안내.txt", readme.read_text(encoding="utf-8"))
    print(f"packaged {zip_path} ({zip_path.stat().st_size:,} bytes, {len(files) + 1} entries)")
    return zip_path


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "dist/youtube-score-pdf").resolve(),
          Path(sys.argv[2] if len(sys.argv) > 2 else "out").resolve())
