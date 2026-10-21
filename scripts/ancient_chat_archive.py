#!/usr/bin/env python3
"""Import and search old chat archives from Adium, iChat/AIM, and future sources."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import plistlib
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


DB = Path("data") / "ancient-chat.db"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SCHEMA = """
PRAGMA journal_mode=WAL;

DROP TRIGGER IF EXISTS messages_ai;
DROP TRIGGER IF EXISTS messages_ad;
DROP TRIGGER IF EXISTS messages_au;
DROP TABLE IF EXISTS message_fts;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    service TEXT,
    account TEXT,
    contact TEXT,
    title TEXT,
    path TEXT,
    first_message_at TEXT,
    last_message_at TEXT,
    message_count INTEGER DEFAULT 0,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    service TEXT,
    account TEXT,
    contact TEXT,
    sender TEXT,
    sent_at TEXT,
    kind TEXT,
    text TEXT,
    path TEXT,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_conversations_source_contact ON conversations(source, contact);
"""


@dataclass
class ParsedMessage:
    source: str
    service: str
    account: str
    contact: str
    sender: str
    sent_at: str
    kind: str
    text: str
    path: str
    raw: dict[str, Any]


def connect(db_path: Path = DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", html.unescape(value)).strip()
    if isinstance(value, bytes):
        return clean_text(value.decode("utf-8", "ignore"))
    if isinstance(value, Iterable) and not isinstance(value, (dict, str, bytes)):
        return clean_text(" ".join(clean_text(x) for x in value))
    return clean_text(str(value))


def stable_id(*parts: Any) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(clean_text(part).encode("utf-8", "ignore"))
        h.update(b"\0")
    return h.hexdigest()


def parse_time_from_filename(path: Path) -> str:
    name = path.name
    patterns = [
        r"\((\d{4}-\d{2}-\d{2}T\d{2}\.\d{2}\.\d{2}(?:[+-]\d{4})?)\)",
        r"\((\d{4}-\d{2}-\d{2})\)",
        r" on (\d{4}-\d{2}-\d{2}) at (\d{2})\.(\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, name)
        if not match:
            continue
        try:
            if len(match.groups()) == 3:
                return datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}:{match.group(3)}:00").isoformat()
            raw = match.group(1)
            if "T" in raw:
                raw = raw.replace(".", ":", 2)
                raw = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw)
            return datetime.fromisoformat(raw).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return ""


def contact_from_filename(path: Path) -> str:
    name = path.name
    for sep in (" on ", " ("):
        if sep in name:
            return clean_text(name.split(sep, 1)[0])
    return clean_text(path.stem)


def infer_adium_meta(path: Path) -> tuple[str, str, str]:
    parts = path.parts
    account = ""
    service = ""
    contact = contact_from_filename(path)
    for part in parts:
        if "." in part and part.lower().startswith(("aim.", "msn.", "jabber.", "icq.", "yahoo.")):
            service, account = part.split(".", 1)
            service = service.upper()
            break
    try:
        if path.parent.suffix.lower() == ".chatlog":
            contact = clean_text(path.parent.parent.name) or contact
        else:
            contact = clean_text(path.parent.name) or contact
    except Exception:
        pass
    return service, account, contact


def strip_xml_text(element: ET.Element) -> str:
    return clean_text(" ".join(element.itertext()))


def parse_adium_xml(path: Path) -> list[ParsedMessage]:
    service, account, contact = infer_adium_meta(path)
    try:
        root = ET.parse(path).getroot()
    except Exception:
        text = path.read_text(encoding="utf-8", errors="ignore")
        root = ET.fromstring(re.sub(r"xmlns=\"[^\"]+\"", "", text, count=1))
    service = clean_text(root.attrib.get("service")) or service
    account = clean_text(root.attrib.get("account")) or account
    messages: list[ParsedMessage] = []
    for index, child in enumerate(root):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag not in {"message", "status", "event"}:
            continue
        text = strip_xml_text(child)
        if tag == "event" and not text:
            text = clean_text(child.attrib.get("type"))
        if not text:
            continue
        messages.append(
            ParsedMessage(
                source="adium",
                service=service,
                account=account,
                contact=contact,
                sender=clean_text(child.attrib.get("sender")),
                sent_at=clean_text(child.attrib.get("time")) or parse_time_from_filename(path),
                kind=tag,
                text=text,
                path=str(path),
                raw={"index": index, "attributes": dict(child.attrib)},
            )
        )
    return messages


class AdiumHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.stack: list[str] = []
        self.capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        cls = attr.get("class", "")
        if tag == "div" and cls in {"send", "receive", "status"}:
            self.current = {"kind": cls, "timestamp": "", "sender": "", "message": ""}
            self.stack = ["div"]
            self.capture = "message" if cls == "status" else None
            return
        if self.current is None:
            return
        self.stack.append(tag)
        if tag == "span" and cls == "timestamp":
            self.capture = "timestamp"
        elif tag == "span" and cls == "sender":
            self.capture = "sender"
        elif tag in {"pre", "span", "p", "div"} and cls == "message":
            self.capture = "message"
        elif tag == "br":
            self.current["message"] += "\n"

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag == "div" and self.stack == ["div"]:
            self.records.append(self.current)
            self.current = None
            self.capture = None
            self.stack = []
            return
        if self.stack:
            self.stack.pop()
        self.capture = None

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture] += data


def parse_adium_html(path: Path) -> list[ParsedMessage]:
    service, account, contact = infer_adium_meta(path)
    parser = AdiumHTMLParser()
    parser.feed(path.read_text(encoding="utf-8", errors="ignore"))
    base_date = parse_time_from_filename(path)
    messages: list[ParsedMessage] = []
    for index, rec in enumerate(parser.records):
        text = clean_text(rec.get("message"))
        if not text:
            continue
        sender = clean_text(rec.get("sender")).rstrip(":")
        messages.append(
            ParsedMessage(
                source="adium",
                service=service,
                account=account,
                contact=contact,
                sender=sender,
                sent_at=base_date,
                kind=clean_text(rec.get("kind")) or "message",
                text=text,
                path=str(path),
                raw={"index": index, "timestamp": clean_text(rec.get("timestamp"))},
            )
        )
    return messages


NOISE_STRINGS = {
    "bplist00",
    "typedstream",
    "NSArray",
    "NSObject",
    "NSString",
    "NSMutableArray",
    "NSDictionary",
    "NSMutableDictionary",
    "NSAttributedString",
    "NSArchiver",
    "NSKeyedArchiver",
    "NSDate",
    "NSColor",
    "NSFont",
    "Helvetica",
    "InstantMessage",
    "Presentity",
    "DirectlyObservableObject",
}


APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def plist_uid_index(value: Any) -> int | None:
    if isinstance(value, plistlib.UID):
        return value.data
    return None


def plist_obj(objects: list[Any], value: Any) -> Any:
    index = plist_uid_index(value)
    if index is None or index < 0 or index >= len(objects):
        return value
    return objects[index]


def plist_classname(objects: list[Any], value: Any) -> str:
    obj = plist_obj(objects, value)
    if not isinstance(obj, dict):
        return ""
    class_obj = plist_obj(objects, obj.get("$class"))
    if isinstance(class_obj, dict):
        return clean_text(class_obj.get("$classname"))
    return ""


def plist_string(objects: list[Any], value: Any) -> str:
    obj = plist_obj(objects, value)
    if isinstance(obj, str):
        return clean_text(obj)
    if isinstance(obj, bytes):
        return clean_text(obj)
    if isinstance(obj, dict):
        if "NSString" in obj:
            return plist_string(objects, obj.get("NSString"))
        if "NS.string" in obj:
            return plist_string(objects, obj.get("NS.string"))
    return clean_text(obj) if obj is not None else ""


def plist_date(objects: list[Any], value: Any) -> str:
    obj = plist_obj(objects, value)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict) and "NS.time" in obj:
        try:
            return (APPLE_EPOCH + timedelta(seconds=float(obj["NS.time"]))).isoformat()
        except (TypeError, ValueError, OverflowError):
            return ""
    return ""


def plist_presentity_id(objects: list[Any], value: Any) -> str:
    obj = plist_obj(objects, value)
    if not isinstance(obj, dict):
        return ""
    return plist_string(objects, obj.get("ID"))


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return clean_text("".join(self.parts))


def html_to_text(value: str) -> str:
    parser = HTMLTextExtractor()
    try:
        parser.feed(value)
        return parser.text()
    except Exception:
        return clean_text(re.sub(r"<[^>]+>", " ", value))


def printable_strings(data: bytes, min_len: int = 3, unique: bool = True) -> list[str]:
    strings: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(rb"[\x20-\x7e]{" + str(min_len).encode() + rb",}", data):
        value = clean_text(match.group().decode("latin1", "ignore"))
        if not value or value in seen or value in NOISE_STRINGS:
            continue
        if value.startswith("$") or value.startswith("NS"):
            continue
        if re.fullmatch(r"[A-Za-z0-9_.:$+-]{1,2}", value):
            continue
        if unique:
            seen.add(value)
        strings.append(value)
    return strings


def parse_ichat_blob(path: Path) -> list[ParsedMessage]:
    data = path.read_bytes()
    strings = printable_strings(data)
    text_parts: list[str] = []
    for value in strings:
        if "<html" in value.lower():
            value = re.sub(r"<[^>]+>", " ", value)
        value = clean_text(value)
        if value:
            text_parts.append(value)
    text = "\n".join(text_parts)
    if not text:
        return []
    contact = contact_from_filename(path)
    return [
        ParsedMessage(
            source="ichat",
            service="AIM/iChat",
            account="",
            contact=contact,
            sender="",
            sent_at=parse_time_from_filename(path),
            kind=path.suffix.lstrip(".") + "-text-blob",
            text=text,
            path=str(path),
            raw={"parser": "printable-string-fallback", "string_count": len(text_parts)},
        )
    ]


def parse_ichat_keyed_archive(path: Path) -> list[ParsedMessage]:
    try:
        archive = plistlib.load(path.open("rb"))
    except Exception:
        return []
    if not isinstance(archive, dict) or archive.get("$archiver") != "NSKeyedArchiver":
        return []
    objects = archive.get("$objects")
    if not isinstance(objects, list):
        return []

    contact = contact_from_filename(path)
    file_time = parse_time_from_filename(path)
    messages: list[ParsedMessage] = []
    for index, obj in enumerate(objects):
        if not isinstance(obj, dict) or plist_classname(objects, obj) != "InstantMessage":
            continue

        text = plist_string(objects, obj.get("MessageText"))
        if not text:
            original = plist_string(objects, obj.get("OriginalMessage"))
            text = html_to_text(original) if original else ""
        if not text:
            continue

        sender = plist_presentity_id(objects, obj.get("Sender"))
        service = "AIM/iChat"
        sender_obj = plist_obj(objects, obj.get("Sender"))
        if isinstance(sender_obj, dict):
            service = plist_string(objects, sender_obj.get("ServiceName")) or service

        sent_at = plist_date(objects, obj.get("Time")) or file_time
        guid = plist_string(objects, obj.get("GUID"))
        messages.append(
            ParsedMessage(
                source="ichat",
                service=service,
                account="",
                contact=contact,
                sender=sender,
                sent_at=sent_at,
                kind="message",
                text=text,
                path=str(path),
                raw={
                    "parser": "nskeyedarchiver",
                    "object_index": index,
                    "guid": guid,
                    "flags": obj.get("Flags"),
                },
            )
        )
    return messages


def iter_files(paths: list[Path]) -> Iterable[Path]:
    for root in paths:
        if not root.exists():
            continue
        if root.is_file():
            yield root
            continue
        for pattern in ("*.chatlog", "*.AdiumHTMLLog", "*.chat", "*.ichat"):
            yield from root.rglob(pattern)


def parse_file(path: Path) -> list[ParsedMessage]:
    suffix = path.suffix.lower()
    if path.is_dir() and suffix == ".chatlog":
        messages: list[ParsedMessage] = []
        for child in sorted(path.rglob("*.xml")):
            messages.extend(parse_adium_xml(child))
        for child in sorted(path.rglob("*.html")):
            messages.extend(parse_adium_html(child))
        return messages
    if suffix == ".chatlog":
        return parse_adium_xml(path)
    if suffix == ".adiumhtmllog":
        return parse_adium_html(path)
    if suffix == ".ichat":
        return parse_ichat_keyed_archive(path) or parse_ichat_blob(path)
    if suffix == ".chat":
        return parse_ichat_blob(path)
    return []


def conversation_id_for(msg: ParsedMessage) -> str:
    return stable_id(msg.source, msg.service, msg.account, msg.contact, msg.path)


def upsert_messages(con: sqlite3.Connection, messages: list[ParsedMessage]) -> None:
    if not messages:
        return
    first = messages[0]
    conv_id = conversation_id_for(first)
    con.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    title = " / ".join(x for x in [first.source, first.service, first.account, first.contact] if x)
    for index, msg in enumerate(messages):
        msg_id = stable_id(conv_id, index, msg.sent_at, msg.sender, msg.text)
        con.execute(
            """
            INSERT OR REPLACE INTO messages(
                id, conversation_id, source, service, account, contact,
                sender, sent_at, kind, text, path, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                conv_id,
                msg.source,
                msg.service,
                msg.account,
                msg.contact,
                msg.sender,
                msg.sent_at,
                msg.kind,
                msg.text,
                msg.path,
                json.dumps(msg.raw, ensure_ascii=False, sort_keys=True),
            ),
        )
    rows = con.execute(
        "SELECT COUNT(*), MIN(sent_at), MAX(sent_at) FROM messages WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()
    con.execute(
        """
        INSERT OR REPLACE INTO conversations(
            id, source, service, account, contact, title, path,
            first_message_at, last_message_at, message_count, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conv_id,
            first.source,
            first.service,
            first.account,
            first.contact,
            title,
            first.path,
            rows[1],
            rows[2],
            rows[0],
            json.dumps({"path": first.path}, ensure_ascii=False, sort_keys=True),
        ),
    )


def command_import(ns: argparse.Namespace) -> int:
    paths = [Path(p) for p in ns.paths]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit("Archive paths do not exist: " + ", ".join(missing))
    if ns.commit_every < 1:
        raise SystemExit("--commit-every must be at least 1")
    con = connect(Path(ns.db))
    stats = {"files_seen": 0, "files_imported": 0, "messages_imported": 0, "errors": []}
    if not ns.append:
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM conversations")
        con.commit()
    con.execute("BEGIN")
    for path in iter_files(paths):
        stats["files_seen"] += 1
        try:
            messages = parse_file(path)
            if messages:
                upsert_messages(con, messages)
                stats["files_imported"] += 1
                stats["messages_imported"] += len(messages)
        except Exception as exc:
            stats["errors"].append({"path": str(path), "error": str(exc)})
            if not ns.keep_going:
                con.rollback()
                raise
        if stats["files_seen"] % ns.commit_every == 0:
            con.commit()
            con.execute("BEGIN")
            print(
                f"imported {stats['files_imported']} / seen {stats['files_seen']} files, "
                f"{stats['messages_imported']} messages",
                file=sys.stderr,
                flush=True,
            )
    con.commit()
    summary = dict(stats)
    errors = summary.pop("errors")
    summary["error_count"] = len(errors)
    if errors:
        error_log = Path(ns.db).with_name("ancient-chat-import-errors.json")
        error_log.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["error_log"] = str(error_log)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def rows_to_json(rows: list[sqlite3.Row]) -> str:
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)


def add_filters(ns: argparse.Namespace, where: list[str], params: list[Any], prefix: str = "m") -> None:
    if ns.source:
        where.append(f"{prefix}.source = ?")
        params.append(ns.source)
    if ns.contact:
        where.append(f"{prefix}.contact LIKE ?")
        params.append(f"%{ns.contact}%")
    if ns.sender:
        where.append(f"{prefix}.sender LIKE ?")
        params.append(f"%{ns.sender}%")
    if ns.service:
        where.append(f"{prefix}.service LIKE ?")
        params.append(f"%{ns.service}%")


def command_search(ns: argparse.Namespace) -> int:
    con = connect(Path(ns.db))
    like = f"%{ns.query}%"
    params: list[Any] = [like, like, like, like, like]
    where = [
        """
        (
            m.text LIKE ?
            OR m.sender LIKE ?
            OR m.contact LIKE ?
            OR c.title LIKE ?
            OR m.service LIKE ?
        )
        """
    ]
    add_filters(ns, where, params)
    params.append(ns.limit)
    rows = con.execute(
        f"""
        SELECT
            m.sent_at,
            c.title AS conversation,
            m.source,
            m.service,
            m.account,
            m.contact,
            m.sender,
            m.kind,
            m.text,
            m.path,
            substr(m.text, 1, 260) AS snippet
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(m.sent_at, '') DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    print(rows_to_json(rows))
    return 0


def command_messages(ns: argparse.Namespace) -> int:
    con = connect(Path(ns.db))
    params: list[Any] = []
    where: list[str] = []
    add_filters(ns, where, params)
    if ns.conversation:
        where.append("(m.conversation_id = ? OR c.title LIKE ?)")
        params.extend([ns.conversation, f"%{ns.conversation}%"])
    params.append(ns.limit)
    sql = """
        SELECT
            m.sent_at,
            c.title AS conversation,
            m.source,
            m.service,
            m.account,
            m.contact,
            m.sender,
            m.kind,
            m.text,
            m.path
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(m.sent_at, '') DESC LIMIT ?"
    print(rows_to_json(con.execute(sql, params).fetchall()))
    return 0


def command_timeline(ns: argparse.Namespace) -> int:
    """Return a bounded chronological slice across one or more contact aliases."""
    con = connect(Path(ns.db))
    params: list[Any] = []
    where: list[str] = []

    alias_clauses: list[str] = []
    for alias in ns.alias:
        value = str(alias).strip()
        if not value:
            continue
        like = f"%{value}%"
        alias_clauses.extend(["m.contact LIKE ?", "m.sender LIKE ?", "c.title LIKE ?"])
        params.extend([like, like, like])
    if not alias_clauses:
        raise SystemExit("timeline requires at least one non-empty --alias")
    where.append("(" + " OR ".join(alias_clauses) + ")")

    if ns.query:
        query_clauses: list[str] = []
        for query in ns.query:
            value = str(query).strip()
            if not value:
                continue
            like = f"%{value}%"
            query_clauses.extend(["m.text LIKE ?", "m.sender LIKE ?", "m.contact LIKE ?"])
            params.extend([like, like, like])
        if query_clauses:
            where.append("(" + " OR ".join(query_clauses) + ")")

    if ns.source:
        where.append("m.source = ?")
        params.append(ns.source)
    if ns.service:
        where.append("m.service LIKE ?")
        params.append(f"%{ns.service}%")
    if ns.after:
        where.append("COALESCE(m.sent_at, '') >= ?")
        params.append(ns.after)
    if ns.before:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ns.before):
            exclusive_end = (datetime.fromisoformat(ns.before) + timedelta(days=1)).date().isoformat()
            where.append("COALESCE(m.sent_at, '') < ?")
            params.append(exclusive_end)
        else:
            where.append("COALESCE(m.sent_at, '') <= ?")
            params.append(ns.before)

    order = "ASC" if ns.order == "asc" else "DESC"
    params.append(ns.limit)
    rows = con.execute(
        f"""
        SELECT
            m.id,
            m.sent_at,
            c.title AS conversation,
            m.source,
            m.service,
            m.account,
            m.contact,
            m.sender,
            m.kind,
            m.text,
            m.path
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(m.sent_at, '') {order}, m.id {order}
        LIMIT ?
        """,
        params,
    ).fetchall()
    print(rows_to_json(rows))
    return 0


def command_conversations(ns: argparse.Namespace) -> int:
    con = connect(Path(ns.db))
    params: list[Any] = []
    where: list[str] = []
    if ns.source:
        where.append("source = ?")
        params.append(ns.source)
    if ns.contact:
        where.append("contact LIKE ?")
        params.append(f"%{ns.contact}%")
    sql = """
        SELECT id, source, service, account, contact, title, first_message_at,
               last_message_at, message_count, path
        FROM conversations
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(last_message_at, '') DESC LIMIT ?"
    params.append(ns.limit)
    print(rows_to_json(con.execute(sql, params).fetchall()))
    return 0


def command_status(ns: argparse.Namespace) -> int:
    con = connect(Path(ns.db))
    by_source = [
        dict(row)
        for row in con.execute(
            "SELECT source, COUNT(*) AS messages FROM messages GROUP BY source ORDER BY messages DESC"
        )
    ]
    result = {
        "database": str(Path(ns.db)),
        "exists": Path(ns.db).exists(),
        "bytes": Path(ns.db).stat().st_size if Path(ns.db).exists() else 0,
        "conversations": con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "by_source": by_source,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def bounded_timeline_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= limit <= 2000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 2000")
    return limit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB), help="SQLite index path (default: data/ancient-chat.db in the current directory).")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="Import archive files.")
    imp.add_argument("paths", nargs="+", help="Archive roots or files (required).")
    imp.add_argument("--append", action="store_true", help="Do not clear existing rows before import.")
    imp.add_argument("--keep-going", action=argparse.BooleanOptionalAction, default=True,
                     help="Continue after per-file parser errors (default: enabled).")
    imp.add_argument("--commit-every", type=int, default=100)
    imp.set_defaults(func=command_import)

    search = sub.add_parser("search", help="Search message text.")
    search.add_argument("query")
    search.add_argument("--source", choices=["adium", "ichat"])
    search.add_argument("--service")
    search.add_argument("--contact")
    search.add_argument("--sender")
    search.add_argument("--limit", type=int, default=25)
    search.set_defaults(func=command_search)

    messages = sub.add_parser("messages", help="List recent or filtered messages.")
    messages.add_argument("--source", choices=["adium", "ichat"])
    messages.add_argument("--service")
    messages.add_argument("--contact")
    messages.add_argument("--sender")
    messages.add_argument("--conversation")
    messages.add_argument("--limit", type=int, default=25)
    messages.set_defaults(func=command_messages)

    timeline = sub.add_parser(
        "timeline",
        help="List a bounded chronological message slice across one or more aliases.",
    )
    timeline.add_argument(
        "--alias",
        action="append",
        required=True,
        help="Contact/sender/conversation alias; repeat for related handles.",
    )
    timeline.add_argument(
        "--query",
        action="append",
        help="Optional text/sender/contact filter; repeat to match any query.",
    )
    timeline.add_argument("--source", choices=["adium", "ichat"])
    timeline.add_argument("--service")
    timeline.add_argument("--after", help="Inclusive ISO timestamp/date lower bound.")
    timeline.add_argument("--before", help="Inclusive ISO timestamp/date upper bound.")
    timeline.add_argument("--order", choices=["asc", "desc"], default="asc")
    timeline.add_argument("--limit", type=bounded_timeline_limit, default=250)
    timeline.set_defaults(func=command_timeline)

    conv = sub.add_parser("conversations", help="List conversations.")
    conv.add_argument("--source", choices=["adium", "ichat"])
    conv.add_argument("--contact")
    conv.add_argument("--limit", type=int, default=50)
    conv.set_defaults(func=command_conversations)

    status = sub.add_parser("status")
    status.set_defaults(func=command_status)
    return parser


def main() -> int:
    ns = build_parser().parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
