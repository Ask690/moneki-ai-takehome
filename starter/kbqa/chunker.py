"""把文档切成检索用的小块。

按文档结构切，不做定长截断：定长切法会把一句话、一张表格拦腰截断，
既伤检索（关键词被切散），也伤引用（`quote` 要求是原文里连续的一段）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .loader import Document

#: 切块参数变了，索引缓存必须失效，所以写进缓存键里。
CHUNKER_VERSION = "chunker-3"

#: 一块的目标长度。超长块会单独成块，不再硬切。
CHUNK_SIZE = 300

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")
#: Markdown 表格行：以 `|` 开头或结尾。
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    text: str
    source_text: str
    heading: str = ""
    kind: str = "text"
    table_header: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "text": self.text,
            "source_text": self.source_text,
            "heading": self.heading,
            "kind": self.kind,
            "table_header": self.table_header,
        }


@dataclass
class _Block:
    """切块的最小单位：一个标题、一段正文，或一整张表格。"""

    text: str
    heading: str
    kind: str = "text"
    table_header: list[str] = field(default_factory=list)


def _split_blocks(text: str) -> list[_Block]:
    """把正文拆成块。标题另起一块，连续表格行并成一块，空行分段。"""
    blocks: list[_Block] = []
    heading = ""
    buffer: list[str] = []
    table: list[str] = []

    def flush_text() -> None:
        nonlocal buffer
        piece = "\n".join(buffer).strip("\n")
        if piece.strip():
            blocks.append(_Block(text=piece, heading=heading))
        buffer = []

    def flush_table() -> None:
        nonlocal table
        if not table:
            return
        rows = [line.strip() for line in table]
        header = [cell.strip() for cell in rows[0].strip("|").split("|")] if rows else []
        blocks.append(
            _Block(text="\n".join(rows), heading=heading, kind="table", table_header=header)
        )
        table = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _TABLE_ROW.match(line):
            flush_text()
            table.append(line)
            continue
        flush_table()

        match = _HEADING.match(line)
        if match:
            flush_text()
            heading = match.group(2).strip()
            blocks.append(_Block(text=line.strip(), heading=heading, kind="heading"))
            continue
        if not line.strip():
            flush_text()
            continue
        buffer.append(line)

    flush_table()
    flush_text()
    return blocks


def _pack(blocks: list[_Block]) -> list[_Block]:
    """把小块合并到 CHUNK_SIZE 左右；单块超长就让它自成一块，不切。"""
    packed: list[_Block] = []
    buffer: list[_Block] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        if not buffer:
            return
        if len(buffer) == 1:
            packed.append(buffer[0])
        else:
            packed.append(
                _Block(
                    text="\n\n".join(item.text for item in buffer),
                    heading=buffer[0].heading,
                    # 合并块里只要含表格就标成表格，引用时好挑原文。
                    kind="table" if any(item.kind == "table" for item in buffer) else "text",
                    table_header=next(
                        (item.table_header for item in buffer if item.table_header), []
                    ),
                )
            )
        buffer = []
        size = 0

    for block in blocks:
        length = len(block.text)
        if buffer and size + length > CHUNK_SIZE:
            flush()
        buffer.append(block)
        size += length
    flush()
    return packed


def chunk_document(document: Document) -> list[Chunk]:
    """按结构切一篇文档。"""
    blocks = _pack(_split_blocks(document.text))
    if not blocks:
        fallback = document.text.strip() or document.title
        blocks = [_Block(text=fallback, heading=document.title)]

    chunks: list[Chunk] = []
    for number, block in enumerate(blocks, start=1):
        # 标题块单独成块没有检索价值，把它并进下一块的开头更有用；
        # 这里保持原样，只在 text 前面补上所处的小节标题，便于命中“某节讲什么”。
        body = block.text
        chunks.append(
            Chunk(
                doc_id=document.doc_id,
                chunk_id="%s#%d" % (document.doc_id, number),
                text=body,
                # 引用要逐字核对，source_text 必须是文档里真实存在的连续文字，不能加料。
                source_text=body,
                heading=block.heading or document.title,
                kind=block.kind,
                table_header=block.table_header,
            )
        )
    return chunks


def chunk_documents(documents: list[Document]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document))
    return chunks
