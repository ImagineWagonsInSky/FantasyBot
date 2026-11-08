"""A minimal HTML tree on the standard library's parser.

Enough to read futbolfantasy.com's lineup pages: elements, attributes,
classes, text, siblings. No scripts run and nothing is fetched while
parsing; it's a pure function from text to a tree, which keeps a scraped
third-party page from needing another dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
        "track", "wbr"}
MAX_DEPTH = 400  # a hostile page can't make the tree arbitrarily deep


class Node:
    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def has_classes(self, *names: str) -> bool:
        return set(names) <= self.classes

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    def text(self) -> str:
        parts: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in ("script", "style"):
                stack.extend(reversed(item.children))
        return " ".join(" ".join(parts).split())

    def iter(self) -> Iterator[Node]:
        stack: list[Node] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed([c for c in node.children if isinstance(c, Node)]))

    def find_all(self, match: Callable[[Node], bool]) -> list[Node]:
        return [n for n in self.iter() if n is not self and match(n)]

    def find(self, match: Callable[[Node], bool]) -> Node | None:
        return next((n for n in self.iter() if n is not self and match(n)), None)

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    @property
    def previous_element(self) -> Node | None:
        """The previous sibling element (DOM previousElementSibling)."""
        if self.parent is None:
            return None
        siblings = [c for c in self.parent.children if isinstance(c, Node)]
        i = next(i for i, c in enumerate(siblings) if c is self)
        return siblings[i - 1] if i > 0 else None


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in VOID and len(self._stack) < MAX_DEPTH:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}, self._stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        # Close up to the matching open element; a stray end tag is ignored.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root
