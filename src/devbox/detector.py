"""DevBox Application Stack & Manifest Detection Engine.

Automatically inspects project directories to infer application name, framework,
internal listening port, and post-deploy lifecycle commands.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


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

    # 2. Python (pyproject.toml, requirements.txt, Pipfile)
    elif (path / "pyproject.toml").exists() or (path / "requirements.txt").exists() or any(path.glob("*.py")):
        if (path / "pyproject.toml").exists():
            detected_files.append("pyproject.toml")
        if (path / "requirements.txt").exists():
            detected_files.append("requirements.txt")

        # Inspect python entry files
        py_files = [f.name for f in path.glob("*.py")]
        detected_files.extend(py_files[:3])
        req_content = ""
        if (path / "requirements.txt").exists():
            try:
                req_content = (path / "requirements.txt").read_text(encoding="utf-8").lower()
            except Exception:
                pass

        if "fastapi" in req_content or "uvicorn" in req_content or any("main.py" == f for f in py_files):
            framework = "Python (FastAPI/Uvicorn)"
            internal_port = 8000
            post_deploy = "uvicorn main:app --host 0.0.0.0 --port 8000"
            confidence = "high"
        elif "flask" in req_content or any("app.py" == f for f in py_files):
            framework = "Python (Flask)"
            internal_port = 5000
            post_deploy = "flask run --host 0.0.0.0 --port 5000"
            confidence = "high"
        elif "django" in req_content or (path / "manage.py").exists():
            framework = "Python (Django)"
            internal_port = 8000
            post_deploy = "python manage.py runserver 0.0.0.0:8000"
            confidence = "high"
        else:
            framework = "Python"
            internal_port = 8000
            main_script = "main.py" if (path / "main.py").exists() else ("app.py" if (path / "app.py").exists() else (py_files[0] if py_files else "main.py"))
            post_deploy = f"python3 {main_script}"
            confidence = "medium"

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

    return {
        "success": True,
        "app_name": app_name,
        "framework": framework,
        "internal_port": internal_port,
        "post_deploy": post_deploy,
        "detected_files": detected_files,
        "confidence": confidence,
    }
