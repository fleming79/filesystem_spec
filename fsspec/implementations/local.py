import datetime
import io
import logging
import os
import os.path as osp
import posixpath
import re
import shutil
import stat
import tempfile

from fsspec import AbstractFileSystem
from fsspec.compression import compr
from fsspec.core import get_compression
from fsspec.utils import isfilelike, stringify_path

logger = logging.getLogger("fsspec.local")


class LocalFileSystem(AbstractFileSystem):
    """Interface to files on local storage

    Parameters
    ----------
    auto_mkdir: bool
        Whether, when opening a file, the directory containing it should
        be created (if it doesn't already exist). This is assumed by pyarrow
        code.
    """

    root_marker = "/"
    protocol = "file", "local"
    local_file = True

    def __init__(self, auto_mkdir=False, **kwargs):
        super().__init__(**kwargs)
        self.auto_mkdir = auto_mkdir

    @property
    def fsid(self):
        return "local"

    def mkdir(self, path, create_parents=True, **kwargs):
        path = self._strip_protocol(path)
        if self.exists(path):
            raise FileExistsError(path)
        if create_parents:
            self.makedirs(path, exist_ok=True)
        else:
            os.mkdir(path, **kwargs)

    def makedirs(self, path, exist_ok=False):
        path = self._strip_protocol(path)
        os.makedirs(path, exist_ok=exist_ok)

    def rmdir(self, path):
        path = self._strip_protocol(path)
        os.rmdir(path)

    def ls(self, path, detail=False, **kwargs):
        path = self._strip_protocol(path)
        if detail:
            with os.scandir(path) as it:
                return [self.info(f) for f in it]
        else:
            return [posixpath.join(path, f) for f in os.listdir(path)]

    def info(self, path, **kwargs):
        if isinstance(path, os.DirEntry):
            # scandir DirEntry
            out = path.stat(follow_symlinks=False)
            link = path.is_symlink()
            if path.is_dir(follow_symlinks=False):
                t = "directory"
            elif path.is_file(follow_symlinks=False):
                t = "file"
            else:
                t = "other"
            path = self._strip_protocol(path.path)
        else:
            # str or path-like
            path = self._strip_protocol(path)
            out = os.stat(path, follow_symlinks=False)
            link = stat.S_ISLNK(out.st_mode)
            if link:
                out = os.stat(path, follow_symlinks=True)
            if stat.S_ISDIR(out.st_mode):
                t = "directory"
            elif stat.S_ISREG(out.st_mode):
                t = "file"
            else:
                t = "other"
        result = {
            "name": path,
            "size": out.st_size,
            "type": t,
            "created": out.st_ctime,
            "islink": link,
        }
        for field in ["mode", "uid", "gid", "mtime", "ino", "nlink"]:
            result[field] = getattr(out, f"st_{field}")
        if result["islink"]:
            result["destination"] = os.readlink(path)
            try:
                out2 = os.stat(path, follow_symlinks=True)
                result["size"] = out2.st_size
            except OSError:
                result["size"] = 0
        return result

    def lexists(self, path, **kwargs):
        return osp.lexists(path)

    def cp_file(self, path1, path2, **kwargs):
        path1 = self._strip_protocol(path1, remove_trailing_slash=True)
        path2 = self._strip_protocol(path2, remove_trailing_slash=True)
        if self.auto_mkdir:
            self.makedirs(self._parent(path2), exist_ok=True)
        if self.isfile(path1):
            shutil.copyfile(path1, path2)
        elif self.isdir(path1):
            self.mkdirs(path2, exist_ok=True)
        else:
            raise FileNotFoundError(path1)

    def get_file(self, path1, path2, callback=None, **kwargs):
        if isfilelike(path2):
            with open(path1, "rb") as f:
                shutil.copyfileobj(f, path2)
        else:
            return self.cp_file(path1, path2, **kwargs)

    def put_file(self, path1, path2, callback=None, **kwargs):
        return self.cp_file(path1, path2, **kwargs)

    def mv_file(self, path1, path2, **kwargs):
        path1 = self._strip_protocol(path1, remove_trailing_slash=True)
        path2 = self._strip_protocol(path2, remove_trailing_slash=True)
        shutil.move(path1, path2)

    def link(self, src, dst, **kwargs):
        src = self._strip_protocol(src)
        dst = self._strip_protocol(dst)
        os.link(src, dst, **kwargs)

    def symlink(self, src, dst, **kwargs):
        src = self._strip_protocol(src)
        dst = self._strip_protocol(dst)
        os.symlink(src, dst, **kwargs)

    def islink(self, path) -> bool:
        return os.path.islink(self._strip_protocol(path))

    def rm_file(self, path):
        os.remove(self._strip_protocol(path))

    def rm(self, path, recursive=False, maxdepth=None):
        if not isinstance(path, list):
            path = [path]

        for p in path:
            p = self._strip_protocol(p, remove_trailing_slash=True)
            if self.isdir(p):
                if not recursive:
                    raise ValueError("Cannot delete directory, set recursive=True")
                if osp.abspath(p) == os.getcwd():
                    raise ValueError("Cannot delete current working directory")
                shutil.rmtree(p)
            else:
                os.remove(p)

    def unstrip_protocol(self, name):
        name = self._strip_protocol(name)  # normalise for local/win/...
        return f"file://{name}"

    def _open(self, path, mode="rb", block_size=None, **kwargs):
        path = self._strip_protocol(path)
        if path.endswith("/"):
            raise IsADirectoryError(path)
        if self.auto_mkdir and "w" in mode:
            self.makedirs(self._parent(path), exist_ok=True)
        return LocalFileOpener(path, mode, fs=self, **kwargs)

    def touch(self, path, truncate=True, **kwargs):
        path = self._strip_protocol(path)
        if self.auto_mkdir:
            self.makedirs(self._parent(path), exist_ok=True)
        if self.exists(path):
            os.utime(path, None)
        else:
            open(path, "a").close()
        if truncate:
            os.truncate(path, 0)

    def created(self, path):
        info = self.info(path=path)
        return datetime.datetime.fromtimestamp(
            info["created"], tz=datetime.timezone.utc
        )

    def modified(self, path):
        info = self.info(path=path)
        return datetime.datetime.fromtimestamp(info["mtime"], tz=datetime.timezone.utc)

    @classmethod
    def _parent(cls, path, sep=os.sep):
        path = cls._strip_protocol(path, sep=sep, remove_trailing_slash=True)
        path = path.rsplit("/", 1)[0]
        if sep == "/":
            # posix
            if not path:
                return "/"
        else:
            # NT
            if path[1:2] == ":" and len(path) == 2:
                return path + "/"  # Root such as c:/
            # More cases may be required here
        return make_path_posix(path)

    @classmethod
    def _strip_protocol(cls, path, sep=os.sep, remove_trailing_slash=False):
        path = stringify_path(path)
        if path.startswith("file://"):
            path = path[7:]
            if sep == "\\":
                path = path.lstrip("/")
        elif path.startswith("file:"):
            path = path[5:]
        elif path.startswith("local://"):
            path = path[8:]
        elif path.startswith("local:"):
            path = path[6:]
        return make_path_posix(path, sep, remove_trailing_slash)

    def _isfilestore(self):
        # Inheriting from DaskFileSystem makes this False (S3, etc. were)
        # the original motivation. But we are a posix-like file system.
        # See https://github.com/dask/dask/issues/5526
        return True

    def chmod(self, path, mode):
        path = stringify_path(path)
        return os.chmod(path, mode)


