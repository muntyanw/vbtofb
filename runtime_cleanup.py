import shutil
from pathlib import Path


RUNTIME_ARTIFACT_DIRS = ("runtime_debug", "runtime_media_cache")
RUNTIME_ARTIFACT_GLOBS = ("debug_run*",)


def cleanup_runtime_artifacts(root=None):
    root_path = Path(root or Path.cwd()).resolve()
    candidates = []

    for name in RUNTIME_ARTIFACT_DIRS:
        candidates.append(root_path / name)
    for pattern in RUNTIME_ARTIFACT_GLOBS:
        candidates.extend(root_path.glob(pattern))

    removed_dirs = 0
    removed_files = 0
    removed_bytes = 0
    for candidate in dict.fromkeys(candidates):
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root_path)
        except (OSError, ValueError):
            continue

        if resolved == root_path or not candidate.exists() or candidate.is_symlink():
            continue

        if candidate.is_dir():
            files = [path for path in candidate.rglob("*") if path.is_file()]
            removed_files += len(files)
            removed_bytes += sum(_file_size(path) for path in files)
            shutil.rmtree(candidate)
            removed_dirs += 1

    return {
        "directories": removed_dirs,
        "files": removed_files,
        "bytes": removed_bytes,
    }


def _file_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0
