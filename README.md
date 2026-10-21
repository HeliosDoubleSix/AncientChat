# AncientChat

A small local Python CLI for importing and searching legacy Adium and iChat/AIM archives in SQLite. No external Python packages, network service, or account connection is required.

## Install

Use Python 3.10 or newer with SQLite support. Download this repository, or clone it:

```sh
git clone https://github.com/heliosdoublesix/AncientChat.git
cd AncientChat
python scripts/ancient_chat_archive.py --help
```

Use `python3` instead of `python` if that is how Python is installed on your system. Run commands from the repository root.

## Import and search

Choose your own archive file or directory. All paths below are generic placeholders.

```sh
python scripts/ancient_chat_archive.py --db data/archive.db import "path/to/archives"
python scripts/ancient_chat_archive.py --db data/archive.db status
python scripts/ancient_chat_archive.py --db data/archive.db search "keyword" --limit 25
python scripts/ancient_chat_archive.py --db data/archive.db search "keyword" --source adium --sender "demo-sender"
python scripts/ancient_chat_archive.py --db data/archive.db conversations --limit 25
python scripts/ancient_chat_archive.py --db data/archive.db messages --source ichat --limit 25
python scripts/ancient_chat_archive.py --db data/archive.db timeline --alias "demo-contact" --after 2005-01-01 --before 2005-12-31 --limit 100
```

Put `--db` before the subcommand. Its default is `data/ancient-chat.db`, relative to the current directory. Source paths are required; there are no built-in personal archive locations.

**Import replaces the selected database's indexed messages and conversations by default.** Use a dedicated index database. Add `--append` to preserve existing rows while importing; a matching conversation is replaced rather than duplicated. Imports commit in batches, so interruption or `--no-keep-going` can leave earlier batches committed.

Malformed files are skipped by default. Read `error_count` in the JSON summary; command success does not mean every file parsed. Details are written beside the database as `ancient-chat-import-errors.json`. Use `--no-keep-going` to stop at the first parser error. Missing input paths are rejected before opening the index.

Results are JSON. Search uses SQLite `LIKE` across text and selected metadata, so `%` and `_` act as wildcards. Filters for sender, contact, and service use substring matching. Timeline aliases match contact, sender, or conversation title and are capped at 2,000 rows.

## Supported formats and limitations

| Input | Behavior |
| --- | --- |
| Adium `.chatlog` XML files | Individual message, status, and event records with available metadata. |
| Adium `.chatlog` package directories | XML and HTML children; point import at the directory containing the packages. |
| Adium `.AdiumHTMLLog` | HTML messages; timestamps currently use the session filename or file modification time rather than precise per-message times. |
| iChat `.ichat` | Attempts `NSKeyedArchiver` plist `InstantMessage` records; falls back to printable strings if structured parsing yields no messages. |
| iChat `.chat` | Noisy printable-string fallback: one searchable blob per file, without reliable sender or per-message timestamps. |

Skype support is unfinished and has no importer. Attachments are not extracted. Archive variants and malformed files may lose formatting, metadata, or messages. Fallback string extraction favors printable ASCII and may miss non-ASCII text. Recursive discovery uses the listed filename casing on case-sensitive filesystems. Dates may come from filenames or filesystem timestamps; mixed timezone strings are sorted lexically, so timelines are approximate. Conversation identity includes the source path; moving an archive can create a new conversation on append.

This is a local archive utility, not a complete fidelity export or restoration tool. Keep original files backed up. Use trusted local inputs.

## Privacy and development

The generated database contains message text, identities, metadata, and source paths. Import-error reports and command output may also contain private information. Keep them private. The repository uses a strict file allowlist in `.gitignore`; review every future addition before publishing.

This release contains only the CLI, this README, ignore rules, and the MIT license, with fresh history. It contains no original conversations, index databases, attachments, import reports, or private configuration.

A release smoke check imported a wholly synthetic two-message Adium XML fixture into a temporary database and verified search, source/sender filters, and timeline output. No personal archive was used for that check.

## License and provenance

MIT; see [LICENSE](LICENSE). The CLI was extracted from the owner's original archive utility. Its source history attributes the original implementation and subsequent changes to the owner. Inspection found standard-library imports and no bundled third-party implementation or notices. Cleanup removes machine-specific defaults, requires explicit source paths, validates missing paths and batch size, and makes the existing stop-on-error option usable. Parser architecture is retained.
