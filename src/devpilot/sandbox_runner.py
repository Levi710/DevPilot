"""DevPilot sandbox execution harness.

The parent process applies Windows Job Object resource limits.  This harness
installs Python audit hooks before untrusted code runs; those hooks are a
runtime guard, not a claim of OS filesystem virtualisation.
"""

import os
import pathlib
import sys


def _is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Return whether *path* stays within *root*, including after link resolution."""
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def install_security_boundary(shadow_dir_str: str) -> None:
    """Register audit hooks that permit only shadow and Python-runtime reads."""
    shadow_root = pathlib.Path(shadow_dir_str).resolve()
    runtime_roots = {
        pathlib.Path(sys.base_prefix).resolve(),
        pathlib.Path(sys.prefix).resolve(),
    }

    def _violation(message: str) -> PermissionError:
        return PermissionError(f"Sandbox violation: {message}")

    def _allows_read(target_path: pathlib.Path) -> bool:
        return _is_within(target_path, shadow_root) or any(
            _is_within(target_path, root) for root in runtime_roots
        )

    def _is_null_device(file_arg: object) -> bool:
        """The Windows null device is safe and used by Python/pytest internals."""
        if not isinstance(file_arg, (str, bytes, os.PathLike)):
            return False
        return os.fsdecode(file_arg).replace("/", "\\").casefold() in {
            "nul",
            r"\\.\nul",
            r"\\?\nul",
        }

    def _security_audit_hook(event: str, args: tuple) -> None:
        # Block raw network operations even when a script bypasses proxy variables.
        if event in (
            "socket.connect",
            "socket.bind",
            "socket.sendto",
            "socket.sendmsg",
            "socket.getaddrinfo",
        ) or event.startswith("http.client.") or event == "urllib.request":
            raise _violation(f"direct network access is forbidden ({event}).")

        # Only shadow files and the selected Python runtime may be read.  Writes
        # and filesystem changes are restricted to the shadow workspace.
        if event in ("open", "io.open"):
            file_arg = args[0] if args else None
            mode_arg = args[1] if len(args) > 1 else "r"
            if isinstance(file_arg, (str, bytes, os.PathLike)):
                target_path = pathlib.Path(os.fsdecode(file_arg)).resolve()
                if _is_null_device(file_arg):
                    return
                is_write = isinstance(mode_arg, str) and any(
                    flag in mode_arg for flag in ("w", "a", "+", "x")
                )
                if is_write and not _is_within(target_path, shadow_root):
                    raise _violation(
                        f"write access outside shadow workspace is forbidden ({target_path})."
                    )
                if not is_write and not _allows_read(target_path):
                    raise _violation(
                        f"read access outside allowed runtime roots is forbidden ({target_path})."
                    )
        elif event in (
            "os.remove",
            "os.unlink",
            "os.rename",
            "os.replace",
            "os.mkdir",
            "os.rmdir",
            "shutil.rmtree",
        ):
            for path_arg in args:
                if isinstance(path_arg, (str, bytes, os.PathLike)):
                    target_path = pathlib.Path(os.fsdecode(path_arg)).resolve()
                    if not _is_within(target_path, shadow_root):
                        raise _violation(
                            f"filesystem modification outside shadow workspace is forbidden ({target_path})."
                        )

        if event in ("os.system", "subprocess.Popen", "os.spawn", "os.posix_spawn"):
            raise _violation(f"child process execution is forbidden ({event}).")

        if event.startswith("ctypes.") or event in ("_ctypes.dlopen", "_ctypes.LoadLibrary"):
            raise _violation(f"native binary execution is forbidden ({event}).")

    # Audit hooks are append-only for the life of this Python process.
    sys.addaudithook(_security_audit_hook)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: sandbox_runner.py <shadow_root> <target_script> [args...]\n")
        sys.exit(1)

    shadow_dir = sys.argv[1]
    target_script = sys.argv[2]
    shadow_root = pathlib.Path(shadow_dir).resolve()

    target_path = pathlib.Path(target_script).resolve()
    if not _is_within(target_path, shadow_root) or not target_path.is_file():
        sys.stderr.write("Target script must be a file inside the shadow workspace.\n")
        sys.exit(1)

    # Ensure both common Python source layouts resolve to the shadow copy, not
    # an installed package from the selected runtime.
    if str(shadow_root) not in sys.path:
        sys.path.insert(0, str(shadow_root))
    source_root = shadow_root / "src"
    if source_root.is_dir() and str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

    # Install the audit hook before the target source is opened.
    install_security_boundary(shadow_dir)

    # Adjust sys.argv so the target script sees standard arguments
    sys.argv = [target_script] + sys.argv[3:]

    # Execute target script in isolated __main__ namespace
    with open(target_path, "rb") as f:
        code = compile(f.read(), str(target_path), "exec")
        exec(code, {"__name__": "__main__", "__file__": str(target_path)})
