import os
import threading
import fnmatch
import logging
from pathlib import Path
from datetime import datetime, timezone

from whoosh import index
from whoosh.fields import Schema, ID, TEXT, KEYWORD, STORED, NUMERIC
from whoosh.qparser import MultifieldParser, QueryParser
from whoosh.writing import BufferedWriter

from config import Config, INDEX_DIR

log = logging.getLogger("ponder.indexer")

SCHEMA = Schema(
    path     = ID(stored=True, unique=True),
    filename = TEXT(stored=True),
    content  = TEXT(stored=False),        # full text — indexed, not stored (saves space)
    ext      = KEYWORD(stored=True),
    snippet  = STORED(),                  # first 300 chars for display
    size     = NUMERIC(stored=True),
    modified = STORED(),
)

MAX_SNIPPET  = 300
CODE_TYPES   = {"py","js","ts","cpp","c","h","cc","cs","go","rs","rb","java",
                "sh","bash","zsh","toml","json","yaml","yml","csv","xml",
                "html","css","ini","cfg","conf","log","txt","md","rst"}


# ── Text extraction ───────────────────────────────────────────────────────

def _extract_text(path: Path, max_mb: float) -> str | None:
    try:
        if path.stat().st_size > max_mb * 1024 * 1024:
            return None

        ext = path.suffix.lower().lstrip(".")

        if ext in CODE_TYPES:
            return path.read_text(encoding="utf-8", errors="ignore")

        if ext == "pdf":
            try:
                from pdfminer.high_level import extract_text
                return extract_text(str(path))
            except ImportError:
                pass                     # built into Merlin: pypdf instead
            except Exception:
                return None
            try:
                # pypdf is pure Python; pdfminer.six needs cryptography (Rust)
                from pypdf import PdfReader
                return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
            except Exception:
                return None

        if ext == "docx":
            try:
                from docx import Document
                return "\n".join(p.text for p in Document(str(path)).paragraphs)
            except Exception:
                return None

    except Exception:
        return None

    return None


def _should_exclude(path: Path, patterns: list) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, p) or name == p for p in patterns)


# ── File watcher ──────────────────────────────────────────────────────────

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class _WatchHandler(FileSystemEventHandler):
        def __init__(self, indexer):
            self._idx = indexer

        def on_modified(self, event):
            if not event.is_directory:
                self._idx._index_file(Path(event.src_path))

        def on_created(self, event):
            if not event.is_directory:
                self._idx._index_file(Path(event.src_path))

        def on_deleted(self, event):
            if not event.is_directory:
                self._idx._delete_file(Path(event.src_path))

        def on_moved(self, event):
            if not event.is_directory:
                self._idx._delete_file(Path(event.src_path))
                self._idx._index_file(Path(event.dest_path))

    WATCHDOG_OK = True

except Exception as e:
    log.warning(f"watchdog unavailable — live file watching disabled ({e})")
    WATCHDOG_OK = False


# ── Indexer ───────────────────────────────────────────────────────────────

