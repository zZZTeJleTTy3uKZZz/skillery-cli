"""Unit tests для парсинга тегов в CLI publish (#397)."""
import pytest


class TestCliTagParsing:
    """Тесты парсинга тегов из --tags опции."""

    @staticmethod
    def _parse_tags(tags_str: str) -> list[str]:
        """Вспомогательная функция парсинга (из __main__.py)."""
        clean = tags_str.strip()
        clean = clean.lstrip("[{").rstrip("]}")
        return [t.strip() for t in clean.split(",") if t.strip()]

    def test_simple_comma_separated(self):
        """Простой список через запятую."""
        result = self._parse_tags("Backend, Python, AI")
        assert result == ["Backend", "Python", "AI"]

    def test_with_square_brackets(self):
        """Список в квадратных скобках (как из JSON/Python list repr)."""
        result = self._parse_tags("[Backend, Python, AI]")
        assert result == ["Backend", "Python", "AI"]

    def test_with_curly_braces(self):
        """Список в фигурных скобках (как из JSON object keys)."""
        result = self._parse_tags("{Backend, Python}")
        assert result == ["Backend", "Python"]

    def test_with_extra_whitespace(self):
        """Пробелы вокруг элементов должны быть обрезаны."""
        result = self._parse_tags("  [  Backend  ,  Python  ]  ")
        assert result == ["Backend", "Python"]

    def test_empty_string(self):
        """Пустая строка → пустой список."""
        result = self._parse_tags("")
        assert result == []

    def test_only_brackets(self):
        """Только скобки → пустой список."""
        result = self._parse_tags("[]")
        assert result == []
        result = self._parse_tags("{}")
        assert result == []

    def test_single_tag(self):
        """Одиночный тег."""
        result = self._parse_tags("Python")
        assert result == ["Python"]

    def test_single_tag_with_brackets(self):
        """Одиночный тег в скобках."""
        result = self._parse_tags("[Python]")
        assert result == ["Python"]

    def test_nested_brackets(self):
        """Вложенные скобки: убираются ВСЕ внешние скобки."""
        # lstrip("[{").rstrip("]}")  убирает ВСЕ скобки слева/справа
        result = self._parse_tags("[[Inner]]")
        # lstrip("[{").rstrip("]}")  → "Inner"
        # split(",")  → ["Inner"]
        # strip()  → "Inner"
        assert result == ["Inner"]

    def test_tags_with_numbers(self):
        """Теги с цифрами."""
        result = self._parse_tags("[Python3, Node.js, C++]")
        assert result == ["Python3", "Node.js", "C++"]
