"""Build the offline installer bundle of the helios-tests web UI for a Linux server or a Mac.

    python tools/build_ui_installer.py                    # -> dist/helios-ui-<version>.tar.gz (Linux)
    python tools/build_ui_installer.py --target macos     # -> dist/helios-ui-<version>-macos.tar.gz
    python tools/build_ui_installer.py --target all --ref HEAD --out dist --wheelhouse-cache ~/.cache/helios-wheels

The bundle (one top directory helios-ui-<version>/):
    app/            the COMMITTED code of <ref> (git archive), without workbooks (testplan/*.xlsx), local
                    inventories (inventory/*.local.yaml), results/ and backups (*.bak*); plus BUILD_INFO.json
                    (version, commit, date) and MANIFEST.txt (the file list; the UI sends exactly these files to the
                    jump host, as git archive does in a checkout)
    wheelhouse/     every runtime dependency (pytest, paramiko, PyYAML, requests, websocket-client, openpyxl and
                    theirs) as wheels for the target: Linux x86_64 (manylinux), CPython 3.10 - 3.12; or macOS
                    (Apple silicon arm64 + Intel x86_64), CPython 3.10 - 3.14; PYTHONS lists them, TARGET names it
    requirements.txt, install.sh, uninstall.sh, README-INSTALL.txt (from tools/installer/ of <ref>), VERSION
    helios-debug/   the committed HEAD of the separate helios-debug repo (link check, debug guide), when found next to
                    this repo or given with --debug-repo (skip: --no-debug); installed next to app/, where the HC-012
                    health check finds it; its inventories (*.local.yaml) and results/ are left out

Version: ``git describe --tags`` when there is a tag, else <commit date YYYYmmdd>-<short sha>.
Needs internet (PyPI) for the wheel download, unless --wheelhouse DIR gives a ready one.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHONS = ("3.10", "3.11", "3.12")
# manylinux2014 == manylinux_2_17 (CentOS 7 / glibc 2.17+); 2_28 for packages that dropped 2014 (e.g. cryptography)
PLATFORMS = ("manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux_2_28_x86_64")
MACOS_MAX = (14, 0)                      # newest macOS tag downloaded for (wheels for older macOS run on newer)
# per target: Python versions, file-name suffix and the CPU architectures (one pip download each)
TARGETS = {
    "linux": {"label": "Linux x86_64", "pythons": PYTHONS, "suffix": "", "archs": ("x86_64",)},
    "macos": {"label": "macOS arm64 + x86_64", "pythons": ("3.10", "3.11", "3.12", "3.13", "3.14"), "suffix": "-macos",
              "archs": ("arm64", "x86_64")},
}
EXTRA_DEPS = ("openpyxl>=3.1",)          # [tools]: workbook pick in the UI, select_cases.py
EXCLUDE = ("testplan/*.xlsx", "testplan/~$*", "inventory/*.local.yaml", "*.bak", "*.bak-*",
           "*.bak.*", "*/~$*", "~$*")
INSTALLER = ("install.sh", "uninstall.sh", "README-INSTALL.txt")


def git(repo: Path, *args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=300)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout if binary else r.stdout.decode().strip()


def excluded(path: str) -> bool:
    """A repo-relative path that never goes into the bundle (secrets, local data, the big workbooks)."""
    name = path.rsplit("/", 1)[-1]
    if path == "results" or path.startswith("results/"):
        return True
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in EXCLUDE)


def version_of(repo: Path, ref: str = "HEAD") -> tuple[str, str, str]:
    """(version, full commit sha, commit date YYYY-mm-dd HH:MM:SS)."""
    sha = git(repo, "rev-parse", f"{ref}^{{commit}}")
    date = git(repo, "log", "-1", "--format=%cd", "--date=format:%Y%m%d", sha)
    when = git(repo, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M:%S", sha)
    r = subprocess.run(["git", "-C", str(repo), "describe", "--tags", sha], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip(), sha, when
    return f"{date}-{sha[:7]}", sha, when


def requirements(repo: Path, ref: str = "HEAD") -> list[str]:
    """Runtime dependencies of pyproject.toml at <ref> + openpyxl (the [tools] extra)."""
    import tomllib
    data = tomllib.loads(git(repo, "show", f"{ref}:pyproject.toml"))
    deps = list(data["project"]["dependencies"])
    for d in EXTRA_DEPS:
        if not any(x.split(">")[0].split("=")[0].strip().lower() == d.split(">")[0].lower() for x in deps):
            deps.append(d)
    return deps


def _platforms(target: str, arch: str) -> list[str]:
    """Wheel platform tags a machine of <target>/<arch> accepts, newest first."""
    if target == "macos":
        from packaging import tags
        return list(tags.mac_platforms(MACOS_MAX, arch))
    return ["manylinux2014_x86_64"] + [f"manylinux_2_{g}_x86_64" for g in range(28, 4, -1)] + \
        ["manylinux2010_x86_64", "manylinux1_x86_64"]


def pip_download_argv(req: Path, dest: Path, py: str, pip: list[str] | None = None, target: str = "linux",
                      arch: str = "x86_64") -> list[str]:
    a = list(pip or [sys.executable, "-m", "pip"]) + ["download", "--disable-pip-version-check", "--no-input",
                                                      "-r", str(req), "--dest", str(dest), "--only-binary=:all:",
                                                      "--implementation", "cp", "--python-version", py]
    for p in (PLATFORMS if target == "linux" else _platforms(target, arch)):
        a += ["--platform", p]
    return a


def target_env(py: str, target: str = "linux", arch: str = "x86_64") -> dict:
    """PEP 508 marker environment of CPython <py> on the target (pip evaluates markers for the HOST Python even
    with --python-version, so e.g. pytest's ``tomli; python_version < "3.11"`` would be missed from a newer Mac)."""
    mac = target == "macos"
    return {"python_version": py, "python_full_version": f"{py}.0", "sys_platform": "darwin" if mac else "linux",
            "os_name": "posix", "platform_system": "Darwin" if mac else "Linux", "platform_machine": arch,
            "platform_release": "", "platform_version": "", "implementation_name": "cpython",
            "implementation_version": f"{py}.0", "platform_python_implementation": "CPython", "extra": ""}


def _supported_tags(py: str, target: str = "linux", arch: str = "x86_64") -> set:
    from packaging import tags
    major, minor = (int(x) for x in py.split("."))
    plats = _platforms(target, arch)
    return set(tags.cpython_tags((major, minor), platforms=plats)) | \
        set(tags.compatible_tags((major, minor), f"cp{major}{minor}", platforms=plats))


def missing_for(reqs: list[str], wheelhouse: Path, py: str, target: str = "linux", arch: str = "x86_64") -> list[str]:
    """Requirements (with their dependencies, markers evaluated for <py> on the target) that no wheel in
    ``wheelhouse`` satisfies for CPython <py> on <target>/<arch>. [] = the wheelhouse installs offline there."""
    import zipfile

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name, parse_wheel_filename
    ok_tags, env = _supported_tags(py, target, arch), target_env(py, target, arch)
    have: dict[str, list] = {}
    for w in wheelhouse.glob("*.whl"):
        name, ver, _, wtags = parse_wheel_filename(w.name)
        if ok_tags & set(wtags):
            have.setdefault(name, []).append((ver, w))
    missing, todo, seen = [], [(Requirement(r), "") for r in reqs], set()
    while todo:
        req, extra = todo.pop()
        if req.marker and not req.marker.evaluate({**env, "extra": extra}):
            continue
        name = canonicalize_name(req.name)
        cands = sorted((c for c in have.get(name, []) if req.specifier.contains(c[0], prereleases=True)), reverse=True)
        if not cands:
            missing.append(str(Requirement(f"{req.name}{'[' + ','.join(sorted(req.extras)) + ']' if req.extras else ''}"
                                           f"{req.specifier}")))
            continue
        for ex in sorted(req.extras) or [""]:
            if (name, ex) in seen:
                continue
            seen.add((name, ex))
            with zipfile.ZipFile(cands[0][1]) as z:
                meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
                for ln in z.read(meta).decode("utf-8", "replace").splitlines():
                    if ln.startswith("Requires-Dist:"):
                        todo.append((Requirement(ln.split(":", 1)[1].strip()), ex))
    return sorted(set(missing))


def build_wheelhouse(req: Path, dest: Path, pythons=None, run=subprocess.run, log=print, target: str = "linux") -> list[str]:
    """Download the wheels for every Python version (and, for macOS, both CPU architectures) into one folder (an
    abi3 / pure wheel lands there once), then add what the markers of the TARGET Python need, until every version
    resolves offline from it. Returns the wheel file names."""
    t = TARGETS[target]
    pythons = pythons or t["pythons"]
    dest.mkdir(parents=True, exist_ok=True)
    reqs = [ln.strip() for ln in req.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    for py in pythons:
        for arch in t["archs"]:
            log(f"  wheels for CPython {py} ({'Linux' if target == 'linux' else 'macOS'} {arch})")
            r = run(pip_download_argv(req, dest, py, target=target, arch=arch), capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"pip download for {py} {arch} failed:\n{(r.stderr or r.stdout)[-2000:]}")
            for _ in range(6):
                miss = missing_for(reqs, dest, py, target, arch)
                if not miss:
                    break
                log(f"    + for {py} {arch}: {', '.join(miss)}")
                extra = req.with_name(f"extra-{py}-{arch}.txt")
                extra.write_text("".join(f"{m}\n" for m in miss))
                r = run(pip_download_argv(extra, dest, py, target=target, arch=arch), capture_output=True, text=True)
                if r.returncode != 0:
                    raise SystemExit(f"pip download for {py} {arch} failed:\n{(r.stderr or r.stdout)[-2000:]}")
            else:
                raise SystemExit(f"the wheelhouse does not resolve for {py} {arch}: "
                                 f"{', '.join(missing_for(reqs, dest, py, target, arch))}")
    (dest / "PYTHONS").write_text(" ".join(pythons) + "\n")
    (dest / "TARGET").write_text(target + "\n")
    return sorted(p.name for p in dest.glob("*.whl"))


def _info(name: str, data: bytes, mode: int, mtime: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = len(data), mode, mtime
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def debug_files(debug_repo: Path | None) -> tuple[list[tuple[str, bytes, int]], dict]:
    """Files of the helios-debug repo's committed HEAD (minus local inventories / results) and its build info."""
    if debug_repo is None or not (debug_repo / ".git").exists():
        return [], {}
    sha = git(debug_repo, "rev-parse", "HEAD^{commit}")
    raw = git(debug_repo, "archive", "--format=tar", sha, binary=True)
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        for m in t.getmembers():
            if m.isfile() and not excluded(m.name):
                out.append((m.name, t.extractfile(m).read(), 0o755 if m.mode & 0o111 else 0o644))
    return out, {"commit": sha, "date": git(debug_repo, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M:%S", sha)}


def build(repo: Path, out_dir: Path, ref: str = "HEAD", wheelhouse: Path | None = None,
          wheel_builder=build_wheelhouse, log=print, debug_repo: Path | None = None, target: str = "linux") -> Path:
    """Write <out_dir>/helios-ui-<version>[-macos].tar.gz and return its path. ``wheelhouse``: a ready folder (with
    PYTHONS) to put in as is; else ``wheel_builder(requirements.txt, dest)`` makes one."""
    ver, sha, when = version_of(repo, ref)
    tgt = TARGETS[target]
    top = f"helios-ui-{ver}{tgt['suffix']}"
    mtime = int(time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S")))
    raw = git(repo, "archive", "--format=tar", sha, binary=True)
    files: list[tuple[str, bytes, int]] = []           # (repo path, data, mode)
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        for m in t.getmembers():
            if not m.isfile() or excluded(m.name):
                continue
            files.append((m.name, t.extractfile(m).read(), 0o755 if m.mode & 0o111 else 0o644))
    names = {n for n, _, _ in files}
    for f in ("ui/__main__.py", *(f"tools/installer/{x}" for x in INSTALLER), "tools/installer/racks.yaml.template"):
        if f not in names:
            raise SystemExit(f"{ref} has no {f}: commit the UI and tools/installer/ first")
    reqs = requirements(repo, sha)
    dfiles, dinfo = debug_files(debug_repo)
    info = {"version": ver, "commit": sha, "commit_date": when, "built": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "ref": ref, "requirements": reqs, "pythons": list(tgt["pythons"]), "target": target}
    if dinfo:
        info["helios_debug"] = dinfo
    manifest = "# files of this bundle's code (git archive of the commit in BUILD_INFO.json, minus local files)\n" + \
        "".join(f"{n}\n" for n, _, _ in sorted(files))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{top}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        req = Path(tmp) / "requirements.txt"
        req.write_text("".join(f"{r}\n" for r in reqs))
        if wheelhouse is None:
            log(f"downloading wheels ({tgt['label']}; {', '.join(tgt['pythons'])}) for: {', '.join(reqs)}")
            wheelhouse = Path(tmp) / "wheelhouse"
            if wheel_builder is build_wheelhouse:
                wheel_builder(req, wheelhouse, target=target)
            else:
                wheel_builder(req, wheelhouse)
        if not (wheelhouse / "PYTHONS").is_file():
            raise SystemExit(f"{wheelhouse}: no PYTHONS file (not a wheelhouse made by this script)")
        part = out.with_suffix(".part")
        with tarfile.open(part, "w:gz", format=tarfile.PAX_FORMAT) as t:
            def add(name: str, data: bytes, mode: int = 0o644) -> None:
                t.addfile(_info(f"{top}/{name}", data, mode, mtime), io.BytesIO(data))
            for n, data, mode in sorted(files):
                add(f"app/{n}", data, mode)
                if n.startswith("tools/installer/") and n.rsplit("/", 1)[-1] in INSTALLER:
                    add(n.rsplit("/", 1)[-1], data, 0o755 if n.endswith(".sh") else 0o644)
            for n, data, mode in sorted(dfiles):
                add(f"helios-debug/{n}", data, mode)
            add("app/BUILD_INFO.json", (json.dumps(info, indent=1) + "\n").encode())
            add("app/MANIFEST.txt", manifest.encode())
            add("requirements.txt", req.read_bytes())
            add("VERSION", f"{ver}\n".encode())
            add("TARGET", f"{target}\n".encode())        # install.sh checks it against the machine
            for w in sorted(wheelhouse.iterdir()):
                if w.is_file() and (w.suffix == ".whl" or w.name == "PYTHONS"):
                    add(f"wheelhouse/{w.name}", w.read_bytes())
        part.replace(out)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="HEAD", help="commit to package (default HEAD; only committed code goes in)")
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="output folder (default dist/)")
    ap.add_argument("--wheelhouse", type=Path, help="use this ready wheelhouse (with PYTHONS) instead of downloading")
    ap.add_argument("--wheelhouse-cache", type=Path,
                    help="download the wheels into this folder (kept; pip skips what is there) and use it")
    ap.add_argument("--debug-repo", type=Path, default=ROOT.parent / "helios-debug",
                    help="the helios-debug repo to bundle (default ../helios-debug next to this repo)")
    ap.add_argument("--no-debug", action="store_true", help="leave helios-debug out (HC-012 then skips on the server)")
    ap.add_argument("--target", choices=("linux", "macos", "all"), default="linux",
                    help="linux (default: x86_64, CPython 3.10-3.12), macos (arm64 + x86_64, CPython 3.10-3.14) or all")
    a = ap.parse_args(argv)
    if shutil.which("git") is None:
        raise SystemExit("git is needed")
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    if dirty and a.ref == "HEAD":
        print("note: uncommitted changes are NOT in the bundle (it packs the committed HEAD)", file=sys.stderr)
    dbg = None if a.no_debug else a.debug_repo.expanduser()
    if dbg is not None and not (dbg / ".git").exists():
        print(f"note: no helios-debug repo at {dbg} - bundle without it (HC-012 skips on the server)", file=sys.stderr)
        dbg = None
    for target in (("linux", "macos") if a.target == "all" else (a.target,)):
        wh = a.wheelhouse
        if wh is None and a.wheelhouse_cache:
            cache = a.wheelhouse_cache.expanduser()
            if target != "linux":                 # one cache per target (its own PYTHONS / TARGET)
                cache = cache.with_name(f"{cache.name}-{target}")
            ver, sha, _ = version_of(ROOT, a.ref)
            with tempfile.TemporaryDirectory() as tmp:
                req = Path(tmp) / "requirements.txt"
                req.write_text("".join(f"{r}\n" for r in requirements(ROOT, sha)))
                build_wheelhouse(req, cache, target=target)
            wh = cache
        out = build(ROOT, a.out, a.ref, wh, debug_repo=dbg, target=target)
        with tarfile.open(out) as t:
            names = t.getnames()
        wheels = [n for n in names if n.endswith(".whl")]
        dbgn = [n for n in names if "/helios-debug/" in n]
        print(f"{out}  ({TARGETS[target]['label']}; {out.stat().st_size / 1e6:.1f} MB, {len(names)} files, "
              f"{len(wheels)} wheels, {len(dbgn)} helios-debug files)")
        if target == "linux":
            print(f"install: scp {out.name} <user>@<server>: && ssh <user>@<server> "
                  f"'tar -xzf {out.name} && {out.name[:-7]}/install.sh'")
        else:
            print(f"install on the Mac: tar -xzf {out.name} && {out.name[:-7]}/install.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
