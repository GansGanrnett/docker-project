"""Добавляет каталоги app/ и tests/ в sys.path, чтобы тесты импортировали
main и общие заглушки (fakes.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))