def make_path_posix(path, sep=os.sep, remove_trailing_slash=False):
    """Make path generic"""
    if not isinstance(path, str) and isinstance(path, (list, set, tuple)):
        return type(path)(make_path_posix(p, sep, remove_trailing_slash) for p in path)
    if path.startswith("~"):
        path = osp.expanduser(path)
    if sep == "/":
        if not path:
            return os.getcwd()
        # most common fast case for posix
        if path.startswith("/"):
            return path.rstrip("/") or "/" if remove_trailing_slash else path
        elif path.startswith("./"):
            path = path[2:]
            path = f"{os.getcwd()}/{path}"
            return path.rstrip("/") or "/" if remove_trailing_slash else path
    else:
        if not path:
            return make_path_posix(os.getcwd(), sep, remove_trailing_slash)
    if (
        (sep not in path and "/" not in path)
        or (sep == "/" and not path.startswith("/"))
        or (sep == "\\" and ":" not in path and not path.startswith("\\\\"))
    ):
        # relative path like "path" or "rel\\path" (win) or rel/path"
        if os.sep == "\\":
            # abspath made some more '\\' separators
            return make_path_posix(osp.abspath(path), sep, remove_trailing_slash)
        else:
            return f"{os.getcwd()}/{path}"
    if path[1:2] == ":":
        # windows full path like "C:\\local\\path"
        path = path.replace("\\", "/").replace("//", "/")
        if len(path) <= 3:
            # nt root (something like c:/)
            return path if len(path) == 3 else path + "/"
        return path.rstrip("/") if remove_trailing_slash else path
    if path.startswith("\\\\"):
        # special case for windows UNC/DFS-style paths, do nothing,
        # just flip the slashes around (case below does not work!)
        path = path.replace("\\", "/")
        return path.rstrip("/") if remove_trailing_slash else path

    if path.startswith("file://"):
        return make_path_posix(
            path.removeprefix("file://").lstrip("/"), sep, remove_trailing_slash
        )
    else:
        raise NotImplementedError(path)


