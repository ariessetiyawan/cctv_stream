# logger_config.py
import logging
import logging.handlers
import os
import sys
from datetime import datetime

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

def setup_logger(name="cctv_app", level=logging.INFO):
    """Setup logger dengan rotasi harian + console output."""

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Hindari duplikasi handler kalau dipanggil 2x
    if logger.handlers:
        return logger

    # Format log
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # === Handler 1: File harian (rotasi otomatis) ===
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(LOG_DIR, "app.log"),
        when="midnight",
        interval=1,
        backupCount=30,         # simpan 30 hari
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # === Handler 2: File error saja ===
    error_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(LOG_DIR, "error.log"),
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setFormatter(fmt)
    error_handler.setLevel(logging.ERROR)
    logger.addHandler(error_handler)

    # === Handler 3: Console ===
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    return logger


def setup_crash_handler(logger):
    """Catat semua exception yang tidak tertangani."""
    import traceback

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.critical(
            "UNCAUGHT EXCEPTION",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

        # Simpan traceback ke file terpisah untuk analisa
        crash_file = os.path.join(LOG_DIR, "crash.log")
        with open(crash_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*70}\n")
            f.write(f"CRASH at {datetime.now().isoformat()}\n")
            f.write(f"{'='*70}\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
            f.write("\n")

    sys.excepthook = handle_exception