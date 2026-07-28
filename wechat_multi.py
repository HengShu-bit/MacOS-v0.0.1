#!/usr/bin/env python3
"""A conservative macOS WeChat multi-instance launcher.

The tool never modifies the original WeChat.app.  It creates independently
identified, ad-hoc signed copies in the current user's Applications directory.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import plistlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


TOOL_VERSION = "1.0.0"
OFFICIAL_BUNDLE_IDS = {"com.tencent.xinWeChat", "com.tencent.xin"}
TENCENT_TEAM_ID = "5A4RE8SF68"
CLONE_MARKER = "CodexWeChatClone"
CLONE_INDEX_KEY = "CodexWeChatCloneIndex"
CLONE_SOURCE_ID_KEY = "CodexWeChatCloneSourceBundleIdentifier"
CLONE_SOURCE_BUILD_KEY = "CodexWeChatCloneSourceBuild"
CLONE_BUNDLE_PREFIX = "com.tencent.xinWeChat.codex.clone"
DEFAULT_OUTPUT_DIR = Path.home() / "Applications" / "微信多开"
MAX_EXTRA_INSTANCES = 5
RISK_ACK_FILE = ".multi-open-risk-acknowledged"
RISK_ACK_VERSION = "2026-07"
RISK_NOTICE = (
    "微信个人账号使用规范把“微信多开插件、外挂、软件或系统”列为常见违规类型，"
    "使用重签名副本可能导致警告、功能限制或封号。"
)


class WeChatMultiError(RuntimeError):
    """Expected user-facing failure."""


@dataclass(frozen=True)
class AppInfo:
    root: Path
    executable: Path
    bundle_id: str
    version: str
    build: str
    display_name: str
    is_clone: bool
    clone_index: Optional[int]
    source_build: Optional[str]

    def to_json(self) -> Dict[str, Any]:
        data = asdict(self)
        data["root"] = str(self.root)
        data["executable"] = str(self.executable)
        return data


@dataclass(frozen=True)
class CloneSpec:
    index: int
    display_name: str
    bundle_id: str
    destination: Path

    def to_json(self) -> Dict[str, Any]:
        data = asdict(self)
        data["destination"] = str(self.destination)
        return data


@dataclass(frozen=True)
class CommandResult:
    argv: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise WeChatMultiError("缺少系统命令：{}".format(argv[0])) from exc
        except PermissionError as exc:
            raise WeChatMultiError(
                "没有权限执行系统命令：{}".format(argv[0])
            ) from exc
        except OSError as exc:
            raise WeChatMultiError(
                "无法执行系统命令 {}：{}".format(argv[0], exc)
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WeChatMultiError("命令执行超时：{}".format(" ".join(argv))) from exc

        result = CommandResult(
            tuple(argv),
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            if detail:
                detail = "\n{}".format(detail)
            raise WeChatMultiError(
                "命令失败（退出码 {}）：{}{}".format(
                    completed.returncode, " ".join(argv), detail
                )
            )
        return result


def _candidate_app_paths() -> List[Path]:
    return [
        Path("/Applications/微信.app"),
        Path("/Applications/WeChat.app"),
        Path.home() / "Applications" / "微信.app",
        Path.home() / "Applications" / "WeChat.app",
    ]


def _read_plist(path: Path) -> Tuple[Dict[str, Any], plistlib.PlistFormat]:
    try:
        raw = path.read_bytes()
        data = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise WeChatMultiError("无法读取 Info.plist：{}".format(path)) from exc
    if not isinstance(data, dict):
        raise WeChatMultiError("Info.plist 根节点不是字典：{}".format(path))
    fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
    return data, fmt


def _write_plist_atomic(
    path: Path, data: Dict[str, Any], fmt: plistlib.PlistFormat
) -> None:
    payload = plistlib.dumps(data, fmt=fmt, sort_keys=False)
    temp_path = path.with_name(".{}.tmp-{}".format(path.name, uuid.uuid4().hex))
    try:
        temp_path.write_bytes(payload)
        os.replace(str(temp_path), str(path))
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def read_app_info(app_path: Path, *, require_official: bool = False) -> AppInfo:
    root = app_path.expanduser().resolve()
    if not root.is_dir() or root.suffix.lower() != ".app":
        raise WeChatMultiError("不是有效的 macOS App：{}".format(root))

    plist_path = root / "Contents" / "Info.plist"
    plist, _ = _read_plist(plist_path)
    bundle_id = str(plist.get("CFBundleIdentifier") or "").strip()
    executable_name = str(plist.get("CFBundleExecutable") or "").strip()
    if not bundle_id or not executable_name:
        raise WeChatMultiError("应用缺少 Bundle ID 或可执行文件信息：{}".format(root))

    executable = root / "Contents" / "MacOS" / executable_name
    if not executable.is_file() or not os.access(str(executable), os.X_OK):
        raise WeChatMultiError("找不到可执行文件：{}".format(executable))

    is_clone = plist.get(CLONE_MARKER) is True
    if require_official and bundle_id not in OFFICIAL_BUNDLE_IDS:
        raise WeChatMultiError(
            "源应用不是官方微信（Bundle ID: {}）：{}".format(bundle_id, root)
        )
    if not require_official and bundle_id not in OFFICIAL_BUNDLE_IDS and not is_clone:
        raise WeChatMultiError(
            "应用既不是官方微信，也不是本工具创建的副本：{}".format(root)
        )

    clone_index_raw = plist.get(CLONE_INDEX_KEY)
    clone_index = clone_index_raw if isinstance(clone_index_raw, int) else None
    source_build_raw = plist.get(CLONE_SOURCE_BUILD_KEY)
    source_build = str(source_build_raw) if source_build_raw is not None else None

    return AppInfo(
        root=root,
        executable=executable,
        bundle_id=bundle_id,
        version=str(plist.get("CFBundleShortVersionString") or "未知"),
        build=str(plist.get("CFBundleVersion") or "未知"),
        display_name=str(
            plist.get("CFBundleDisplayName")
            or plist.get("CFBundleName")
            or root.stem
        ),
        is_clone=is_clone,
        clone_index=clone_index,
        source_build=source_build,
    )


def discover_wechat(explicit_path: Optional[str] = None) -> AppInfo:
    if explicit_path:
        return read_app_info(Path(explicit_path), require_official=True)

    found: List[AppInfo] = []
    errors: List[str] = []
    seen: set = set()
    for candidate in _candidate_app_paths():
        if not candidate.exists():
            continue
        try:
            info = read_app_info(candidate, require_official=True)
        except WeChatMultiError as exc:
            errors.append(str(exc))
            continue
        if info.root not in seen:
            found.append(info)
            seen.add(info.root)

    if not found:
        suffix = "\n" + "\n".join(errors) if errors else ""
        raise WeChatMultiError(
            "没有找到官方微信。可用 --app '/Applications/微信.app' 指定路径。{}".format(
                suffix
            )
        )
    if len(found) > 1:
        paths = "\n".join("  - {}".format(item.root) for item in found)
        raise WeChatMultiError(
            "发现多个微信，请用 --app 明确指定：\n{}".format(paths)
        )
    return found[0]


def make_clone_specs(output_dir: Path, extra: int) -> List[CloneSpec]:
    if extra < 1 or extra > MAX_EXTRA_INSTANCES:
        raise WeChatMultiError(
            "额外实例数量必须在 1 到 {} 之间。".format(MAX_EXTRA_INSTANCES)
        )
    root = output_dir.expanduser().resolve()
    return [
        CloneSpec(
            index=index,
            display_name="微信 {}".format(index),
            bundle_id="{}{}".format(CLONE_BUNDLE_PREFIX, index),
            destination=root / "微信 {}.app".format(index),
        )
        for index in range(2, extra + 2)
    ]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _edit_clone_plist(
    clone_root: Path,
    *,
    source: AppInfo,
    spec: CloneSpec,
    keep_url_schemes: bool,
) -> None:
    plist_path = clone_root / "Contents" / "Info.plist"
    plist, fmt = _read_plist(plist_path)
    plist["CFBundleIdentifier"] = spec.bundle_id
    plist["CFBundleName"] = spec.display_name
    plist["CFBundleDisplayName"] = spec.display_name
    plist["CFBundleGetInfoString"] = spec.display_name
    plist["LSMultipleInstancesProhibited"] = False
    plist[CLONE_MARKER] = True
    plist[CLONE_INDEX_KEY] = spec.index
    plist[CLONE_SOURCE_ID_KEY] = source.bundle_id
    plist[CLONE_SOURCE_BUILD_KEY] = source.build
    if not keep_url_schemes:
        plist.pop("CFBundleURLTypes", None)
    _write_plist_atomic(plist_path, plist, fmt)


def _backup_path(destination: Path) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = destination.with_name(
        "{}.backup-{}".format(destination.name, timestamp)
    )
    counter = 2
    while candidate.exists():
        candidate = destination.with_name(
            "{}.backup-{}-{}".format(
                destination.name, timestamp, counter
            )
        )
        counter += 1
    return candidate


def _copy_app(source: Path, destination: Path, runner: CommandRunner) -> None:
    # APFS clone-on-write is fast and initially consumes little extra disk space.
    cloned = runner.run(
        ["/bin/cp", "-cR", str(source), str(destination)], check=False
    )
    if cloned.returncode == 0:
        return
    if destination.exists():
        shutil.rmtree(str(destination))
    runner.run(
        ["/usr/bin/ditto", "--rsrc", "--extattr", str(source), str(destination)]
    )


def _resign_clone(clone_root: Path, runner: CommandRunner) -> None:
    runner.run(
        ["/usr/bin/codesign", "--remove-signature", str(clone_root)], check=False
    )
    runner.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            str(clone_root),
        ]
    )
    runner.run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(clone_root),
        ]
    )


def _codesign_details(app: Path, runner: CommandRunner) -> CommandResult:
    return runner.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
        check=False,
    )


def _verify_official_source(source: AppInfo, runner: CommandRunner) -> None:
    details = _codesign_details(source.root, runner)
    output = "\n".join(part for part in (details.stdout, details.stderr) if part)
    if (
        details.returncode != 0
        or "TeamIdentifier={}".format(TENCENT_TEAM_ID) not in output
        or "Identifier={}".format(source.bundle_id) not in output
    ):
        raise WeChatMultiError(
            "源微信没有通过腾讯签名身份检查，拒绝复制或启动：{}".format(
                source.root
            )
        )

    integrity = runner.run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(source.root),
        ],
        check=False,
    )
    integrity_detail = (integrity.stderr or integrity.stdout).strip()
    if (
        integrity.returncode != 0
        and "CSSMERR_TP_NOT_TRUSTED" not in integrity_detail
    ):
        raise WeChatMultiError(
            "源微信代码完整性校验失败，拒绝复制或启动：{}".format(source.root)
        )


def _verify_clone_signature(clone: AppInfo, runner: CommandRunner) -> None:
    result = runner.run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(clone.root),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise WeChatMultiError(
            "副本签名校验失败，请使用 --replace 重建：{}".format(clone.root)
        )


def _ensure_app_stopped(app: AppInfo, runner: CommandRunner) -> None:
    running = _app_processes(app.root, runner)
    if running is None:
        raise WeChatMultiError(
            "无法确认旧副本是否已完全退出；为保护聊天数据库，已停止重建：{}".format(
                app.root
            )
        )
    if running:
        raise WeChatMultiError(
            "旧副本仍在运行（PID {}）。请完全退出该副本后再使用 --replace。".format(
                ", ".join(map(str, running))
            )
        )


def _register_app(app: Path, runner: CommandRunner) -> None:
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if lsregister.is_file():
        runner.run([str(lsregister), "-f", str(app)], check=False)


def create_clone(
    source: AppInfo,
    spec: CloneSpec,
    *,
    runner: CommandRunner,
    replace: bool,
    reuse_existing: bool,
    keep_url_schemes: bool,
    dry_run: bool,
) -> Tuple[str, Optional[Path]]:
    output_dir = spec.destination.parent
    if _is_within(output_dir, source.root):
        raise WeChatMultiError("输出目录不能位于官方微信 App 内。")

    if spec.destination.exists():
        existing: Optional[AppInfo] = None
        try:
            existing = read_app_info(spec.destination)
        except WeChatMultiError:
            pass
        if (
            existing is None
            or not existing.is_clone
            or existing.bundle_id != spec.bundle_id
        ):
            raise WeChatMultiError(
                "目标路径已被其他应用占用，出于安全考虑不会替换：{}".format(
                    spec.destination
                )
            )
        if (
            reuse_existing
            and not replace
            and existing
            and existing.bundle_id == spec.bundle_id
        ):
            if existing.source_build != source.build:
                raise WeChatMultiError(
                    "官方微信已更新（副本来源 Build {}，当前 Build {}）。"
                    "请在 start 命令后添加 --replace 安全重建副本。".format(
                        existing.source_build or "未知",
                        source.build,
                    )
                )
            _verify_clone_signature(existing, runner)
            return "reused", None
        if replace and not dry_run:
            _ensure_app_stopped(existing, runner)
        if not replace:
            raise WeChatMultiError(
                "副本已存在：{}。如需同步新版微信，请加 --replace；旧副本会先备份。".format(
                    spec.destination
                )
            )

    if dry_run:
        return "planned", None

    output_dir.mkdir(parents=True, exist_ok=True)
    temp_root = output_dir / ".{}.codex-{}.app".format(
        spec.display_name, uuid.uuid4().hex
    )
    backup: Optional[Path] = None
    try:
        _copy_app(source.root, temp_root, runner)
        copied_source = read_app_info(temp_root, require_official=True)
        _verify_official_source(copied_source, runner)
        _edit_clone_plist(
            temp_root,
            source=source,
            spec=spec,
            keep_url_schemes=keep_url_schemes,
        )
        _resign_clone(temp_root, runner)
        temp_info = read_app_info(temp_root)
        if temp_info.bundle_id != spec.bundle_id:
            raise WeChatMultiError("副本 Bundle ID 验证失败：{}".format(temp_root))

        if spec.destination.exists():
            current = read_app_info(spec.destination)
            if (
                not current.is_clone
                or current.bundle_id != spec.bundle_id
                or current.clone_index != spec.index
            ):
                raise WeChatMultiError(
                    "重建期间目标 App 身份发生变化，已停止替换：{}".format(
                        spec.destination
                    )
                )
            _ensure_app_stopped(current, runner)
            backup = _backup_path(spec.destination)
            spec.destination.rename(backup)
        try:
            temp_root.rename(spec.destination)
        except Exception:
            if backup and backup.exists() and not spec.destination.exists():
                backup.rename(spec.destination)
            raise
        _register_app(spec.destination, runner)
        return "created", backup
    finally:
        if temp_root.exists():
            shutil.rmtree(str(temp_root), ignore_errors=True)


def list_clones(output_dir: Path) -> List[AppInfo]:
    root = output_dir.expanduser().resolve()
    if not root.is_dir():
        return []
    found: List[AppInfo] = []
    for candidate in sorted(root.glob("*.app")):
        if candidate.name.startswith(".") or ".backup-" in candidate.stem:
            continue
        try:
            info = read_app_info(candidate)
        except WeChatMultiError:
            continue
        if info.is_clone:
            found.append(info)
    return sorted(found, key=lambda item: item.clone_index or 0)


def _libproc_process_paths() -> Optional[List[Tuple[int, Path]]]:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        list_all = libproc.proc_listallpids
        list_all.argtypes = [ctypes.c_void_p, ctypes.c_int]
        list_all.restype = ctypes.c_int
        pid_path = libproc.proc_pidpath
        pid_path.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        pid_path.restype = ctypes.c_int
    except (AttributeError, OSError):
        return None

    estimated = list_all(None, 0)
    if estimated <= 0:
        return None
    capacity = estimated + max(64, estimated // 8)
    pids = None
    count = 0
    for _ in range(3):
        pids = (ctypes.c_int * capacity)()
        count = list_all(pids, ctypes.sizeof(pids))
        if count <= 0:
            return None
        if count < capacity:
            break
        capacity = count + max(64, count // 8)
    if pids is None or count >= capacity:
        return None

    found: List[Tuple[int, Path]] = []
    for pid in pids[: min(count, capacity)]:
        if pid <= 0:
            continue
        buffer = ctypes.create_string_buffer(4096)
        length = pid_path(pid, buffer, ctypes.sizeof(buffer))
        if length <= 0:
            continue
        try:
            found.append((int(pid), Path(os.fsdecode(buffer.value))))
        except (TypeError, ValueError):
            continue
    return found or None


def _ps_process_paths(
    runner: CommandRunner,
) -> Optional[List[Tuple[int, Path]]]:
    try:
        result = runner.run(["/bin/ps", "-axo", "pid=,comm="], check=False)
    except WeChatMultiError:
        return None
    if result.returncode != 0:
        return None
    found: List[Tuple[int, Path]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pieces = stripped.split(None, 1)
        if len(pieces) != 2:
            continue
        pid_text, command = pieces
        try:
            found.append((int(pid_text), Path(command)))
        except ValueError:
            continue
    return found


def _process_paths(
    runner: CommandRunner,
) -> Optional[List[Tuple[int, Path]]]:
    return _libproc_process_paths() or _ps_process_paths(runner)


def _same_executable(left: Path, right: Path) -> bool:
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (
        right_stat.st_dev,
        right_stat.st_ino,
    )


def _main_processes(
    executable: Path, runner: CommandRunner
) -> Optional[List[int]]:
    processes = _process_paths(runner)
    if processes is None:
        return None
    return [
        pid
        for pid, process_path in processes
        if _same_executable(process_path, executable)
    ]


def _app_processes(
    app_root: Path, runner: CommandRunner
) -> Optional[List[int]]:
    processes = _process_paths(runner)
    if processes is None:
        return None
    root = app_root.resolve()
    return [
        pid
        for pid, process_path in processes
        if _is_within(process_path, root)
    ]


def launch_app(app: AppInfo, runner: CommandRunner, *, background: bool) -> None:
    argv = ["/usr/bin/open"]
    if background:
        argv.append("-g")
    argv.append(str(app.root))
    runner.run(argv, timeout=15)


def expected_container(app: AppInfo) -> Path:
    return Path.home() / "Library" / "Containers" / app.bundle_id


def wait_for_independent_container(
    app: AppInfo,
    runner: CommandRunner,
    *,
    timeout: float = 12.0,
) -> bool:
    if not app.is_clone:
        return True
    target = expected_container(app)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = _main_processes(app.executable, runner)
        if pids and target.is_dir():
            return True
        time.sleep(0.2)
    pids = _main_processes(app.executable, runner)
    return bool(pids) and target.is_dir()


def ensure_risk_acknowledged(
    output_dir: Path,
    *,
    accept_risk: bool,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    root = output_dir.expanduser().resolve()
    marker = root / RISK_ACK_FILE
    try:
        if marker.read_text(encoding="utf-8").strip() == RISK_ACK_VERSION:
            return
    except OSError:
        pass

    if not accept_risk:
        if not sys.stdin.isatty():
            raise WeChatMultiError(
                "{}\n非交互运行时请在确认风险后添加 --accept-risk。".format(
                    RISK_NOTICE
                )
            )
        print("\n风险提示：{}".format(RISK_NOTICE), file=sys.stderr)
        print(
            "如果仍要继续，请输入 yes：",
            file=sys.stderr,
            end="",
            flush=True,
        )
        answer = input().strip().lower()
        if answer != "yes":
            raise WeChatMultiError("已取消，未创建或启动微信副本。")

    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(RISK_ACK_VERSION + "\n", encoding="utf-8")


def _print_app(info: AppInfo, *, prefix: str = "") -> None:
    running = ""
    print(
        "{}{} — {} ({}) — {}".format(
            prefix, info.display_name, info.version, info.build, info.root
        )
        + running
    )


def _json_dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_doctor(args: argparse.Namespace, runner: CommandRunner) -> int:
    source = discover_wechat(args.app)
    output_dir = Path(args.output_dir).expanduser().resolve()
    clones = list_clones(output_dir)
    verification = runner.run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(source.root),
        ],
        check=False,
    )
    disk = shutil.disk_usage(str(output_dir.parent if output_dir.parent.exists() else Path.home()))
    signature_detail = (verification.stderr or verification.stdout).strip()
    if verification.returncode == 0:
        signature_status = "valid"
    elif "CSSMERR_TP_NOT_TRUSTED" in signature_detail:
        signature_status = "not_trusted"
    else:
        signature_status = "invalid"
    payload = {
        "tool_version": TOOL_VERSION,
        "platform": platform.platform(),
        "source": source.to_json(),
        "source_signature_status": signature_status,
        "source_signature_detail": signature_detail,
        "source_pids": _main_processes(source.executable, runner),
        "output_dir": str(output_dir),
        "free_disk_gb": round(disk.free / (1024 ** 3), 2),
        "clones": [item.to_json() for item in clones],
    }
    if args.json:
        _json_dump(payload)
        return 0

    print("微信多开器 {} 诊断".format(TOOL_VERSION))
    print("macOS：{}".format(platform.mac_ver()[0] or "未知"))
    print("官方微信：{} ({})".format(source.version, source.build))
    print("路径：{}".format(source.root))
    print("Bundle ID：{}".format(source.bundle_id))
    signature_labels = {
        "valid": "有效",
        "not_trusted": "存在，但系统信任链未通过",
        "invalid": "校验失败",
    }
    print("官方签名：{}".format(signature_labels[signature_status]))
    pids = payload["source_pids"]
    if pids is None:
        running_text = "当前环境无权查询"
    elif pids:
        running_text = "PID " + ", ".join(map(str, pids))
    else:
        running_text = "未运行"
    print("运行状态：{}".format(running_text))
    print("副本目录：{}".format(output_dir))
    print("剩余磁盘：{} GB".format(payload["free_disk_gb"]))
    print("已创建副本：{} 个".format(len(clones)))
    for clone in clones:
        _print_app(clone, prefix="  - ")
    return 0


def _create_requested(
    args: argparse.Namespace,
    runner: CommandRunner,
    *,
    reuse_existing: bool,
) -> Tuple[AppInfo, List[AppInfo], List[Dict[str, Any]]]:
    source = discover_wechat(args.app)
    _verify_official_source(source, runner)
    output_dir = Path(args.output_dir)
    specs = make_clone_specs(output_dir, args.extra)
    ensure_risk_acknowledged(
        output_dir,
        accept_risk=args.accept_risk,
        dry_run=args.dry_run,
    )
    reports: List[Dict[str, Any]] = []
    for spec in specs:
        state, backup = create_clone(
            source,
            spec,
            runner=runner,
            replace=args.replace,
            reuse_existing=reuse_existing,
            keep_url_schemes=args.keep_url_schemes,
            dry_run=args.dry_run,
        )
        reports.append(
            {
                "state": state,
                "spec": spec.to_json(),
                "backup": str(backup) if backup else None,
            }
        )

    clones: List[AppInfo] = []
    if not args.dry_run:
        for spec in specs:
            clones.append(read_app_info(spec.destination))
    return source, clones, reports


def command_create(args: argparse.Namespace, runner: CommandRunner) -> int:
    source, clones, reports = _create_requested(
        args, runner, reuse_existing=False
    )
    isolation: Dict[str, bool] = {}
    if not args.dry_run and args.launch:
        for clone in clones:
            launch_app(clone, runner, background=args.background)
            isolation[clone.bundle_id] = wait_for_independent_container(
                clone, runner
            )
        failed = [
            item for item in clones if not isolation.get(item.bundle_id, False)
        ]
        if failed:
            paths = "、".join(str(expected_container(item)) for item in failed)
            raise WeChatMultiError(
                "副本已收到启动请求，但没有同时确认主进程和独立数据容器。"
                "请不要登录，先退出副本。预期目录：{}".format(paths)
            )

    if args.json:
        _json_dump(
            {
                "source": source.to_json(),
                "dry_run": args.dry_run,
                "reports": reports,
                "launched": (
                    [item.to_json() for item in clones]
                    if args.launch and not args.dry_run
                    else []
                ),
                "independent_containers": isolation,
            }
        )
        return 0

    print("官方微信：{} ({})".format(source.version, source.build))
    for report in reports:
        spec = report["spec"]
        labels = {
            "created": "已创建",
            "planned": "计划创建",
            "reused": "已复用",
        }
        print("{}：{}".format(labels[report["state"]], spec["destination"]))
        if report["backup"]:
            print("  旧 App 已备份：{}".format(report["backup"]))
    if args.dry_run:
        print("这是 dry-run，没有修改任何文件。")
    elif args.launch:
        print(
            "已启动 {} 个微信副本，主进程和独立数据容器已确认。".format(
                len(clones)
            )
        )
    return 0


def command_start(args: argparse.Namespace, runner: CommandRunner) -> int:
    source, clones, reports = _create_requested(
        args, runner, reuse_existing=True
    )
    if args.dry_run:
        if args.json:
            _json_dump(
                {
                    "source": source.to_json(),
                    "dry_run": True,
                    "reports": reports,
                }
            )
        else:
            print("官方微信：{} ({})".format(source.version, source.build))
            labels = {"planned": "计划创建", "reused": "计划复用"}
            for report in reports:
                print(
                    "{}：{}".format(
                        labels.get(report["state"], report["state"]),
                        report["spec"]["destination"],
                    )
                )
            print("这是 dry-run，没有修改任何文件。")
        return 0

    if not args.no_original:
        launch_app(source, runner, background=args.background)
        time.sleep(0.4)
    for clone in clones:
        launch_app(clone, runner, background=args.background)
        time.sleep(0.4)

    isolation = {
        item.bundle_id: wait_for_independent_container(item, runner)
        for item in clones
    }
    missing = [
        item for item in clones if not isolation.get(item.bundle_id, False)
    ]
    if missing:
        paths = "、".join(str(expected_container(item)) for item in missing)
        raise WeChatMultiError(
            "副本已收到启动请求，但没有同时确认主进程和独立数据容器。"
            "请不要登录，先退出副本。预期目录：{}".format(paths)
        )

    if args.json:
        _json_dump(
            {
                "source": source.to_json(),
                "reports": reports,
                "launched": (
                    ([] if args.no_original else [source.to_json()])
                    + [item.to_json() for item in clones]
                ),
                "independent_containers": isolation,
            }
        )
    else:
        created = sum(1 for item in reports if item["state"] == "created")
        reused = sum(1 for item in reports if item["state"] == "reused")
        print(
            "已启动 {} 个微信（原版 {} 个，副本 {} 个；新建 {}，复用 {}），"
            "副本主进程和独立数据容器已确认。".format(
                len(clones) + (0 if args.no_original else 1),
                0 if args.no_original else 1,
                len(clones),
                created,
                reused,
            )
        )
    return 0


def command_list(args: argparse.Namespace, runner: CommandRunner) -> int:
    del runner
    clones = list_clones(Path(args.output_dir))
    if args.json:
        _json_dump([item.to_json() for item in clones])
        return 0
    if not clones:
        print("尚未创建微信副本。")
        return 0
    for clone in clones:
        _print_app(clone)
    return 0


def command_launch(args: argparse.Namespace, runner: CommandRunner) -> int:
    specs = make_clone_specs(Path(args.output_dir), args.extra)
    clones: List[AppInfo] = []
    for spec in specs:
        if not spec.destination.exists():
            raise WeChatMultiError(
                "副本不存在：{}。请先运行 start 或 create。".format(spec.destination)
            )
        clone = read_app_info(spec.destination)
        if (
            not clone.is_clone
            or clone.bundle_id != spec.bundle_id
            or clone.clone_index != spec.index
        ):
            raise WeChatMultiError(
                "副本身份校验失败，拒绝启动：{}".format(spec.destination)
            )
        _verify_clone_signature(clone, runner)
        clones.append(clone)

    source: Optional[AppInfo] = None
    if args.include_original:
        source = discover_wechat(args.app)
        _verify_official_source(source, runner)

    ensure_risk_acknowledged(
        Path(args.output_dir),
        accept_risk=args.accept_risk,
        dry_run=False,
    )

    if source is not None:
        launch_app(source, runner, background=args.background)
        time.sleep(0.4)
    for clone in clones:
        launch_app(clone, runner, background=args.background)
        time.sleep(0.4)
    missing = [
        item
        for item in clones
        if not wait_for_independent_container(item, runner)
    ]
    if missing:
        paths = "、".join(str(expected_container(item)) for item in missing)
        raise WeChatMultiError(
            "没有同时确认副本主进程和独立数据容器，请不要登录并退出副本。"
            "预期目录：{}".format(paths)
        )
    print(
        "已启动 {} 个微信，副本主进程和独立数据容器已确认。".format(
            len(clones) + (1 if args.include_original else 0)
        )
    )
    return 0


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", help="官方微信 .app 路径；通常可自动发现")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="副本目录（默认：%(default)s）",
    )


def _add_clone_options(parser: argparse.ArgumentParser) -> None:
    _add_common_paths(parser)
    parser.add_argument(
        "--extra",
        type=int,
        default=1,
        help="额外微信实例数，1-%s（默认：%%(default)s）" % MAX_EXTRA_INSTANCES,
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="用当前官方版本重建副本；旧 App 会备份，账号数据不删除",
    )
    parser.add_argument(
        "--keep-url-schemes",
        action="store_true",
        help="保留 weixin:// 等链接注册（默认移除，避免副本抢链接）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只展示计划，不写文件")
    parser.add_argument(
        "--accept-risk",
        action="store_true",
        help="确认已了解微信多开可能导致账号限制或封禁",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="macOS 微信多开器：不修改官方微信，创建独立 Bundle ID 的副本。"
    )
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查微信、签名和副本状态")
    _add_common_paths(doctor)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    create = subparsers.add_parser("create", help="创建或更新微信副本")
    _add_clone_options(create)
    create.add_argument("--launch", action="store_true", help="创建完成后启动副本")
    create.add_argument("--background", action="store_true", help="在后台启动")
    create.set_defaults(handler=command_create)

    start = subparsers.add_parser("start", help="一键确保副本存在并启动")
    _add_clone_options(start)
    start.add_argument("--no-original", action="store_true", help="不启动官方微信")
    start.add_argument("--background", action="store_true", help="在后台启动")
    start.set_defaults(handler=command_start)

    list_parser = subparsers.add_parser("list", help="列出本工具创建的副本")
    list_parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="副本目录"
    )
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=command_list)

    launch = subparsers.add_parser("launch", help="只启动已经创建的副本")
    _add_common_paths(launch)
    launch.add_argument("--extra", type=int, default=1, help="要启动的副本数")
    launch.add_argument(
        "--include-original", action="store_true", help="同时启动官方微信"
    )
    launch.add_argument("--background", action="store_true", help="在后台启动")
    launch.add_argument(
        "--accept-risk",
        action="store_true",
        help="确认已了解微信多开可能导致账号限制或封禁",
    )
    launch.set_defaults(handler=command_launch)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("此工具仅支持 macOS。")
    runner = CommandRunner()
    try:
        return int(args.handler(args, runner))
    except WeChatMultiError as exc:
        if getattr(args, "json", False):
            _json_dump({"error": str(exc)})
        else:
            print("错误：{}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
