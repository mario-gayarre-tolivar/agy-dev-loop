# agy-dev-loop

Skill global reutilizable para **Google Antigravity (`agy`)** que ejecuta desarrollo guiado por planes en un bucle Micro-TDD aislado con Git worktrees, validaciones deterministas anti-trampa y un bucle externo de mejora de reglas sujeto a benchmarks offline y aprobación humana.

Cero dependencias externas (`pip` o `npm`): funciona únicamente con biblioteca estándar de Python (`>= 3.12`), Git y el runner nativo `node --test` de Node 24.

---

## Requisitos

- **Google Antigravity (`agy`)** con soporte para `run_workflow`.
- **Python 3.12+** en `PATH` (`python3` para `run_workflow`).
- **Git 2.23+** (requerido para `git worktree` y `git restore --worktree`).
- **Node.js 24+** (solo para ejecutar el mini-benchmark offline `benchmarks/js_fixture` con `node --test`).

---

## Instalación en `agy`

### Opción 1: Registro desde el repositorio (recomendado)

Clona o guarda este repositorio (por ejemplo en `~/Workspace/agy-dev-loop`) y añade su carpeta `skills` a `~/.gemini/config/skills.json`:

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

Abre una sesión nueva de `agy` para cargar la skill `agy-dev-loop` globalmente en cualquier proyecto.

### Opción 2: Copia directa en skills globales

Copia `skills/agy-dev-loop/` dentro de `~/.gemini/config/skills/agy-dev-loop/`. Si usas el bucle externo desde esa copia, pasa `benchmarks_root` en `Args` apuntando a la carpeta `benchmarks/` del repositorio.

---

## Cómo funciona

El sistema separa el control determinista (`skills/agy-dev-loop/scripts/core/`) de la ejecución de agentes en `run_workflow`, dividiendo el trabajo en dos bucles:

### 1. Bucle interno (`scripts/inner_loop.py`)

Procesa un plan en Markdown **una subtarea (`- [ ]`) a la vez**:

1. **Validación previa (fail-closed):** exige repositorio Git limpio, manifiesto `.agents/loop.json` válido y una corrida en seco (`dry-run`) exitosa de `test_cmd` antes de crear worktrees o invocar agentes.
2. **Aislamiento:** crea un `git worktree` dedicado en `.agents/worktrees/<run_id>` sobre la rama `agy-loop/<run_id>` (nunca modifica `main`/`master`).
3. **Ejecutor (`flash` -> `pro`):** recibe solo la subtarea actual, las lecciones locales (`.agents/loop/lessons.md`) y el último fallo si reintenta. Empieza en tier `flash` y escala a `pro` ante 2 fallos seguidos o ante una huella de error repetida (`error_fingerprint`). Al alcanzar `circuit_breaker` (por defecto `3`), detiene la corrida.
4. **Gate determinista (0 tokens) con cortocircuito:** antes de llamar al Revisor LLM, Python verifica en orden:
   - `git_guard`: bloquea borrar, renombrar o reducir aserciones (incluso comentándolas) en archivos cubiertos por `protected_test_globs`.
   - Verificador `RED`: comprueba que todo test nuevo realmente falla cuando se revierten temporalmente los cambios de producción, bloqueando tests tautológicos (`assert True`).
   - Comandos del proyecto: `test_cmd` y los opcionales `lint_cmd` y `build_cmd`.
   Si cualquier etapa falla, hace `rollback` al último checkpoint y devuelve el error al Ejecutor sin invocar al Revisor.
5. **Revisor aislado (`inherit`):** solo se invoca cuando el gate determinista está en verde. Recibe únicamente la subtarea, el `git diff` y la salida de los checks (nunca el historial del Ejecutor).
6. **Checkpoint y memoria local:** si el Revisor aprueba, marca `- [x]` en el plan y crea un commit atómico. Al finalizar la corrida guarda telemetría JSON en `.agents/loop/telemetry/` y actualiza `.agents/loop/lessons.md` con poda automática (máximo 12 lecciones y 600 tokens estimados).

### 2. Bucle externo (`scripts/outer_loop.py`)

Evoluciona las reglas globales del flujo sin mutar el núcleo base ni degradar otros proyectos:

1. **Cadencia mínima:** solo se activa cuando existen al menos `3` archivos de telemetría válidos (`min_runs`, configurable).
2. **Mutación acotada:** el cuerpo base de `prompts/executor.md` y `prompts/reviewer.md` es inmutable. El Optimizador (`inherit`) solo puede proponer hasta `12` reglas atómicas (`<= 600` tokens estimados) entre `<!-- LEARNED_RULES_START -->` y `<!-- LEARNED_RULES_END -->`.
3. **Benchmarks offline + puerta humana:** valida las reglas candidatas contra `benchmarks/py_fixture` (`python3 -m unittest`) y `benchmarks/js_fixture` (`node --test`), genera el diff unificado y restaura el prompt original byte a byte devolviendo `status: "staged_pending_approval"`. Solo aplica el cambio si vuelves a ejecutar `outer_loop.py` con `apply_approved: True` y el objeto `approved_proposal` revisado.

---

## Uso paso a paso en cualquier proyecto

### Paso 1. Inicializar `.agents/loop.json` (una vez por repositorio)

Con el árbol Git limpio, pide a `agy` que use `agy-dev-loop` para inicializar el manifiesto, o ejecuta desde Python:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Users\Perrotruco\Workspace\agy-dev-loop\skills\agy-dev-loop\scripts").resolve()))
from core.manifest import init_manifest

