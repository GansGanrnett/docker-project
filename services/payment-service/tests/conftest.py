"""Добавляет каталог app/ в sys.path, чтобы тесты импортировали main."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))