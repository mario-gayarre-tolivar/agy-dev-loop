# agy-dev-loop

Skill global portable para **Google Antigravity (`agy`)** que ejecuta desarrollo guiado por planes en un bucle Micro-TDD aislado con Git worktrees, validaciones deterministas anti-trampa y un bucle externo de mejora de reglas sujeto a benchmarks offline autocontenidos y aprobación humana.

Cero dependencias externas (`pip` o `npm`): funciona únicamente con biblioteca estándar de Python (`>= 3.12`), Git y el runner nativo `node --test` de Node 24.

---

## Requisitos

- **Google Antigravity (`agy`)** con soporte para `run_workflow`.
- **Python 3.12+** en `PATH` (`python3` o `python`).
- **Git 2.23+** (requerido para `git worktree` y `git restore --worktree`).
- **Node.js 24+** (solo para ejecutar el mini-benchmark offline `benchmarks/js_fixture` con `node --test`).

---

## Instalación en `agy`

### Opción 1: `npx skills add` (autocontenido)

La skill incluye `skills/agy-dev-loop/benchmarks/` embebido, por lo que funciona sin clonar archivos adicionales:

```bash
npx skills add <repo-url-or-local-path> --skill agy-dev-loop
```

### Opción 2: Registro desde repositorio local

Clona el repositorio (por ejemplo en `~/Workspace/agy-dev-loop`) y registra su carpeta `skills` en `~/.gemini/config/skills.json`:

```json
{
  "entries": [
    {
      "path": "~/.gemini/config/skills"
    },
    {
      "path": "~/Workspace/agy-dev-loop/skills"
    }
  ]
}
```

### Opción 3: Copia directa

Copia `skills/agy-dev-loop/` dentro de `~/.gemini/config/skills/agy-dev-loop/`. Al incluir `benchmarks/` dentro de la skill, el bucle externo y `cli.py doctor` funcionan directamente sin pasar `benchmarks_root`.

---

## Cómo funciona

El sistema separa el control determinista (`skills/agy-dev-loop/scripts/core/`) de la ejecución de agentes en `run_workflow`:

### 1. Bucle interno (`scripts/inner_loop.py`)

Procesa un plan en Markdown **una subtarea (`- [ ]`) a la vez**:

1. **Validación previa (fail-closed) y aislamiento:** exige repositorio Git limpio y `.agents/loop.json` válido. Crea un `git worktree` en `.agents/worktrees/<run_id>` (`agy-loop/<run_id>`), hidrata el entorno (`PATH` con `.venv`/`venv` y `node_modules/.bin`, `VIRTUAL_ENV`, `NODE_PATH`), ejecuta `setup_cmd` si existe, valida `test_cmd` en seco dentro del worktree y limpia artefactos residuales del dry-run. Si falla, elimina el worktree automáticamente.
2. **Ejecutor (`flash` -> `pro`):** recibe solo la subtarea actual, `.agents/loop/lessons.md` y el último fallo. Escala a `pro` ante 2 fallos seguidos o ante una `error_fingerprint` repetida; aborta en `circuit_breaker`.
3. **Gate determinista (0 tokens) con cortocircuito:** verifica en orden:
   - `git_guard`: bloquea borrar, renombrar o reducir aserciones en `protected_test_globs` (con soporte multi-lenguaje y `assert_regex` opcional).
   - Verificador `RED` (si `verify_red` es `true`): comprueba que todo test nuevo falla al revertir cambios de producción.
   - Comandos del proyecto: `test_cmd`, `lint_cmd` y `build_cmd` con `test_timeout_sec`.
4. **Revisor aislado (`inherit`):** solo se invoca en verde; recibe únicamente la subtarea, `git diff` y salida del gate.
5. **Checkpoint y memoria local:** marca `- [x]`, crea commit atómico, guarda telemetría JSON en `.agents/loop/telemetry/` y poda `.agents/loop/lessons.md` (`<= 12` lecciones, `<= 600` tokens).

### 2. Bucle externo (`scripts/outer_loop.py`)

Evoluciona las reglas globales cuando existen al menos `3` corridas de telemetría (`min_runs`):

1. El Optimizador (`inherit`) propone hasta `12` reglas atómicas (`<= 600` tokens) entre `<!-- LEARNED_RULES_START -->` y `<!-- LEARNED_RULES_END -->` de `prompts/executor.md` o `prompts/reviewer.md`.
2. Valida las reglas contra `skills/agy-dev-loop/benchmarks/py_fixture` y `js_fixture`, devuelve `proposed_diff` con `status: "staged_pending_approval"` y restaura el prompt byte a byte.
3. Solo muta el prompt si vuelves a ejecutar `outer_loop.py` con `apply_approved: True` y `approved_proposal`.

---

## Uso paso a paso en cualquier proyecto

### Paso 1. Inicializar y verificar con la CLI pública

Con el repositorio limpio, ejecuta `init` (usa detección multi-stack y dry-run hidratado) y luego `doctor` tras hacer commit:

```bash
python3 <path-to-skill>/scripts/cli.py init --repo <abs-repo-path>
```

Stacks auto-detectados:
- **Python:** Django (`manage.py`), `pytest` (`pytest.ini`, `conftest.py`, `[tool.pytest]`, `*_test.py`), `unittest` (`tests/test_*.py`, `test_*.py`).
- **JavaScript / TypeScript:** `npm`, `pnpm`, `yarn`, `bun` (desde `package.json` + lockfile) o `node --test` (`*.test.js`/`mjs`/`ts`).
- **Go / Rust / Java / .NET:** `go.mod`, `Cargo.toml`, `pom.xml` (`mvnw`/`mvn`), `build.gradle*` (`gradlew`/`gradle`), `*.sln`/`*.csproj`.

