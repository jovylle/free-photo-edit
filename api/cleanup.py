"""Seven-day terminal-job artifact retention and stuck-job recovery.

Files are deleted after seven days; the ledger itself is never pruned.
"""
import logging
import os
from pathlib import Path
import stat
import time

from .credits import CreditsStore

RETENTION_SECONDS = 7 * 24 * 60 * 60
SWEEP_INTERVAL_SECONDS = 60 * 60
STUCK_JOB_SECONDS = int(os.environ.get("STUCK_JOB_SECONDS", str(15 * 60)))
RECOVERY_INTERVAL_SECONDS = 60
STUCK_JOB_ERROR = "timed out"
ARTIFACTS = tuple(f"input_{i}{ext}" for i in range(5)
                  for ext in (".jpg", ".jpeg", ".png", ".webp")) + ("output.png",)
log = logging.getLogger(__name__)


def artifacts_expired(job: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (job["status"] in ("done", "failed")
            and job.get("terminal_at") is not None
            and job["terminal_at"] <= now - RETENTION_SECONDS)


def recover_stuck(store: CreditsStore, now: float | None = None) -> int:
    """Fail and refund running jobs older than STUCK_JOB_SECONDS.

    Returns the number recovered; one log line per job, none when idle.
    """
    now = time.time() if now is None else now
    cutoff = now - STUCK_JOB_SECONDS
    recovered = 0
    for job_id in store.stale_running_job_ids(cutoff):
        if store.refund_stale_job(job_id, cutoff, STUCK_JOB_ERROR):
            log.warning("recovered stuck job %s: failed 'timed out', credit refunded",
                        job_id)
            recovered += 1
    return recovered


def sweep(store: CreditsStore, storage: Path, now: float | None = None) -> None:
    now = time.time() if now is None else now
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root = os.open(storage, flags)
    except FileNotFoundError:
        return
    except OSError:
        log.warning("TTL storage root unavailable; skipping", exc_info=True)
        return
    try:
        for job_id in store.expired_job_ids(now - RETENTION_SECONDS):
            if not job_id or job_id in (".", "..") or Path(job_id).name != job_id:
                continue
            try:
                # Descriptor-relative operations cannot follow swapped job symlinks.
                directory = os.open(job_id, flags, dir_fd=root)
                try:
                    for name in ARTIFACTS:
                        try:
                            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                            if stat.S_ISREG(info.st_mode):
                                os.unlink(name, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            log.warning("TTL artifact deletion failed; retry next sweep", exc_info=True)
                finally:
                    os.close(directory)
                try:
                    os.rmdir(job_id, dir_fd=root)  # Only empty directories; unknown content stays.
                except OSError:
                    pass
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("TTL job directory unavailable; skipping", exc_info=True)
    finally:
        os.close(root)
