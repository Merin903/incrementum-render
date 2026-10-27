"""Thin wrapper around the real `hdfs dfs` command line. Nothing here pretends HDFS exists if it does not."""
import shutil
import subprocess

from . import config


def _run(args, timeout=300):
    cmd = ["hdfs", "dfs"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, " ".join(cmd), str(e)
    out = "\n".join(l for l in (r.stdout + r.stderr).splitlines() if "NativeCodeLoader" not in l)
    return r.returncode == 0, " ".join(cmd), out.strip()


def is_available() -> bool:
    if not shutil.which("hdfs"):
        return False
    return _run(["-ls", "/"], timeout=30)[0]


def mkdir(path):
    return _run(["-mkdir", "-p", path])


def put(local, hdfs_dir):
    return _run(["-put", "-f", str(local), hdfs_dir])


def ls(hdfs_dir):
    return _run(["-ls", "-h", hdfs_dir])


def upload_cleaned():
    """Upload the cleaned files; returns a list of (ok, command, output)."""
    results = [mkdir(config.HDFS_CLEANED)]
    for name in ("cleaned_sales.csv", "cleaned_guest_sales.csv", "returns_cancellations.csv"):
        f = config.CLEANED_DIR / name
        if f.exists():
            results.append(put(f, config.HDFS_CLEANED + "/"))
    results.append(ls(config.HDFS_CLEANED))
    return results
