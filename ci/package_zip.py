# -*- coding: utf-8 -*-
"""설치 프로그램을 ZIP 하나로 감싼다 (exe 직접 다운로드가 막히는 브라우저 대비책).

    python ci/package_zip.py installer/youtube-score-pdf-setup-1.1.0.exe out

1.1.0 이전에는 --onedir 폴더 전체를 압축했다. 1.1.0 에 복사 방지가 들어가면서
그 ZIP 은 더 이상 쓸 수 없다: 압축을 푼 폴더는 정의상 "설치하지 않고 복사한
폴더" 이고, 프로그램이 거부한다. 그래서 ZIP 안에 드는 것은 프로그램 폴더가
아니라 설치 파일 자체다. 브라우저가 .exe 다운로드를 막아도 ZIP 은 받아지고,
압축을 풀면 설치 파일이 나오며, 그것을 실행하면 정상 설치된다.

최상위 이름은 ASCII 로 고정한다. 탐색기가 ZIP 안의 한글 이름을 못 읽는 경우가
아직 있고, 하필 그게 실행 파일이면 그대로 사고이기 때문이다.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ytscore import config  # noqa: E402

INSIDE_README = """유튜브 악보 PDF 변환기 - 설치 방법
=====================================

이 ZIP 안에 있는 {setup} 을 실행하면 설치됩니다.

* 설치 파일로 설치하셔야 프로그램이 동작합니다.
* 설치한 폴더를 다른 컴퓨터로 복사해서 실행하면 동작하지 않습니다.
  그 컴퓨터에서도 이 설치 파일을 한 번 실행해 주세요.
* 설치할 수 있는 PC 대수에는 제한이 없습니다.

자세한 사용법은 설치 후 시작 메뉴의 "사용 설명서" 를 참고해 주세요.
"""


def build(installer: Path, out_dir: Path) -> Path:
    if not installer.is_file():
        raise SystemExit(f"installer not found: {installer}")
    top = f"{config.APP_SLUG}-setup-{config.APP_VERSION}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{top}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(installer, f"{top}/{installer.name}")
        zf.writestr(f"{top}/설치방법.txt", INSIDE_README.format(setup=installer.name))
        readme = ROOT / "readme-ko.txt"
        if readme.is_file():
            zf.writestr(f"{top}/사용안내.txt", readme.read_text(encoding="utf-8"))
    print(f"packaged {zip_path} ({zip_path.stat().st_size:,} bytes) "
          f"wrapping {installer.name}")
    return zip_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: package_zip.py <installer.exe> [outdir]")
    build(Path(sys.argv[1]).resolve(),
          Path(sys.argv[2] if len(sys.argv) > 2 else "out").resolve())