init_manifest(Path(r"C:\ruta\a\tu\repo"), run_dry_check=True)
```

Esto crea `.agents/loop.json` tras comprobar que `test_cmd` sale con código `0`:

```json
{
  "test_cmd": "python3 -m unittest discover -s tests -v",
  "lint_cmd": null,
  "build_cmd": null,
  "protected_test_globs": ["tests/**", "test/**", "**/*.test.*", "**/*.spec.*"],
  "max_iter": 5,
  "circuit_breaker": 3
}
```

> **Importante:** `test_cmd` debe ser no interactivo y terminar con código `0` (por ejemplo `vitest run`, no `vitest` en modo watch).

Añade al `.gitignore` del proyecto y haz commit de ambos archivos:

```gitignore
.agents/worktrees/
.agents/loop/
```

*(Si prefieres versionar `.agents/loop/lessons.md` en Git, ignora solo `.agents/worktrees/` y `.agents/loop/telemetry/`, y haz commit de `lessons.md` antes de cada corrida.)*

### Paso 2. Ejecutar un plan aprobado

Con un plan en Markdown que contenga subtareas `- [ ]` (por ejemplo `docs/plans/2026-11-20-mi-feature.md`), pide a `agy`:

```text
Ejecuta el plan docs/plans/2026-11-20-mi-feature.md usando agy-dev-loop.
```

`agy` invocará `run_workflow`:

```python
run_workflow(
    ScriptPath=r"C:\Users\Perrotruco\Workspace\agy-dev-loop\skills\agy-dev-loop\scripts\inner_loop.py",
    Description="Execute plan subtasks with agy-dev-loop",
    Args={
        "repo_root": r"C:\ruta\a\tu\repo",
        "plan_path": "docs/plans/2026-11-20-mi-feature.md",
    },
)
```

Al terminar, revisa la rama `agy-loop/<run_id>` generada en `.agents/worktrees/<run_id>` y decide si fusionarla.

### Paso 3. Revisar y aprobar mejoras globales

Cuando `.agents/loop/telemetry/` acumule al menos 3 corridas, pide a `agy` correr el bucle externo en modo staging:

```python
run_workflow(
    ScriptPath=r"C:\Users\Perrotruco\Workspace\agy-dev-loop\skills\agy-dev-loop\scripts\outer_loop.py",
    Description="Propose benchmark-validated global rules from loop telemetry",
    Args={
        "metrics_dir": r"C:\ruta\a\tu\repo\.agents\loop\telemetry",
    },
)
```

Inspecciona `proposed_diff` y `rationale`. Para promover las reglas aprobadas, vuelve a invocar `outer_loop.py` pasando `"apply_approved": True` y `"approved_proposal": <propuesta_revisada>`.

---

## Parámetros de `Args`

| Script | Parámetro | Obligatorio | Por defecto / Notas |
| --- | --- | --- | --- |
| `inner_loop.py` | `repo_root` | Sí | Ruta absoluta al repositorio objetivo |
| `inner_loop.py` | `plan_path` | Sí | Ruta al plan Markdown (relativa a `repo_root` o absoluta) |
| `inner_loop.py` | `run_id` | No | Generado por timestamp UTC (`run-YYYYMMDDTHHMMSSZ`) |
| `inner_loop.py` | `verify_red` | No | `True` (verificación anti-test-tautológico) |
| `inner_loop.py` | `metrics_dir` / `lessons_path` | No | `.agents/loop/telemetry` y `.agents/loop/lessons.md` |
| `outer_loop.py` | `metrics_dir` | Sí | Carpeta con archivos JSON de telemetría |
| `outer_loop.py` | `min_runs` | No | `3` |
| `outer_loop.py` | `apply_approved` | No | `False` (staging sin mutar prompts) |
| `outer_loop.py` | `approved_proposal` | Si `apply_approved=True` | Diccionario de propuesta previamente revisado |
| `outer_loop.py` | `benchmarks_root` | No | `<repo>/benchmarks` |
| `outer_loop.py` | `max_rules` / `max_tokens_est` | No | `12` / `600` |

---

## Estructura del repositorio

```text
agy-dev-loop/
├── benchmarks/
│   ├── js_fixture/                 # Mini-benchmark offline (node --test)
│   └── py_fixture/                 # Mini-benchmark offline (python3 -m unittest)
├── docs/plans/
│   └── 2026-11-19-agy-dev-loop.md  # Plan de implementación ejecutado
├── skills/agy-dev-loop/
│   ├── SKILL.md                    # Definición e instrucciones de la skill
│   ├── prompts/
│   │   ├── executor.md             # Prompt base inmutable + ## Reglas Aprendidas
│   │   ├── reviewer.md             # Prompt base inmutable + ## Reglas Aprendidas
│   │   └── optimizer.md            # Prompt del Optimizador externo
│   └── scripts/
│       ├── core/
│       │   ├── manifest.py         # Carga, detección y dry-run de .agents/loop.json
│       │   ├── plan_tasks.py       # Parser de checkboxes (- [ ] / - [x]) con soporte de fences
│       │   ├── git_guard.py        # Worktree, diff guard, verificador RED y rollback
│       │   ├── eval_runner.py      # Gate determinista, huella de error y circuit-breaker
│       │   ├── memory.py           # Telemetría JSON y podador de lessons.md
│       │   ├── optimizer_guard.py  # Parcheo acotado y runner de benchmarks offline
│       │   └── workflow_logic.py   # Orquestación asíncrona de ambos bucles
│       ├── inner_loop.py           # Entrypoint run_workflow (bucle interno)
│       └── outer_loop.py           # Entrypoint run_workflow (bucle externo)
└── tests/                          # Suite completa (42 tests en python -m unittest)
```

---

## Desarrollo y tests

Ejecutar toda la suite del repositorio (sin dependencias externas):

```bash
python3 -m unittest discover -s tests -v
```
