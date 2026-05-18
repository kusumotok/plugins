#!/usr/bin/env python3
"""Build plugin JARs and publish Fiji/ImageJ update-site directories.

The generated db.xml.gz follows the ImageJ updater database shape for simple
current-version plugin files. It intentionally keeps every update site
self-contained: JARs are copied into each site directory rather than linked.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


DOCTYPE = """<!DOCTYPE pluginRecords [
<!ELEMENT pluginRecords ((update-site | disabled-update-site)*, plugin*)>
<!ELEMENT update-site EMPTY>
<!ELEMENT disabled-update-site EMPTY>
<!ELEMENT plugin (platform*, category*, version?, previous-version*)>
<!ELEMENT version (description?, dependency*, link*, author*)>
<!ELEMENT previous-version EMPTY>
<!ELEMENT description (#PCDATA)>
<!ELEMENT dependency EMPTY>
<!ELEMENT link (#PCDATA)>
<!ELEMENT author (#PCDATA)>
<!ELEMENT platform (#PCDATA)>
<!ELEMENT category (#PCDATA)>
<!ATTLIST update-site name CDATA #REQUIRED>
<!ATTLIST update-site url CDATA #REQUIRED>
<!ATTLIST update-site ssh-host CDATA #IMPLIED>
<!ATTLIST update-site upload-directory CDATA #IMPLIED>
<!ATTLIST update-site description CDATA #IMPLIED>
<!ATTLIST update-site maintainer CDATA #IMPLIED>
<!ATTLIST update-site timestamp CDATA #REQUIRED>
<!ATTLIST disabled-update-site name CDATA #REQUIRED>
<!ATTLIST disabled-update-site url CDATA #REQUIRED>
<!ATTLIST disabled-update-site ssh-host CDATA #IMPLIED>
<!ATTLIST disabled-update-site upload-directory CDATA #IMPLIED>
<!ATTLIST disabled-update-site description CDATA #IMPLIED>
<!ATTLIST disabled-update-site maintainer CDATA #IMPLIED>
<!ATTLIST disabled-update-site timestamp CDATA #REQUIRED>
<!ATTLIST plugin update-site CDATA #IMPLIED>
<!ATTLIST plugin filename CDATA #REQUIRED>
<!ATTLIST plugin executable CDATA #IMPLIED>
<!ATTLIST dependency filename CDATA #REQUIRED>
<!ATTLIST dependency timestamp CDATA #IMPLIED>
<!ATTLIST dependency overrides CDATA #IMPLIED>
<!ATTLIST version timestamp CDATA #REQUIRED>
<!ATTLIST version checksum CDATA #REQUIRED>
<!ATTLIST version filesize CDATA #REQUIRED>
<!ATTLIST previous-version filename CDATA #IMPLIED>
<!ATTLIST previous-version timestamp CDATA #REQUIRED>
<!ATTLIST previous-version checksum CDATA #REQUIRED>
]>
"""


def run(cmd: str | list[str], cwd: Path | None = None, printable: str | None = None) -> None:
    if isinstance(cmd, list):
        printable_cmd = printable or " ".join(cmd)
        shell = False
    else:
        printable_cmd = printable or cmd
        shell = True
    print(f"+ {printable_cmd}", flush=True)
    completed = subprocess.run(cmd, cwd=cwd, shell=shell)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {printable_cmd}")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config.get("plugins", []), list):
        raise ValueError("plugins must be a list")
    return config


def default_site_base_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}"


def clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def clone_url(repo: str) -> tuple[str, str]:
    token = os.environ.get("SOURCE_REPO_TOKEN", "")
    if token and repo.startswith("https://github.com/"):
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("::add-mask::" + token)
        return repo.replace("https://github.com/", f"https://x-access-token:{token}@github.com/", 1), repo
    return repo, repo


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def timestamp_for(path: Path) -> str:
    dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S")


def normalize_url(base: str, site_dir: str) -> str:
    if not base:
        return ""
    return f"{base.rstrip('/')}/{site_dir.strip('/')}/"


def should_exclude(path: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def selected_jars(repo_dir: Path, plugin: dict[str, Any]) -> list[Path]:
    raw_globs = plugin.get("jar_glob", "target/*.jar")
    globs = raw_globs if isinstance(raw_globs, list) else [raw_globs]
    excludes = plugin.get("exclude_jars", ["*-sources.jar", "*-javadoc.jar", "*-tests.jar"])
    jars: list[Path] = []
    for pattern in globs:
        jars.extend(repo_dir.glob(pattern))
    jars = sorted({p.resolve() for p in jars if p.is_file() and not should_exclude(p, excludes)})
    if not jars:
        raise FileNotFoundError(f"No JARs matched {globs} in {repo_dir}")
    return jars


def copy_plugin_artifacts(repo_dir: Path, plugin: dict[str, Any], dist: Path, all_dir: Path) -> None:
    site_dir = str(plugin["site_dir"]).strip("/")
    site_plugins = dist / site_dir / "plugins"
    all_plugins = all_dir / "plugins"
    copy_as = plugin.get("copy_as")
    jars = selected_jars(repo_dir, plugin)
    if copy_as and len(jars) != 1:
        raise ValueError(f"copy_as can only be used when one JAR is selected: {plugin['name']}")

    for jar in jars:
        filename = copy_as or jar.name
        targets = [site_plugins / filename, all_plugins / filename]
        for target in targets:
            if target.exists():
                raise FileExistsError(
                    f"{target} already exists. Use copy_as to avoid filename collisions."
                )
            copy_file(jar, target)

    for dependency in plugin.get("dependencies", []):
        dependency_path = all_dir / dependency
        if not dependency_path.exists():
            raise FileNotFoundError(
                f"{plugin['name']} dependency is missing from all/: {dependency}"
            )
        copy_file(dependency_path, dist / site_dir / dependency)


def build_plugins(config: dict[str, Any], work_dir: Path, dist: Path) -> None:
    all_site_dir = str(config.get("all_site_dir", "all")).strip("/")
    all_dir = dist / all_site_dir
    for plugin in config.get("plugins", []):
        for key in ("name", "repo", "ref", "site_dir", "build"):
            if key not in plugin:
                raise ValueError(f"Plugin entry missing required key: {key}")
        repo_dir = work_dir / str(plugin["site_dir"]).strip("/")
        authenticated_url, printable_url = clone_url(str(plugin["repo"]))
        run(
            ["git", "clone", "--depth", "1", "--branch", str(plugin["ref"]), authenticated_url, str(repo_dir)],
            printable=f"git clone --depth 1 --branch {plugin['ref']} {printable_url} {repo_dir}",
        )
        build_dir = repo_dir / str(plugin.get("build_dir", "."))
        build_steps = plugin["build"] if isinstance(plugin["build"], list) else [plugin["build"]]
        for step in build_steps:
            run(step, cwd=build_dir)
        copy_plugin_artifacts(repo_dir, plugin, dist, all_dir)


def write_db(site_dir: Path) -> None:
    files = sorted(p for p in site_dir.rglob("*") if p.is_file() and p.name != "db.xml.gz")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', DOCTYPE, "<pluginRecords>"]
    for path in files:
        rel = path.relative_to(site_dir).as_posix()
        checksum = sha1(path)
        timestamp = timestamp_for(path)
        filesize = path.stat().st_size
        lines.append(f'  <plugin filename="{escape(rel)}">')
        lines.append(
            f'    <version checksum="{checksum}" timestamp="{timestamp}" filesize="{filesize}"/>'
        )
        lines.append("  </plugin>")
    lines.append("</pluginRecords>")
    xml = "\n".join(lines) + "\n"
    with gzip.open(site_dir / "db.xml.gz", "wb") as f:
        f.write(xml.encode("utf-8"))


def site_dirs(dist: Path, all_site_dir: str) -> list[Path]:
    dirs = [p for p in dist.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: (p.name != all_site_dir, p.name))


def write_site_index(site_dir: Path, site_name: str, url: str) -> None:
    jars = sorted((site_dir / "plugins").glob("*.jar")) if (site_dir / "plugins").exists() else []
    jar_items = "\n".join(f"<li>{html.escape(j.name)}</li>" for j in jars) or "<li>No JARs published yet.</li>"
    update_url = html.escape(url or site_dir.name)
    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(site_name)}</title>
</head>
<body>
  <main>
    <h1>{html.escape(site_name)}</h1>
    <p>Fiji/ImageJ update site URL:</p>
    <pre>{update_url}</pre>
    <h2>Published JARs</h2>
    <ul>
      {jar_items}
    </ul>
  </main>
</body>
</html>
"""
    (site_dir / "index.html").write_text(content, encoding="utf-8")


def write_root_index(config: dict[str, Any], dist: Path, base_url: str) -> None:
    all_site_dir = str(config.get("all_site_dir", "all")).strip("/")
    dirs = site_dirs(dist, all_site_dir)
    items = []
    for path in dirs:
        url = normalize_url(base_url, path.name)
        label = "ALL" if path.name == all_site_dir else path.name
        items.append(f'<li><a href="{html.escape(path.name)}/">{html.escape(label)}</a> <code>{html.escape(url)}</code></li>')
    body = "\n".join(items) or "<li>No update sites published yet.</li>"
    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fiji/ImageJ Update Sites</title>
</head>
<body>
  <main>
    <h1>Fiji/ImageJ Update Sites</h1>
    <p>Add one of these URLs in Fiji via Help &gt; Update &gt; Manage update sites.</p>
    <ul>
      {body}
    </ul>
  </main>
</body>
</html>
"""
    (dist / "index.html").write_text(content, encoding="utf-8")


def generate_indexes_and_dbs(config: dict[str, Any], dist: Path) -> None:
    base_url = str(config.get("site_base_url") or default_site_base_url())
    all_site_dir = str(config.get("all_site_dir", "all")).strip("/")
    for path in site_dirs(dist, all_site_dir):
        name = "ALL" if path.name == all_site_dir else path.name
        write_db(path)
        write_site_index(path, name, normalize_url(base_url, path.name))
    write_root_index(config, dist, base_url)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="plugins.json", type=Path)
    parser.add_argument("--work-dir", default="_work", type=Path)
    parser.add_argument("--dist", default="public", type=Path)
    args = parser.parse_args()

    config = load_config(args.config)
    clean_dir(args.work_dir)
    clean_dir(args.dist)
    (args.dist / str(config.get("all_site_dir", "all")).strip("/")).mkdir(parents=True, exist_ok=True)
    build_plugins(config, args.work_dir, args.dist)
    generate_indexes_and_dbs(config, args.dist)
    return 0


if __name__ == "__main__":
    sys.exit(main())