class Indexer:
    def __init__(self, cfg: Config):
        self.cfg         = cfg
        self._observer   = None
        self._lock       = threading.Lock()
        self._doc_count  = 0
        self._last_built = "never"

        try:
            if index.exists_in(str(INDEX_DIR)):
                self._ix = index.open_dir(str(INDEX_DIR))
                with self._ix.searcher() as s:
                    self._doc_count = s.doc_count()
                log.info(f"Opened existing index ({self._doc_count} docs)")
            else:
                self._ix = index.create_in(str(INDEX_DIR), SCHEMA)
                log.info("Created new index")
        except Exception as e:
            log.error(f"Index init failed: {e} — recreating")
            self._ix = index.create_in(str(INDEX_DIR), SCHEMA)

    # ── Public ───────────────────────────────────────────────────────────

    def start(self):
        if self._doc_count == 0:
            log.info("Index empty — starting initial build in background")
            t = threading.Thread(target=self.build_index, daemon=True, name="ponder-indexer")
            t.start()
        self._start_watcher()

    def stop(self):
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass

    def build_index(self):
        log.info("Index build started")
        try:
            writer = self._ix.writer()
            total  = 0
            for d in self.cfg.watch_dirs:
                root = Path(os.path.expanduser(d))
                if not root.exists():
                    log.warning(f"Watch dir not found, skipping: {root}")
                    continue
                log.info(f"Scanning: {root}")
                count = 0
                # Use os.walk instead of rglob — handles PermissionError per-dir
                for dirpath, dirnames, filenames in os.walk(root):
                    # Skip excluded dirs in-place (prunes the walk)
                    dirnames[:] = [
                        dn for dn in dirnames
                        if not _should_exclude(Path(dirpath) / dn, self.cfg.exclude_patterns)
                        and not dn.startswith('.')
                    ]
                    for fname in filenames:
                        fp = Path(dirpath) / fname
                        if not _should_exclude(fp, self.cfg.exclude_patterns):
                            try:
                                self._add_to_writer(writer, fp)
                                count += 1
                            except Exception as e:
                                log.debug(f"Skipped {fp}: {e}")
                log.info(f"  {root.name}: {count} files indexed")
                total += count
            writer.commit()
            self._doc_count  = total
            self._last_built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            log.info(f"Index build complete — {total} total files")
        except Exception as e:
            log.error(f"Index build error: {e}", exc_info=True)

    def search(self, query: str, limit: int = 20) -> list:
        if not query.strip():
            return []
        try:
            with self._ix.searcher() as s:
                # Search across filename and full content
                parser = MultifieldParser(
                    ["filename", "content"],
                    schema=self._ix.schema,
                )
                q   = parser.parse(query)
                hits = s.search(q, limit=limit)
                return [
                    {
                        "title":    h["filename"],
                        "path":     h["path"],
                        "snippet":  h.get("snippet", ""),
                        "ext":      h.get("ext", ""),
                        "size":     _fmt_size(h.get("size") or 0),
                        "modified": h.get("modified", ""),
                    }
                    for h in hits
                ]
        except Exception as e:
            log.warning(f"Search error: {e}")
            return []

    def status(self) -> dict:
        try:
            with self._ix.searcher() as s:
                self._doc_count = s.doc_count()
        except Exception:
            pass
        return {
            "doc_count":    self._doc_count,
            "last_indexed": self._last_built,
            "watch_dirs":   self.cfg.watch_dirs,
        }

    # ── Private ──────────────────────────────────────────────────────────

    def _start_watcher(self):
        if not WATCHDOG_OK:
            return
        try:
            self._observer = Observer()
            handler        = _WatchHandler(self)
            watched        = 0
            for d in self.cfg.watch_dirs:
                path = os.path.expanduser(d)
                if os.path.exists(path):
                    self._observer.schedule(handler, path, recursive=True)
                    watched += 1
            if watched:
                self._observer.start()
                log.info(f"File watcher active on {watched} director(ies)")
            else:
                log.warning("No watch dirs found — file watcher not started")
        except Exception as e:
            # Non-fatal — search still works, just no live updates
            log.warning(f"File watcher failed to start: {e}")
            self._observer = None

    def _index_file(self, path: Path):
        if _should_exclude(path, self.cfg.exclude_patterns):
            return
        with self._lock:
            try:
                writer = self._ix.writer()
                self._add_to_writer(writer, path)
                writer.commit()
            except Exception as e:
                log.debug(f"Failed to index {path}: {e}")

    def _delete_file(self, path: Path):
        with self._lock:
            try:
                writer = self._ix.writer()
                writer.delete_by_term("path", str(path))
                writer.commit()
            except Exception as e:
                log.debug(f"Failed to delete {path} from index: {e}")

    def _add_to_writer(self, writer, path: Path):
        try:
            stat    = path.stat()
            text    = _extract_text(path, self.cfg.max_file_mb) or ""
            snippet = text[:MAX_SNIPPET].replace("\n", " ").strip()
            writer.update_document(
                path     = str(path),
                filename = path.name,
                content  = text,
                ext      = path.suffix.lower().lstrip("."),
                snippet  = snippet,
                size     = stat.st_size,
                modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
            )
        except Exception as e:
            log.debug(f"Skipped {path}: {e}")


def _fmt_size(b: int) -> str:
    if b < 1024:        return f"{b} B"
    if b < 1024 ** 2:   return f"{b/1024:.1f} KB"
    return f"{b/1024**2:.1f} MB"
