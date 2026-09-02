"""Colouring a command line as it is typed.

One classifier, two consumers: the popup's editor (a real QSyntaxHighlighter)
and the navigator's painted command line. Both call `spans()`, so the two
prompts can never drift apart in what they consider valid.

  green (accent)  the first word names a real command
  red             it does not
  white           a modifier
  blue            a quoted modifier
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat, QTextDocument

from ..commands import is_command, tokenize
from .paint import ACCENT, BAD, QUOTED, TEXT

OK = "ok"
UNKNOWN = "unknown"
ARG = "arg"
QUOTE = "quote"


def spans(line: str) -> list[tuple[int, int, str]]:
    """(start, end, role) for each token, in order."""
    out: list[tuple[int, int, str]] = []
    for position, token in enumerate(tokenize(line)):
        if position == 0 and not token.quoted:
            role = OK if is_command(token.text) else UNKNOWN
        elif token.quoted:
            role = QUOTE
        else:
            role = ARG
        out.append((token.start, token.end, role))
    return out


def colour_for(role: str) -> QColor:
    if role == OK:
        return ACCENT
    if role == UNKNOWN:
        return BAD
    if role == QUOTE:
        return QUOTED
    return TEXT


class CommandHighlighter(QSyntaxHighlighter):
    """Applies `spans()` to the popup's editor."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)

    def highlightBlock(self, text: str) -> None:
        for start, end, role in spans(text):
            fmt = QTextCharFormat()
            fmt.setForeground(colour_for(role))
            if role == OK:
                fmt.setFontWeight(700)
            self.setFormat(start, end - start, fmt)
