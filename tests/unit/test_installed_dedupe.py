"""#1058: навыки задваивались в `--installed` (global + project с ОДНИМ путём).

`--installed` сканирует global (`~/.claude/skills/`) и project
(`<project>/.claude/skills/`) РАЗДЕЛЬНО. Когда project scope резолвится в ту же
физическую папку, что и global (команда из home; или `.claude/skills` —
junction/symlink на глобальную), один навык попадал в вывод дважды. Дедуп по
realpath+normcase схлопывает такие дубли, не трогая реально раздельные установки.
"""
from __future__ import annotations

import os

import pytest

from skillery_cli.__main__ import _dedupe_installed_by_realpath


def _item(path: str, scope: str, slug: str = "atlas") -> dict:
    return {"slug": slug, "ref": slug, "version": "1.0.0", "scope": scope, "path": path}


class TestDedupeInstalled:
    def test_same_path_global_and_project_collapse(self, tmp_path):
        """Один навык в global и project с ОДНИМ путём → один ряд (был баг)."""
        p = str(tmp_path / ".claude" / "skills" / "atlas")
        items = [_item(p, "global"), _item(p, "project")]

        out = _dedupe_installed_by_realpath(items)

        assert len(out) == 1, "одна физическая папка не должна давать два ряда"
        assert out[0]["scope"] == "global", "остаётся канонический global (первый)"

    def test_distinct_physical_paths_are_kept(self, tmp_path):
        """Реально разные папки global vs project — оба ряда сохраняются."""
        g = str(tmp_path / "home" / ".claude" / "skills" / "atlas")
        pr = str(tmp_path / "proj" / ".claude" / "skills" / "atlas")
        items = [_item(g, "global"), _item(pr, "project")]

        out = _dedupe_installed_by_realpath(items)

        assert len(out) == 2, "раздельные установки схлопывать нельзя"

    def test_symlink_project_to_global_collapses(self, tmp_path):
        """project-папка — symlink на global → один навык (realpath резолвит)."""
        real = tmp_path / "global" / "atlas"
        real.mkdir(parents=True)
        link = tmp_path / "proj" / "atlas"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink не поддерживается в этой среде")

        items = [_item(str(real), "global"), _item(str(link), "project")]
        out = _dedupe_installed_by_realpath(items)

        assert len(out) == 1

    @pytest.mark.skipif(os.name != "nt", reason="регистронезависимость — Windows")
    def test_case_insensitive_on_windows(self, tmp_path):
        """Windows регистронезависим: два регистра одного пути → один навык."""
        base = tmp_path / "skills" / "atlas"
        base.mkdir(parents=True)
        p = str(base)
        items = [_item(p.upper(), "global"), _item(p.lower(), "project")]

        out = _dedupe_installed_by_realpath(items)

        assert len(out) == 1

    def test_robust_to_bad_path(self):
        """Пустой/битый путь не роняет листинг."""
        out = _dedupe_installed_by_realpath(
            [_item("", "global"), {"slug": "b", "scope": "project"}]
        )
        assert isinstance(out, list)
