from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import project_path


class CodexError(RuntimeError):
    pass


def _codex_path() -> str | None:
    return shutil.which("codex")


def codex_status() -> dict:
    executable = _codex_path()
    if not executable:
        return {
            "available": False,
            "installed": False,
            "authenticated": False,
            "message": "Codex CLI is not installed or not on PATH.",
        }
    try:
        result = subprocess.run(
            [executable, "login", "status"],
            cwd=project_path(),
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        return {
            "available": False,
            "installed": True,
            "authenticated": False,
            "message": f"Codex CLI status check failed: {exc}",
        }
    output = (result.stdout + result.stderr).strip()
    authenticated = result.returncode == 0 and "logged in" in output.lower()
    if not authenticated:
        return {
            "available": False,
            "installed": True,
            "authenticated": False,
            "message": output or "Codex CLI is not logged in.",
        }
    return {
        "available": True,
        "installed": True,
        "authenticated": True,
        "message": output,
    }


def codex_unavailable_message(error_text: str) -> str:
    lowered = error_text.lower()
    if "not logged in" in lowered or "login" in lowered or "auth" in lowered:
        return "Codex CLI is not logged in. Run `codex login` before selecting a Codex model."
    if "rate limit" in lowered or "usage limit" in lowered or "quota" in lowered:
        return "Codex usage limit or quota is exhausted for this account."
    if "subscription" in lowered or "plan" in lowered or "entitlement" in lowered:
        return "This account does not appear to have access to the selected Codex model."
    return error_text.strip() or "Codex CLI call failed."


def chat(messages: list[dict[str, str]], model: str = "gpt-5.5", *, timeout: float = 300.0) -> str:
    executable = _codex_path()
    if not executable:
        raise CodexError("Codex CLI is not installed or not on PATH.")

    prompt = "\n\n".join(f"{message.get('role', 'user').upper()}:\n{message.get('content', '')}" for message in messages)
    output_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="astro-wiki-codex-", suffix=".txt", delete=False) as handle:
            output_path = Path(handle.name)
        result = subprocess.run(
            [
                executable,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "-m",
                model,
                "--output-last-message",
                str(output_path),
                prompt,
            ],
            cwd=project_path(),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise CodexError(codex_unavailable_message(result.stdout + result.stderr))
        if output_path.exists():
            content = output_path.read_text(encoding="utf-8", errors="ignore").strip()
            if content:
                return content
        return result.stdout.strip()
    except subprocess.TimeoutExpired as exc:
        raise CodexError(f"Codex CLI timed out after {timeout:.0f} seconds.") from exc
    finally:
        if output_path and output_path.exists():
            output_path.unlink(missing_ok=True)
