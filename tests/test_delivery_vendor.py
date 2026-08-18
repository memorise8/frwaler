# -*- coding: utf-8 -*-
"""delivery/vendor 사본이 원본(레거시 디렉터리)과 동일한지 고정.

번들은 레거시 디렉터리(crawlers-share, libertree-app)를 제외하므로 Dockerfile 은
delivery/vendor 의 사본을 COPY 한다. 원본이 이 저장소에 남아 있는 동안에는 두
파일이 어긋나면 안 된다 — 어긋나는 순간 번들 빌드와 저장소 빌드가 다른 것을
담게 된다.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAIRS = [
    ("crawlers-share/requirements.txt", "delivery/vendor/crawler-runtime-requirements.txt"),
    ("libertree-app/src/lib/categories.ts", "delivery/vendor/libertree-lib/categories.ts"),
    ("libertree-app/src/lib/doc-type-map.generated.ts", "delivery/vendor/libertree-lib/doc-type-map.generated.ts"),
]


class VendorCopiesMatchTest(unittest.TestCase):
    def test_vendored_files_match_their_legacy_sources(self):
        for src, dst in PAIRS:
            with self.subTest(src=src):
                s, d = ROOT / src, ROOT / dst
                if not s.exists():
                    continue  # 레거시 원본이 정리된 뒤에는 사본이 유일본이다
                self.assertEqual(s.read_bytes(), d.read_bytes(),
                                 f"{dst} 가 {src} 와 다르다 -- 한쪽만 고쳐졌다")


if __name__ == "__main__":
    unittest.main()
