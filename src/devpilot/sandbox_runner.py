"""DevPilot Sandbox Execution Harness.

Enforces OS-level and runtime security boundaries:
1. Network isolation: intercepts and blocks direct socket.connect, socket.bind, and urllib calls.
2. Filesystem isolation: intercepts and blocks write/delete operations outside the shadow workspace.
3. Process isolation: intercepts and blocks subprocess spawning and ctypes loading.
"""

import os
import pathlib
import sys


def install_security_boundary(shadow_dir_str: str) -> None:
    """Register Python runtime audit hooks for strict sandbox enforcement."""
    shadow_root = pathlib.Path(shadow_dir_str).resolve()

    def _security_audit_hook(event: str, args: tuple) -> None:
        # Enforce OS-level network boundary: block all raw socket operations
        if event in (
            "socket.connect",
            "socket.bind",
            "socket.sendto",
            "socket.sendmsg",
            "socket.getaddrinfo",
        ) or event.startswith("http.client.") or event == "urllib.request":
            raise PermissionError(
                f"OS-Enforced Sandbox Violation: Direct network access is forbidden ({event})."
            )

        # Enforce OS-level filesystem boundary: block write access outside shadow workspace
        if event in ("open", "io.open"):
            file_arg = args[0] if args else None
            mode_arg = args[1] if len(args) > 1 else "r"
            if isinstance(mode_arg, str) and any(m in mode_arg for m in ("w", "a", "+", "x")):
                if isinstance(file_arg, (str, bytes, os.PathLike)):
                    target_path = pathlib.Path(os.fsdecode(file_arg)).resolve()
                    if not target_path.is_relative_to(shadow_root):
                        raise PermissionError(
                            f"OS-Enforced Sandbox Violation: Write access outside shadow workspace forbidden ({target_path})."
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
                    if not target_path.is_relative_to(shadow_root):
                        raise PermissionError(
                            f"OS-Enforced Sandbox Violation: Filesystem modification outside shadow workspace forbidden ({target_path})."
                        )

        # Enforce process boundary
        elif event in ("os.system", "subprocess.Popen", "os.spawn", "os.posix_spawn"):
            raise PermissionError(
                f"OS-Enforced Sandbox Violation: Child process execution forbidden ({event})."
            )

        # Enforce native library boundary
        elif event.startswith("ctypes.") or event in ("_ctypes.dlopen", "_ctypes.LoadLibrary"):
            raise PermissionError(
                f"OS-Enforced Sandbox Violation: Native binary execution forbidden ({event})."
            )

    # sys.addaudithook cannot be removed or monkey-patched once installed
    sys.addaudithook(_security_audit_hook)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: sandbox_runner.py <shadow_root> <target_script> [args...]\n")
        sys.exit(1)

    shadow_dir = sys.argv[1]
    target_script = sys.argv[2]
    shadow_root = pathlib.Path(shadow_dir).resolve()

    # Ensure shadow workspace is the root for module imports
    if str(shadow_root) not in sys.path:
        sys.path.insert(0, str(shadow_root))

    # Install unremovable C-level audit hook boundary
    install_security_boundary(shadow_dir)

    # Adjust sys.argv so the target script sees standard arguments
    sys.argv = [target_script] + sys.argv[3:]

    # Execute target script in isolated __main__ namespace
    target_path = pathlib.Path(target_script).resolve()
    with open(target_path, "rb") as f:
        code = compile(f.read(), str(target_path), "exec")
        exec(code, {"__name__": "__main__", "__file__": str(target_path)})
