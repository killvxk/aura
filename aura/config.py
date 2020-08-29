# coding=utf-8
import os
import sys
import typing
import time
import resource
import logging
import warnings
import concurrent.futures
from importlib import resources
from pathlib import Path
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from typing import Optional, Generator

import tqdm
import pkg_resources
from ruamel.yaml import YAML


try:
    import simplejson as json
except ImportError:
    import json


CFG: Optional[dict] = None
CFG_PATH = None
SEMANTIC_RULES: Optional[dict] = None
LOG_FMT = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
LOG_ERR = None
# This is used to trigger breakpoint during AST traversing of specific lines
DEBUG_LINES = set()
DEFAULT_AST_STAGES = ("convert", "rewrite", "ast_pattern_matching", "taint_analysis", "readonly")
AST_PATTERNS_CACHE = None


if "AURA_DEBUG_LINES" in os.environ:
    DEBUG_LINES = set(int(x.strip()) for x in os.environ["AURA_DEBUG_LINES"].split(","))


# Check if the log file can be created otherwise it will crash here
if os.access("aura_errors.log", os.W_OK):
    LOG_ERR = RotatingFileHandler("aura_errors.log", maxBytes=1024 ** 2, backupCount=5)
    LOG_ERR.setLevel(logging.ERROR)


logger = logging.getLogger("aura")


if os.environ.get("AURA_DEBUG_LEAKS"):
    import gc

    gc.set_debug(gc.DEBUG_LEAK)


class TqdmLoggingHandler(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.tqdm.write(msg, file=sys.stderr)
            self.flush()
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError(record)


def configure_logger(level):
    logging.captureWarnings(capture=True)
    logger.setLevel(level)
    log_stream = TqdmLoggingHandler(level=level)
    log_stream.setFormatter(LOG_FMT)
    logger.addHandler(log_stream)
    if LOG_ERR is not None:
        logger.addHandler(LOG_ERR)


# Helper for loading API tokens for external integrations
def get_token(name: str) -> str:
    value = CFG.get("api_tokens", name, fallback=None)
    # If the token is not specified in the config, fall back to the env variable
    if value is None:
        value = os.environ.get(f"AURA_{name.upper()}_TOKEN", None)

    return value


def get_logger(name: str) -> logging.Logger:
    _log = logging.getLogger(name)
    if LOG_ERR is not None:
        _log.addHandler(LOG_ERR)
    return _log


def get_settings(pth: str, fallback=None) -> Optional[str]:
    data = CFG

    for part in pth.split("."):
        if part in data:
            data = data[part]
        else:
            return fallback

    return data


@lru_cache()
def get_score_or_default(score_type: str, fallback: int) -> int:
    """
    Retrieve score as defined in the config or fallback to the default provided value
    The scoring values are cached using lru_cache to avoid unnecessary lookups

    :param score_type: name of the scoring parameter as defined in the [score] aura config section
    :param fallback: fallback default value
    :return: Score integer
    """
    return CFG["score"].get(score_type, fallback)


def find_configuration() -> Path:  # TODO: add tests
    pth = Path(os.environ.get("AURA_CFG", "aura_config.yaml"))
    if pth.is_absolute():
        return pth

    cwd = Path.cwd()
    root = (cwd.root, cwd.drive, "/")
    while str(cwd) not in root:
        if (cwd / pth).exists():
            return cwd/pth
        else:
            cwd = cwd.parent

    return Path("aura.data.aura_config.yaml")


def get_file_location(location: str, base_path: Optional[str]=None) -> str:
    if location.startswith("aura.data."):  # Load file as a resource from aura package
        return location

    if os.path.exists(location):
        return location

    if base_path is not None:
        pth = Path(base_path) / location
        if pth.is_file():
            return str(pth)

    # TODO: use custom exception here so we can log as fatal and sys.exit(1)
    raise ValueError(f"Can't find configuration file `{location}` using base path `{base_path}`")


def get_file_content(location: str, base_path: Optional[str]=None) -> str:
    pth = get_file_location(location, base_path)

    if pth.startswith("aura.data."):  # Load file as a resource from aura package
        filename = pth[len("aura.data."):]
        return resources.read_text("aura.data", filename)

    else:
        with open(location, "r") as fd:
            return fd.read()


def load_config():
    global SEMANTIC_RULES, CFG, CFG_PATH

    CFG_PATH = str(find_configuration())
    logger.debug(f"Aura configuration located at {CFG_PATH}")

    yaml = YAML(typ="safe")
    CFG = yaml.load(get_file_content(CFG_PATH))

    semantic_sig_pth = CFG["aura"]["semantic-rules"]

    SEMANTIC_RULES = yaml.load(get_file_content(semantic_sig_pth, CFG_PATH))

    if "AURA_LOG_LEVEL" in os.environ:
        log_level = logging.getLevelName(os.getenv("AURA_LOG_LEVEL").upper())
    else:
        log_level = logging.getLevelName(
            CFG["aura"].get("log-level", "warning").upper()
        )

    configure_logger(log_level)

    if not sys.warnoptions:
        w_filter = CFG["aura"].get("warnings", "default")
        warnings.simplefilter(w_filter)
        os.environ["PYTHONWARNINGS"] = w_filter

    rss = CFG["aura"].get("rlimit-memory")
    if rss:
        resource.setrlimit(resource.RLIMIT_RSS, (rss, rss))

    fsize = CFG["aura"].get("rlimit-fsize")
    if fsize:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

    rec_limit = os.environ.get("AURA_RECURSION_LIMIT") or CFG["aura"].get("python-recursion-limit")

    if rec_limit:
        sys.setrecursionlimit(int(rec_limit))


def get_pypi_stats_path() -> Path:
    pth = os.environ.get("AURA_PYPI_STATS", None) or CFG["aura"]["pypi_stats"]
    return Path(get_file_location(pth, CFG_PATH))


def iter_pypi_stats() -> Generator[dict, None, None]:
    pth = get_pypi_stats_path()
    with pth.open() as fd:
        for line in fd:
            yield json.loads(line)


def get_maximum_archive_size() ->typing.Optional[int] :
    """
    Get settings for a maximum archive file size that can be extracted
    If the limit is not specified, fallback to the rlimit-fsize (if configured)

    :return: File int size in bytes for configured limit; otherwise None
    """
    size = CFG["aura"].get("max-archive-size") or CFG["aura"].get("rlimit-fsize")
    return size


def get_default_tag_filters() -> typing.List[str]:
    tags = CFG.get("tags", [])
    return tags


def get_installed_stages() -> typing.Generator[str,None,None]:
    for x in pkg_resources.iter_entry_points("aura.ast_visitors"):
        yield x.name


def get_ast_stages() -> typing.Tuple[str,...]:
    cfg_value = CFG["aura"].get("ast-stages") or DEFAULT_AST_STAGES
    return [x for x in cfg_value if x]


def get_ast_patterns():
    global AST_PATTERNS_CACHE
    from .pattern_matching import ASTPattern

    if AST_PATTERNS_CACHE is None:
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor() as e:
            AST_PATTERNS_CACHE = tuple(e.map(ASTPattern, SEMANTIC_RULES.get("patterns", [])))
        elapsed = round(time.monotonic() - start, 5)
        logger.debug(f"AST Pattern compilation took {elapsed}s")
    return AST_PATTERNS_CACHE

load_config()