def trailing_sep(path):
    """Return True if the path ends with a path separator.

    A forward slash is always considered a path separator, even on Operating
    Systems that normally use a backslash.
    """
    # TODO: if all incoming paths were posix-compliant then separator would
    # always be a forward slash, simplifying this function.
    # See https://github.com/fsspec/filesystem_spec/pull/1250
    return path.endswith(os.sep) or (os.altsep is not None and path.endswith(os.altsep))


class LocalFileOpener(io.IOBase):
    def __init__(
        self, path, mode, autocommit=True, fs=None, compression=None, **kwargs
    ):
        logger.debug("open file: %s", path)
        self.path = path
        self.mode = mode
        self.fs = fs
        self.f = None
        self.autocommit = autocommit
        self.compression = get_compression(path, compression)
        self.blocksize = io.DEFAULT_BUFFER_SIZE
        self._open()

    def _open(self):
        if self.f is None or self.f.closed:
            if self.autocommit or "w" not in self.mode:
                self.f = open(self.path, mode=self.mode)
                if self.compression:
                    compress = compr[self.compression]
                    self.f = compress(self.f, mode=self.mode)
            else:
                # TODO: check if path is writable?
                i, name = tempfile.mkstemp()
                os.close(i)  # we want normal open and normal buffered file
                self.temp = name
                self.f = open(name, mode=self.mode)
            if "w" not in self.mode:
                self.size = self.f.seek(0, 2)
                self.f.seek(0)
                self.f.size = self.size

    def _fetch_range(self, start, end):
        # probably only used by cached FS
        if "r" not in self.mode:
            raise ValueError
        self._open()
        self.f.seek(start)
        return self.f.read(end - start)

    def __setstate__(self, state):
        self.f = None
        loc = state.pop("loc", None)
        self.__dict__.update(state)
        if "r" in state["mode"]:
            self.f = None
            self._open()
            self.f.seek(loc)

    def __getstate__(self):
        d = self.__dict__.copy()
        d.pop("f")
        if "r" in self.mode:
            d["loc"] = self.f.tell()
        else:
            if not self.f.closed:
                raise ValueError("Cannot serialise open write-mode local file")
        return d

    def commit(self):
        if self.autocommit:
            raise RuntimeError("Can only commit if not already set to autocommit")
        shutil.move(self.temp, self.path)

    def discard(self):
        if self.autocommit:
            raise RuntimeError("Cannot discard if set to autocommit")
        os.remove(self.temp)

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return "r" not in self.mode

    def read(self, *args, **kwargs):
        return self.f.read(*args, **kwargs)

    def write(self, *args, **kwargs):
        return self.f.write(*args, **kwargs)

    def tell(self, *args, **kwargs):
        return self.f.tell(*args, **kwargs)

    def seek(self, *args, **kwargs):
        return self.f.seek(*args, **kwargs)

    def seekable(self, *args, **kwargs):
        return self.f.seekable(*args, **kwargs)

    def readline(self, *args, **kwargs):
        return self.f.readline(*args, **kwargs)

    def readlines(self, *args, **kwargs):
        return self.f.readlines(*args, **kwargs)

    def close(self):
        return self.f.close()

    def truncate(self, size=None) -> int:
        return self.f.truncate(size)

    @property
    def closed(self):
        return self.f.closed

    def fileno(self):
        return self.raw.fileno()

    def flush(self) -> None:
        self.f.flush()

    def __iter__(self):
        return self.f.__iter__()

    def __getattr__(self, item):
        return getattr(self.f, item)

    def __enter__(self):
        self._incontext = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._incontext = False
        self.f.__exit__(exc_type, exc_value, traceback)
