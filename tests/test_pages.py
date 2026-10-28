# -*- coding: utf-8 -*-
from __future__ import annotations

from pdf_translator.book import Book


def test_book_uses_zero_based_indices(project):
    book = Book(project.pdf)
    try:
        assert book.page_count == 4
        first = book.paragraphs(0)
        assert len(first) == 2
        assert "formation of Christendom" in first[0]
        assert book.paragraphs(1) and "Arian" in book.paragraphs(1)[0]
        assert book.paragraphs(2) == []            # 第 3 页无文本
        assert "medieval world" in book.paragraphs(3)[0]
    finally:
        book.close()
