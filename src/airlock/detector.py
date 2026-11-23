"""AirlockVM Application Stack & Manifest Detection Engine.

Automatically inspects project directories to infer application name, framework,
internal listening port, and post-deploy lifecycle commands.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
from typing import Any


def _detect_python_project(path: Path) -> tuple[str, str, int, str, str, list[str], dict[str, Any]]:
    """Inspects a Python project (pyproject.toml, uv.lock, requirements.txt, entrypoints).

    Returns:
        (app_name, framework, internal_port, post_deploy, confidence, detected_files, extras)
    """
    detected: list[str] = []
    app_name = path.name
    is_uv = False
    python_version = ""
    deps_blob_parts: list[str] = []

    pyproject_path = path / "pyproject.toml"
    if pyproject_path.exists():
        detected.append("pyproject.toml")
        try:
            py_doc = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            proj = py_doc.get("project", {}) or {}
            if isinstance(proj.get("name"), str) and proj["name"].strip():
                app_name = proj["name"].strip()
            for dep in proj.get("dependencies", []) or []:
                deps_blob_parts.append(str(dep).lower())
            for group_deps in (proj.get("optional-dependencies") or {}).values():
                for dep in group_deps or []:
                    deps_blob_parts.append(str(dep).lower())
            for group_deps in (py_doc.get("dependency-groups") or {}).values():
                for dep in group_deps or []:
                    if isinstance(dep, str):
                        deps_blob_parts.append(dep.lower())
            if "uv" in (py_doc.get("tool") or {}):
                is_uv = True
        except Exception:
            pass

    if (path / "uv.lock").exists():
        is_uv = True
        detected.append("uv.lock")

    py_ver_file = path / ".python-version"
    if py_ver_file.exists():
        detected.append(".python-version")
        try:
            python_version = py_ver_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except Exception:
            pass

    req_path = path / "requirements.txt"
    if req_path.exists():
        detected.append("requirements.txt")
        try:
            deps_blob_parts.append(req_path.read_text(encoding="utf-8").lower())
        except Exception:
            pass

    deps_blob = "\n".join(deps_blob_parts)

    # Collect root Python files excluding non-entrypoint build/test helpers
    ignored_py = {"setup.py", "conftest.py", "__init__.py"}
    py_files = sorted(f.name for f in path.glob("*.py") if f.name not in ignored_py)
    detected.extend(py_files[:3])

    runner_py = "uv run python" if is_uv else "python3"
    uv_prefix = "uv run " if is_uv else ""
    extras: dict[str, Any] = {
        "package_manager": "uv" if is_uv else "pip",
    }
    if python_version:
        extras["python_version"] = python_version

    def _find_asgi_target() -> str:
        for cand_file, target in (("main.py", "main:app"), ("app.py", "app:app"), ("server.py", "server:app")):
            if (path / cand_file).exists():
                return target
        if (path / "app" / "main.py").exists():
            return "app.main:app"
        return ""

    if "fastapi" in deps_blob or "uvicorn" in deps_blob:
        framework = "Python (uv, FastAPI/Uvicorn)" if is_uv else "Python (FastAPI/Uvicorn)"
        internal_port = 8000
        asgi_target = _find_asgi_target()
        post_deploy = f"{uv_prefix}uvicorn {asgi_target} --host 0.0.0.0 --port 8000" if asgi_target else ""
        confidence = "high" if asgi_target else "medium"
    elif "flask" in deps_blob:
        framework = "Python (uv, Flask)" if is_uv else "Python (Flask)"
        internal_port = 5000
        has_flask_entry = (path / "app.py").exists() or (path / "wsgi.py").exists()
        post_deploy = f"{uv_prefix}flask run --host 0.0.0.0 --port 5000" if has_flask_entry else ""
        confidence = "high" if has_flask_entry else "medium"
    elif "django" in deps_blob or (path / "manage.py").exists():
        framework = "Python (uv, Django)" if is_uv else "Python (Django)"
        internal_port = 8000
        post_deploy = f"{runner_py} manage.py runserver 0.0.0.0:8000" if (path / "manage.py").exists() else ""
        confidence = "high" if post_deploy else "medium"
    else:
        framework = "Python (uv)" if is_uv else "Python"
        internal_port = 8000
        # Only set post_deploy if a concrete runnable entrypoint script actually exists
        entry_script = next((cand for cand in ("main.py", "app.py", "server.py", "run.py") if (path / cand).exists()), "")
        if entry_script:
            post_deploy = f"{runner_py} {entry_script}"
            confidence = "medium"
        else:
            post_deploy = ""
            confidence = "medium" if (is_uv or pyproject_path.exists()) else "low"

    return app_name, framework, internal_port, post_deploy, confidence, detected, extras


def detect_application(local_path: str | Path) -> dict[str, Any]:
    """Inspects a project directory and infers its runtime profile.

    Returns:
        dict: {
            "app_name": str,
            "framework": str,
            "internal_port": int,
            "post_deploy": str,
            "detected_files": list[str],
            "confidence": str
        }
    """
    path = Path(local_path).expanduser().resolve()
    if not path.exists():
        return {
            "success": False,
            "error": f"Path does not exist: {path}",
            "app_name": path.name,
            "internal_port": 3000,
            "post_deploy": "",
        }

    detected_files: list[str] = []
    app_name = path.name
    framework = "Generic"
    internal_port = 3000
    post_deploy = ""
    confidence = "low"

    # Check for .env or .env.local port overrides
    env_port = None
    for env_name in (".env", ".env.local", ".env.development"):
        env_file = path / env_name
        if env_file.exists():
            detected_files.append(env_name)
            try:
                content = env_file.read_text(encoding="utf-8")
                match = re.search(r"^\s*PORT\s*=\s*(\d+)", content, re.MULTILINE)
                if match:
                    env_port = int(match.group(1))
            except Exception:
                pass

    # 1. Node.js (package.json)
    pkg_json = path / "package.json"
    if pkg_json.exists():
        detected_files.append("package.json")
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                pkg = json.load(f)
            app_name = pkg.get("name", app_name)
            scripts = pkg.get("scripts", {})
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            if "next" in deps:
                framework = "Next.js"
                internal_port = 3000
                post_deploy = "npm run build && npm start" if "build" in scripts else "npm start"
            elif "vite" in deps:
                framework = "Vite"
                internal_port = 5173
                post_deploy = "npm run preview" if "preview" in scripts else "npm run dev"
            elif "express" in deps or "koa" in deps or "fastify" in deps:
                framework = "Node.js (Express/HTTP)"
                internal_port = 3000
                post_deploy = "npm start" if "start" in scripts else "node server.js"
            elif "start" in scripts:
                framework = "Node.js"
                internal_port = 3000
                post_deploy = "npm start"
            else:
                framework = "Node.js"
                internal_port = 3000
                post_deploy = "node index.js" if (path / "index.js").exists() else "node server.js"

            confidence = "high"
        except Exception:
            pass

    # 2. Python (pyproject.toml, uv.lock, requirements.txt, or root *.py)
    elif (
        (path / "pyproject.toml").exists()
        or (path / "uv.lock").exists()
        or (path / "requirements.txt").exists()
        or any(path.glob("*.py"))
    ):
        app_name, framework, internal_port, post_deploy, confidence, py_detected, py_extras = _detect_python_project(path)
        detected_files.extend(py_detected)

    # 3. Rust (Cargo.toml)
    elif (path / "Cargo.toml").exists():
        detected_files.append("Cargo.toml")
        framework = "Rust"
        internal_port = 8080
        post_deploy = "cargo run --release"
        confidence = "high"

    # 4. Go (go.mod)
    elif (path / "go.mod").exists():
        detected_files.append("go.mod")
        framework = "Go"
        internal_port = 8080
        post_deploy = "go run ."
        confidence = "high"

    # 5. Docker Compose / Dockerfile
    if (path / "compose.yaml").exists() or (path / "docker-compose.yml").exists():
        compose_file = "compose.yaml" if (path / "compose.yaml").exists() else "docker-compose.yml"
        detected_files.append(compose_file)
        framework = f"{framework} + Docker Compose"
        try:
            cf_content = (path / compose_file).read_text(encoding="utf-8")
            port_match = re.search(r"-\s*[\"']?(\d+):(\d+)[\"']?", cf_content)
            if port_match:
                internal_port = int(port_match.group(2))
        except Exception:
            pass
        post_deploy = "docker compose up -d"
        confidence = "high"
    elif (path / "Dockerfile").exists() and not post_deploy:
        detected_files.append("Dockerfile")
        framework = "Docker"
        try:
            df_content = (path / "Dockerfile").read_text(encoding="utf-8")
            expose_match = re.search(r"^\s*EXPOSE\s+(\d+)", df_content, re.MULTILINE | re.IGNORECASE)
            if expose_match:
                internal_port = int(expose_match.group(1))
        except Exception:
            pass
        post_deploy = f"docker build -t {app_name.lower()} . && docker run -d -p {internal_port}:{internal_port} {app_name.lower()}"
        confidence = "high"

    # Override internal port if .env explicitly defined PORT
    if env_port is not None:
        internal_port = env_port

    result: dict[str, Any] = {
        "success": True,
        "app_name": app_name,
        "framework": framework,
        "internal_port": internal_port,
        "post_deploy": post_deploy,
        "detected_files": detected_files,
        "confidence": confidence,
    }
    if "py_extras" in locals():
        result.update(py_extras)
    return result
