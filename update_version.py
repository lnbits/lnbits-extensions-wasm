#!/usr/bin/env python3
import json
import stat
import sys
import urllib.request
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZipFile, ZipInfo

ORG = "lnbits"
MANIFEST_PATH = "extensions.json"
NATIVE_ARTIFACT_SUFFIXES = {
    ".dll",
    ".dylib",
    ".exe",
    ".py",
    ".pyc",
    ".pyd",
    ".pyo",
    ".so",
}
NATIVE_ARTIFACT_FILENAMES = {"__init__.py"}
NATIVE_ARTIFACT_DIRS = {"__pycache__"}


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python update_version.py <repo_name> <version>")
        sys.exit(1)

    repo_name = sys.argv[1]
    tag = sys.argv[2]
    version = tag[1:] if tag.startswith("v") else tag
    repo = f"https://github.com/{ORG}/{repo_name}"
    archive_url = f"{repo}/archive/refs/tags/{tag}.zip"

    with urllib.request.urlopen(archive_url, timeout=30) as response:
        archive_data = response.read()

    archive_hash = sha256(archive_data).hexdigest()
    with ZipFile(BytesIO(archive_data)) as archive:
        root, config_name = validate_wasm_archive_layout(archive)
        with archive.open(config_name) as config_file:
            config = json.load(config_file)
        validate_wasm_config(repo_name, tag, version, archive, root, config)

    update_manifest(repo_name, tag, version, repo, archive_url, archive_hash, config)


def update_manifest(
    repo_name: str,
    tag: str,
    version: str,
    repo: str,
    archive_url: str,
    archive_hash: str,
    config: dict,
) -> None:
    with open(MANIFEST_PATH, encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)

    manifest.setdefault("extensions", [])
    ext_id = config["id"]
    latest_extension = None
    latest_index = None
    for index, extension in enumerate(manifest["extensions"]):
        if extension["id"] == ext_id:
            latest_extension = extension
            latest_index = index

    new_ext = release_entry(
        repo_name,
        tag,
        version,
        repo,
        archive_url,
        archive_hash,
        config,
    )

    if latest_extension is None or latest_index is None:
        manifest["extensions"].append(new_ext)
    elif latest_extension.get("min_lnbits_version") != config.get(
        "min_lnbits_version"
    ):
        updated_extensions = manifest["extensions"][:]
        updated_extensions.insert(latest_index + 1, new_ext)
        updated_extensions[latest_index]["max_lnbits_version"] = config.get(
            "min_lnbits_version"
        )
        manifest["extensions"] = updated_extensions
    else:
        preserved_max_version = latest_extension.get("max_lnbits_version")
        latest_extension.clear()
        latest_extension.update(new_ext)
        if preserved_max_version:
            latest_extension["max_lnbits_version"] = preserved_max_version

    with open(MANIFEST_PATH, "w", encoding="utf-8") as manifest_file:
        manifest_file.write(json.dumps(manifest, indent=4))
        manifest_file.write("\n")


def release_entry(
    repo_name: str,
    tag: str,
    version: str,
    repo: str,
    archive_url: str,
    archive_hash: str,
    config: dict,
) -> dict:
    raw_url = f"https://raw.githubusercontent.com/{ORG}/{repo_name}/{tag}"
    icon = raw_asset_url(raw_url, repo_name, config.get("tile"))

    entry = {
        "id": config["id"],
        "repo": repo,
        "version": version,
        "archive": archive_url,
        "hash": archive_hash,
        "min_lnbits_version": config.get("min_lnbits_version"),
        "name": config.get("name"),
        "short_description": config.get("short_description"),
        "icon": icon,
        "details_link": f"{raw_url}/config.json",
        "html_url": f"{repo}/releases/tag/{tag}",
        "extension_type": "wasm",
    }
    optional_fields = [
        "max_lnbits_version",
        "warning",
        "info_notification",
        "critical_notification",
        "paid_features",
        "pay_link",
    ]
    for field in optional_fields:
        if config.get(field):
            entry[field] = config[field]
    return entry


def raw_asset_url(raw_url: str, repo_name: str, asset_path: str | None) -> str:
    if not asset_path:
        return f"{raw_url}/static/assets/icon.png"

    ext_assets_prefix = f"/ext-assets/{repo_name}/"
    if asset_path.startswith(ext_assets_prefix):
        return f"{raw_url}/static/{asset_path.removeprefix(ext_assets_prefix)}"

    if asset_path.startswith("/"):
        return f"{raw_url}{asset_path}"
    return f"{raw_url}/{asset_path}"


def validate_wasm_archive_layout(archive: ZipFile) -> tuple[str, str]:
    roots: set[str] = set()
    config_names: list[str] = []
    seen_names: set[str] = set()

    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if info.is_dir() and len(path.parts) == 1 and path.parts[0] not in {
            "",
            ".",
            "..",
        }:
            roots.add(path.parts[0])
            continue

        path = safe_archive_path(info.filename)
        normalized_name = path.as_posix()
        if normalized_name in seen_names:
            raise ValueError(f"Duplicate path in WASM archive: {path}")
        seen_names.add(normalized_name)
        reject_native_artifact(info, path)

        roots.add(path.parts[0])
        if len(path.parts) == 2 and path.name == "config.json":
            config_names.append(info.filename)
        elif path.name == "config.json":
            raise ValueError("WASM archive must contain one top-level config.json")

    if len(roots) != 1:
        raise ValueError("WASM archive must contain exactly one top-level directory")
    if len(config_names) != 1:
        raise ValueError("WASM archive must contain exactly one top-level config.json")
    return next(iter(roots)), config_names[0]


def validate_wasm_config(
    repo_name: str,
    tag: str,
    version: str,
    archive: ZipFile,
    root: str,
    config: dict,
) -> None:
    ext_id = config.get("id")
    if ext_id != repo_name:
        raise ValueError(f"config.json id '{ext_id}' does not match repo '{repo_name}'")
    if config.get("version") != version:
        raise ValueError(
            f"config.json version '{config.get('version')}' does not match tag '{tag}'"
        )
    if config.get("extension_type") != "wasm":
        raise ValueError("config.json must declare extension_type='wasm'")

    wasm = config.get("wasm")
    module = wasm.get("module") if isinstance(wasm, dict) else None
    if not isinstance(module, str) or not module:
        raise ValueError("config.json must declare wasm.module")

    module_path = safe_archive_path(f"{root}/{module}")
    if module_path.parts[0] != root:
        raise ValueError("wasm.module escapes archive root")
    module_name = module_path.as_posix()
    if module_name not in archive.namelist():
        raise ValueError(f"Missing wasm.module file: {module}")
    with archive.open(module_name) as module_file:
        if module_file.read(4) != b"\0asm":
            raise ValueError(f"Invalid WASM module file: {module}")


def safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or len(path.parts) < 2
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"Unsafe path in WASM archive: {name}")
    return path


def reject_native_artifact(info: ZipInfo, path: PurePosixPath) -> None:
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValueError(f"WASM archive contains symlink: {path}")

    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & NATIVE_ARTIFACT_DIRS:
        raise ValueError(f"WASM archive contains Python cache path: {path}")

    name = path.name.lower()
    if name in NATIVE_ARTIFACT_FILENAMES:
        raise ValueError(f"WASM archive contains native Python file: {path}")
    if path.suffix.lower() in NATIVE_ARTIFACT_SUFFIXES:
        raise ValueError(f"WASM archive contains native artifact: {path}")


if __name__ == "__main__":
    main()
