from typing import Final

from .models import LawTarget

# (법률명, 국세/지방세). 각 항목은 법률/시행령/시행규칙 3종으로 전개된다.
_NATIONAL_BASES: Final[tuple[str, ...]] = (
    "국세기본법", "국세징수법", "법인세법", "소득세법", "부가가치세법",
    "상속세 및 증여세법", "조세특례제한법", "종합부동산세법",
    "국제조세조정에 관한 법률", "개별소비세법", "교육세법",
    "농어촌특별세법", "증권거래세법", "인지세법", "주세법",
)
_LOCAL_BASES: Final[tuple[str, ...]] = (
    "지방세기본법", "지방세징수법", "지방세법", "지방세특례제한법",
)

# 시행령/시행규칙 접미사 규칙: "X법" → "X법 시행령" / "X법 시행규칙",
#   "...에 관한 법률" → "...에 관한 법률 시행령" 등 단순 접미.
_SUFFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("법률", ""),
    ("시행령", " 시행령"),
    ("시행규칙", " 시행규칙"),
)


def _expand(bases: tuple[str, ...], scope: str) -> list[LawTarget]:
    out: list[LawTarget] = []
    for base in bases:
        for category, suffix in _SUFFIXES:
            # name은 기본 법령명; category suffix는 law.go.kr 검색 시 law_search_name()으로 합산
            out.append(LawTarget(name=base, category=category, tax_scope=scope))
    return out


def law_search_name(target: LawTarget) -> str:
    """law.go.kr 검색에 사용할 전체 법령명 (예: '법인세법 시행령')."""
    suffix_map = {"법률": "", "시행령": " 시행령", "시행규칙": " 시행규칙"}
    return f"{target.name}{suffix_map.get(target.category, '')}"


LAW_TARGETS: Final[tuple[LawTarget, ...]] = tuple(
    _expand(_NATIONAL_BASES, "국세") + _expand(_LOCAL_BASES, "지방세")
)