Ejemplo de `.agents/loop.json` (incluyendo los campos opcionales de generalización):

```json
{
  "test_cmd": "python3 -m unittest discover -s tests -v",
  "lint_cmd": null,
  "build_cmd": null,
  "setup_cmd": null,
  "protected_test_globs": ["tests/**", "test/**", "**/tests/**", "**/test/**", "**/test_*.py", "**/*_test.py", "**/*_test.go", "**/*_test.rs", "**/*.test.*", "**/*.spec.*", "**/*Test.java", "**/*Tests.java", "**/*Tests.cs", "**/*Test.cs"],
  "max_iter": 5,
  "circuit_breaker": 3,
  "verify_red": true,
  "test_timeout_sec": 120,
  "assert_regex": null
}
```

Añade al `.gitignore` del proyecto y haz commit junto con `.agents/loop.json`:

```gitignore
.agents/worktrees/
.agents/loop/
```

Comprueba entorno, manifiesto y dry-run aislado sin ensuciar el repositorio:

```bash
python3 <path-to-skill>/scripts/cli.py doctor --repo <abs-repo-path>
```

### Paso 2. Ejecutar un plan aprobado

Con un plan en Markdown con subtareas `- [ ]` (por ejemplo `docs/plans/2026-11-20-mi-feature.md`), ejecuta `inner_loop.py` vía `run_workflow`:

```python
run_workflow(
    ScriptPath="<path-to-skill>/scripts/inner_loop.py",
    Description="Execute plan subtasks with agy-dev-loop",
    Args={
        "repo_root": "<abs-repo-path>",
        "plan_path": "docs/plans/2026-11-20-mi-feature.md",
    },
)
```

Al terminar, revisa la rama `agy-loop/<run_id>` generada en `.agents/worktrees/<run_id>`.

### Paso 3. Revisar y aprobar mejoras globales

Cuando `.agents/loop/telemetry/` acumule al menos 3 corridas, ejecuta `outer_loop.py`:

```python
run_workflow(
    ScriptPath="<path-to-skill>/scripts/outer_loop.py",
    Description="Propose benchmark-validated global rules from loop telemetry",
    Args={
        "metrics_dir": "<abs-repo-path>/.agents/loop/telemetry",
    },
)
```

Inspecciona `proposed_diff` y `rationale`. Para promover reglas aprobadas, re-ejecuta con `"apply_approved": True` y `"approved_proposal": <propuesta_revisada>`.

---

## Parámetros de `Args`

| Script | Parámetro | Obligatorio | Por defecto / Notas |
| --- | --- | --- | --- |
| `inner_loop.py` | `repo_root` | Sí | Ruta absoluta al repositorio objetivo |
| `inner_loop.py` | `plan_path` | Sí | Ruta al plan Markdown (relativa a `repo_root` o absoluta) |
| `inner_loop.py` | `run_id` | No | Generado por timestamp UTC (`run-YYYYMMDDTHHMMSSZ`) |
| `inner_loop.py` | `verify_red` | No | `None` (respeta `manifest.verify_red`; `False` lo desactiva) |
| `inner_loop.py` | `metrics_dir` / `lessons_path` | No | `.agents/loop/telemetry` y `.agents/loop/lessons.md` |
| `outer_loop.py` | `metrics_dir` | Sí | Carpeta con archivos JSON de telemetría |
| `outer_loop.py` | `min_runs` | No | `3` |
| `outer_loop.py` | `apply_approved` | No | `False` (staging sin mutar prompts) |
| `outer_loop.py` | `approved_proposal` | Si `apply_approved=True` | Diccionario de propuesta previamente revisado |
| `outer_loop.py` | `benchmarks_root` | No | `skills/agy-dev-loop/benchmarks` |
| `outer_loop.py` | `max_rules` / `max_tokens_est` | No | `12` / `600` |

---

## Estructura del repositorio

```text
agy-dev-loop/
├── docs/plans/
│   ├── 2026-11-19-agy-dev-loop.md
│   └── 2026-11-20-agy-dev-loop-generalization.md
├── skills/agy-dev-loop/
│   ├── SKILL.md                    # Definición e instrucciones portables
│   ├── benchmarks/                 # Mini-benchmarks offline autocontenidos
│   │   ├── js_fixture/             # node --test
│   │   └── py_fixture/             # unittest
│   ├── prompts/
│   │   ├── executor.md             # Prompt base inmutable + ## Reglas Aprendidas
│   │   ├── reviewer.md             # Prompt base inmutable + ## Reglas Aprendidas
│   │   └── optimizer.md            # Prompt del Optimizador externo
│   └── scripts/
│       ├── cli.py                  # CLI pública (init y doctor)
│       ├── core/
│       │   ├── manifest.py         # Carga, detección multi-stack y dry-run
│       │   ├── plan_tasks.py       # Parser de checkboxes (- [ ] / - [x])
│       │   ├── git_guard.py        # Worktree, hidratación, diff guard y RED
│       │   ├── eval_runner.py      # Gate determinista, fingerprint y circuit-breaker
│       │   ├── memory.py           # Telemetría JSON y podador de lessons.md
│       │   ├── optimizer_guard.py  # Parcheo acotado y benchmarks offline
│       │   └── workflow_logic.py   # Orquestación asíncrona de ambos bucles
│       ├── inner_loop.py           # Entrypoint run_workflow (bucle interno)
│       └── outer_loop.py           # Entrypoint run_workflow (bucle externo)
└── tests/                          # Suite completa en python3/python -m unittest
```

---

## Desarrollo y tests

Ejecutar toda la suite del repositorio (sin dependencias externas):

```bash
python3 -m unittest discover -s tests -v
```
