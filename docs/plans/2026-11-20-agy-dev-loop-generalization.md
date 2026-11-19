# agy-dev-loop Generalization & Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalizar `agy-dev-loop` para que cualquier desarrollador pueda instalarlo y ejecutarlo en Windows, macOS y Linux sobre cualquier stack con validador CLI determinista (Python, JS/TS, Go, Rust, Java, .NET, IaC y docs-as-code), hidratando dependencias en `git worktree` sin ensuciar `repo_root` y verificando las mejoras con una suite automatizada para los criterios `C1` a `C6`.

**Architecture:** Se amplía el núcleo stdlib (`manifest.py`, `git_guard.py`, `eval_runner.py`, `optimizer_guard.py`, `workflow_logic.py`) manteniendo retrocompatibilidad con `.agents/loop.json`. El dry-run inicial se traslada dentro del `git worktree` con un entorno hidratado (`PATH` de `.venv` y `node_modules/.bin`, `VIRTUAL_ENV`, `NODE_PATH` y `setup_cmd` opcional), eliminando el worktree automáticamente si la validación inicial falla. Los mini-benchmarks se empaquetan dentro de `skills/agy-dev-loop/benchmarks/` junto con una CLI pública (`scripts/cli.py` con subcomandos `init` y `doctor`) y una suite de evaluación binaria (`tests/test_generalization_eval.py`).

**Tech Stack:** Python 3.12+ / 3.15 stdlib (`unittest`, `argparse`, `shutil`, `sys`, `os`, `subprocess`, `pathlib`, `json`, `re`, `dataclasses`), Node.js v24+ (`node --test`), Git 2.23+ (`git worktree`, `git restore --worktree`).

**Spec:** Auditoría y matriz de evaluación `C1`–`C6` acordadas en la sesión de convergencia del `2026-11-20`.

## Global Constraints

- **Cero dependencias externas:** Prohibido añadir paquetes de `pip` o `npm` fuera de la biblioteca estándar de Python (`>= 3.12`) y `node --test`.
- **Compatibilidad dual de Python:** Toda la suite `tests/` DEBE pasar en `python3` (`Python 3.12.10`) y `python` (`Python 3.15.0`).
- **Frontera de validación determinista:** Todo proyecto DEBE tener un `test_cmd` ejecutable (`exit 0` / `exit != 0`). Se soporta `verify_red: false` en `.agents/loop.json` para trabajos técnicos sin paso contrafactual `RED` (migraciones, configuración, IaC, linters de documentación), pero nunca se permite un modo sin `test_cmd`.
- **Cero rutas personales y paquete autocontenido:** `git grep -i perrotruco` sobre `skills/` y `README.md` debe devolver vacío. `skills/agy-dev-loop/` debe contener sus propios `benchmarks/` sin depender de niveles padre (`parents[4]` eliminado).
- **Inmunidad de `repo_root`:** El dry-run de `test_cmd` en `run_inner_loop` nunca debe ensuciar `repo_root` con caches (`.pytest_cache`, `.coverage`, etc.); debe correr dentro del worktree aislado y destruir el worktree + rama si falla.
- **Micro-TDD Estricto (`GEMINI.md`):** Escribir primero tests/stubs que fallen en `RED`, verificar en terminal, implementar el código mínimo para pasar a `GREEN`, verificar en `python3` y `python`, y hacer commit por tarea.

## Review Focus

1. **Worktree huérfano tras fallo en `setup_cmd` o `verify_manifest_test_cmd`:** Si el dry-run inicial o `setup_cmd` falla dentro del worktree recién creado en `run_inner_loop`, `remove_isolated_worktree` DEBE ejecutarse en el bloque de error para borrar tanto `.agents/worktrees/<run_id>` como la rama `agy-loop/<run_id>`, dejando `repo_root` limpio.
2. **Regex `assert_regex` inválida en `.agents/loop.json`:** Un patrón mal formado en `assert_regex` debe rechazarse en `load_manifest` con `ValueError` antes de iniciar cualquier corrida.
3. **Resolución de Python en sistemas sin `python3`:** `resolve_python_cmd` y `run_offline_benchmarks` no deben fallar en entornos Windows donde solo exista `python` (`sys.executable`) o donde `python3.exe` sea el stub de Microsoft Store (`returncode != 0`).
4. **Comentarios de bloque y `--` en `git_guard.py`:** Comentar una aserción con `-- assert ...` o `/* assert ... */` en cualquier lenguaje soportado debe seguir siendo detectado como debilitamiento de test (`check_protected_tests_diff -> False`).
5. **Configuración hostil `diff.mnemonicPrefix=true` en `~/.gitconfig`:** `_run_git_diff` debe forzar `-c diff.mnemonicPrefix=false` para que Git nunca emita `+++ w/` o `+++ i/`.

---

## File Structure

```text
C:\Users\Perrotruco\Workspace\agy-dev-loop\
├── README.md                                              # Task 4: Documentación pública sin rutas personales
├── docs\plans\
│   ├── 2026-11-19-agy-dev-loop.md
│   └── 2026-11-20-agy-dev-loop-generalization.md          # Este plan
├── skills\agy-dev-loop\
│   ├── SKILL.md                                           # Task 4: Instrucciones portables con cli.py
│   ├── benchmarks\                                        # Task 3: Movido desde raíz para paquete autocontenido
│   │   ├── py_fixture\
│   │   └── js_fixture\
│   ├── prompts\
│   │   ├── executor.md
│   │   ├── reviewer.md
│   │   └── optimizer.md
│   └── scripts\
│       ├── cli.py                                         # Task 4: CLI pública (init y doctor)
│       ├── inner_loop.py
│       ├── outer_loop.py
│       └── core\
│           ├── __init__.py
│           ├── manifest.py                                # Task 1: Multi-stack, resolve_python_cmd y nuevos campos
│           ├── plan_tasks.py
│           ├── git_guard.py                               # Task 2: build_worktree_env, remove_isolated_worktree, multi-lang guard
│           ├── eval_runner.py                             # Task 2: Propagación de env, verify_red, test_timeout_sec, assert_regex
│           ├── memory.py
│           ├── optimizer_guard.py                         # Task 3: sys.executable en benchmarks offline
│           └── workflow_logic.py                          # Task 3: Dry-run aislado en worktree + setup_cmd + SKILL_ROOT/benchmarks
└── tests\
    ├── test_manifest.py                                   # Task 1: Tests de nuevos stacks, campos y resolve_python_cmd
    ├── test_git_guard.py                                  # Task 2: Tests de build_worktree_env, mnemonicPrefix, Go/Rust/Java/C# asserts
    ├── test_eval_runner.py                                # Task 2: Tests de manifest.verify_red=False, env y timeouts
    ├── test_optimizer_guard.py                            # Task 3: Ruta actualizada de benchmarks dentro de la skill
    ├── test_workflow_contracts.py                         # Task 3: Tests de dry-run en worktree, setup_cmd y limpieza ante fallo
    └── test_generalization_eval.py                        # Task 4: Suite binaria automatizada para C1..C6
```

---

## Contratos de Interfaz Actualizados

### Task 1 (`skills/agy-dev-loop/scripts/core/manifest.py`)

```python
DEFAULT_PROTECTED_TEST_GLOBS: tuple[str, ...] = (
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
    "**/*_test.rs",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*Test.java",
    "**/*Tests.java",
    "**/*Tests.cs",
    "**/*Test.cs",
)

@dataclass(frozen=True)
class LoopManifest:
    test_cmd: str
    lint_cmd: str | None = None
    build_cmd: str | None = None
    setup_cmd: str | None = None
    protected_test_globs: tuple[str, ...] = DEFAULT_PROTECTED_TEST_GLOBS
    max_iter: int = 5
    circuit_breaker: int = 3
    verify_red: bool = True
    test_timeout_sec: int = 120
    assert_regex: str | None = None

def resolve_python_cmd() -> str: ...
def load_manifest(repo_root: Path) -> LoopManifest: ...
def detect_stack_manifest(repo_root: Path) -> LoopManifest: ...
def verify_manifest_test_cmd(
    exec_cwd: Path,
    manifest: LoopManifest,
    env: dict[str, str] | None = None,
) -> None: ...
def init_manifest(repo_root: Path, run_dry_check: bool = True) -> Path: ...
```

### Task 2 (`skills/agy-dev-loop/scripts/core/git_guard.py` & `eval_runner.py`)

```python
# git_guard.py
def build_worktree_env(
    repo_root: Path,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]: ...

def remove_isolated_worktree(
    repo_root: Path,
    worktree_path: Path,
    run_id: str,
) -> None: ...

def check_protected_tests_diff(
    worktree_path: Path,
    base_ref: str,
    protected_globs: tuple[str, ...],
    assert_regex: str | None = None,
) -> tuple[bool, str]: ...

def verify_new_tests_fail_without_prod_changes(
    worktree_path: Path,
    base_ref: str,
    protected_globs: tuple[str, ...],
    test_cmd: str,
    timeout_sec: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]: ...

# eval_runner.py
def run_deterministic_gate(
    worktree_path: Path,
    manifest: LoopManifest,
    base_ref: str,
    verify_red: bool | None = None,
    env: dict[str, str] | None = None,
) -> GateResult: ...
```

### Task 4 (`skills/agy-dev-loop/scripts/cli.py`)

```python
def main(argv: list[str] | None = None) -> int:
    # Subcommands:
    #   init --repo <path> [--no-dry-run]
    #   doctor --repo <path>
    ...
```

---

## Matriz Secuencial de Micro-TDD

| Orden | Tarea | Módulos Principales | Suite de Test (`python3 -m unittest`) | Criterios Cubiertos | Estado |
| --- | --- | --- | --- | --- | --- |
| 1 | Intérprete Python portable, manifiesto multi-stack y nuevos campos de `LoopManifest` | `skills/agy-dev-loop/scripts/core/manifest.py` | `tests/test_manifest.py` | `C1`, `C3`, `C5` | `[x]` |
| 2 | Entorno hidratado de worktree, teardown seguro y guardián anti-trampa multi-lenguaje | `skills/agy-dev-loop/scripts/core/git_guard.py`, `eval_runner.py` | `tests/test_git_guard.py`, `tests/test_eval_runner.py` | `C2`, `C3`, `C4`, `C5` | `[ ]` |
| 3 | Benchmarks autocontenidos en la skill y dry-run aislado dentro del worktree con `setup_cmd` | `skills/agy-dev-loop/scripts/core/optimizer_guard.py`, `workflow_logic.py` | `tests/test_optimizer_guard.py`, `tests/test_workflow_contracts.py` | `C1`, `C2`, `C4` | `[ ]` |
| 4 | CLI pública (`init`/`doctor`), documentación sin rutas personales y suite de evaluación `C1`–`C6` | `skills/agy-dev-loop/scripts/cli.py`, `SKILL.md`, `README.md` | `tests/test_generalization_eval.py` | `C1`..`C6` | `[ ]` |

---

### Task 1: Intérprete Python Portable, Manifiesto Multi-Stack y Nuevos Campos de `LoopManifest`

**Files:**
- Modify: `skills/agy-dev-loop/scripts/core/manifest.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `sys.executable`, `shutil.which`, `re.compile`, `os.environ`, `subprocess.run`, `pathlib.Path`, `json`
- Produces: `LoopManifest` (con `setup_cmd`, `verify_red`, `test_timeout_sec`, `assert_regex`), `DEFAULT_PROTECTED_TEST_GLOBS` ampliado, `resolve_python_cmd`, `load_manifest`, `detect_stack_manifest`, `verify_manifest_test_cmd`, `init_manifest`

- [x] **Step 1: Añadir tests fallidos (`RED`) en `tests/test_manifest.py` para `resolve_python_cmd`, nuevos campos de `LoopManifest` y detección de `pytest`, `pnpm`/`yarn`/`bun`, Go, Rust, Java y .NET**

```python
def test_detect_stack_manifest_supports_pytest_go_rust_java_dotnet_and_lockfiles(self):
    # Verifica pyproject.toml con [tool.pytest -> pytest
    # Verifica go.mod -> go test ./... y go build ./...
    # Verifica Cargo.toml -> cargo test y cargo check
    # Verifica pnpm-lock.yaml + package.json -> pnpm test, pnpm run lint, pnpm run build
    # Verifica pom.xml -> mvn test y *.csproj -> dotnet test
    ...

def test_load_manifest_validates_new_optional_fields_and_rejects_bad_assert_regex(self):
    # Verifica setup_cmd, verify_red=False, test_timeout_sec=300, assert_regex válido
    # Verifica que assert_regex="[invalid" o verify_red="yes" o test_timeout_sec=0 lanzan ValueError
    ...
```

- [x] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_manifest.py -v`
Expected: FAIL / ERROR en los nuevos tests

- [x] **Step 3: Implementar `resolve_python_cmd`, los nuevos campos de `LoopManifest` y la detección multi-stack en `skills/agy-dev-loop/scripts/core/manifest.py`**

Recortar espacios en cada elemento de `protected_test_globs` (`tuple(item.strip() for item in raw_globs)`), compilar `assert_regex` con `re.compile` al cargar para fallar rápido si es inválido y pasar `timeout=manifest.test_timeout_sec` y `env` opcional en `verify_manifest_test_cmd`.

- [x] **Step 4: Ejecutar test runner en `python3` y `python` para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_manifest.py -v && python -m unittest tests/test_manifest.py -v`
Expected: PASS (`OK`)

- [x] **Step 5: Commit de Task 1**

```bash
git add skills/agy-dev-loop/scripts/core/manifest.py tests/test_manifest.py
git commit -m "feat(manifest): add multi-stack detection, portable Python resolution, and extensible manifest fields"
```

---

### Task 2: Entorno Hidratado de Worktree, Teardown Seguro y Guardián Anti-Trampa Multi-Lenguaje

**Files:**
- Modify: `skills/agy-dev-loop/scripts/core/git_guard.py`
- Modify: `skills/agy-dev-loop/scripts/core/eval_runner.py`
- Modify: `tests/test_git_guard.py`
- Modify: `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `LoopManifest` (Task 1)
- Produces: `build_worktree_env`, `remove_isolated_worktree`, `check_protected_tests_diff(..., assert_regex=None)`, `verify_new_tests_fail_without_prod_changes(..., timeout_sec=120, env=None)` en `git_guard.py`; `run_deterministic_gate(..., verify_red=None, env=None)` en `eval_runner.py`

- [ ] **Step 1: Añadir tests fallidos (`RED`) en `tests/test_git_guard.py` y `tests/test_eval_runner.py`**

```python
# En tests/test_git_guard.py:
def test_build_worktree_env_hydrates_venv_and_node_modules_and_remove_worktree_cleans_up(self):
    # Verifica que repo_root/.venv y repo_root/node_modules/.bin se inyectan en PATH, VIRTUAL_ENV y NODE_PATH
    # Verifica que remove_isolated_worktree elimina el directorio del worktree y la rama agy-loop/<run_id>
    ...

def test_multilang_assertions_mnemonic_prefix_and_comment_styles(self):
    # Verifica Go (t.Fatalf), Rust (assert_eq!), Java/C# (Assert.AreEqual / .Should()) y assert_regex personalizado
    # Verifica bloqueo al comentar con -- o /* ... */ y con git config diff.mnemonicPrefix true
    ...

# En tests/test_eval_runner.py:
def test_gate_respects_manifest_verify_red_false_custom_assert_regex_and_env(self):
    # Con manifest.verify_red=False, un cambio sin paso RED pasa el gate determinista y habilita al Revisor
    ...
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_git_guard.py tests/test_eval_runner.py -v`
Expected: FAIL / ERROR en los nuevos tests

- [ ] **Step 3: Implementar `build_worktree_env`, `remove_isolated_worktree`, `-c diff.mnemonicPrefix=false` y soporte multi-lenguaje en `git_guard.py` y `eval_runner.py`**

- [ ] **Step 4: Ejecutar test runner en `python3` y `python` para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_git_guard.py tests/test_eval_runner.py -v && python -m unittest tests/test_git_guard.py tests/test_eval_runner.py -v`
Expected: PASS (`OK`)

- [ ] **Step 5: Commit de Task 2**

```bash
git add skills/agy-dev-loop/scripts/core/git_guard.py skills/agy-dev-loop/scripts/core/eval_runner.py tests/test_git_guard.py tests/test_eval_runner.py
git commit -m "feat(guard): add worktree env hydration, safe teardown, and multi-language assertion guard"
```

---

### Task 3: Benchmarks Autocontenidos en la Skill y Dry-Run Aislado Dentro del Worktree con `setup_cmd`

**Files:**
- Move: `benchmarks/` -> `skills/agy-dev-loop/benchmarks/`
- Modify: `skills/agy-dev-loop/scripts/core/optimizer_guard.py`
- Modify: `skills/agy-dev-loop/scripts/core/workflow_logic.py`
- Modify: `tests/test_optimizer_guard.py`
- Modify: `tests/test_workflow_contracts.py`

**Interfaces:**
- Consumes: `build_worktree_env`, `remove_isolated_worktree` (Task 2), `verify_manifest_test_cmd` (Task 1), `sys.executable`
- Produces: `DEFAULT_BENCHMARKS_ROOT = SKILL_ROOT / "benchmarks"` en `workflow_logic.py`; dry-run y `setup_cmd` ejecutados dentro de `worktree_path` con limpieza automática del worktree si fallan.

- [ ] **Step 1: Mover `benchmarks/` a `skills/agy-dev-loop/benchmarks/` con `git mv` y añadir tests fallidos (`RED`) en `tests/test_optimizer_guard.py` y `tests/test_workflow_contracts.py`**

```python
# En tests/test_workflow_contracts.py:
def test_inner_loop_runs_setup_and_dry_run_inside_worktree_and_cleans_up_on_failure(self):
    # 1. test_cmd que crea un archivo cache no ignorado (.cache_artifact) durante el dry-run:
    #    verifica que repo_root permanece 100% limpio y que la corrida completa con éxito.
    # 2. test_cmd o setup_cmd que falla en el dry-run inicial:
    #    verifica que lanza RuntimeError, que agent_fn no se llama y que el worktree + rama agy-loop/<run_id> fueron eliminados.
    ...
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_optimizer_guard.py tests/test_workflow_contracts.py -v`
Expected: FAIL / ERROR en los tests actualizados y nuevos

- [ ] **Step 3: Actualizar `optimizer_guard.py` (`sys.executable`) y `workflow_logic.py` (`DEFAULT_BENCHMARKS_ROOT = SKILL_ROOT / "benchmarks"`, hidratación `build_worktree_env`, `setup_cmd`, dry-run en worktree y `remove_isolated_worktree` si falla)**

En `run_inner_loop`, limpiar cualquier `.cache` creado durante el dry-run inicial en el worktree (`rollback_to_ref(worktree_path, "HEAD")`) justo después de que `verify_manifest_test_cmd` pase, para que la primera subtarea arranque con el worktree limpio.

- [ ] **Step 4: Ejecutar test runner en `python3` y `python` para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_optimizer_guard.py tests/test_workflow_contracts.py -v && python -m unittest tests/test_optimizer_guard.py tests/test_workflow_contracts.py -v`
Expected: PASS (`OK`)

- [ ] **Step 5: Commit de Task 3**

```bash
git add skills/agy-dev-loop/ benchmarks/ tests/test_optimizer_guard.py tests/test_workflow_contracts.py
git commit -m "feat(workflow): bundle benchmarks inside skill and isolate dry-run inside hydrated worktree"
```

---

### Task 4: CLI Pública (`init` / `doctor`), Documentación Portable y Suite de Evaluación `C1`–`C6`

**Files:**
- Create: `skills/agy-dev-loop/scripts/cli.py`
- Modify: `skills/agy-dev-loop/SKILL.md`
- Modify: `README.md`
- Create: `tests/test_generalization_eval.py`

**Interfaces:**
- Consumes: `init_manifest`, `load_manifest`, `detect_stack_manifest`, `verify_manifest_test_cmd` (Task 1), `ensure_clean_repo`, `build_worktree_env` (Task 2), `DEFAULT_BENCHMARKS_ROOT` (Task 3)
- Produces: `main(argv: list[str] | None = None) -> int` en `skills/agy-dev-loop/scripts/cli.py` con subcomandos `init --repo <path> [--no-dry-run]` y `doctor --repo <path>`; suite de verificación `tests/test_generalization_eval.py` cubriendo `C1` a `C6`.

- [ ] **Step 1: Escribir stub `NotImplementedError` en `skills/agy-dev-loop/scripts/cli.py` y escribir la suite completa `tests/test_generalization_eval.py` para los criterios `C1` a `C6`**

```python
class GeneralizationEvalTests(unittest.TestCase):
    def test_c1_multi_os_python_resolution_and_zero_personal_paths(self): ...
    def test_c2_worktree_dependency_hydration_and_clean_repo_root(self): ...
    def test_c3_multistack_detection_and_multilang_git_guard(self): ...
    def test_c4_git_mnemonic_prefix_and_shallow_import_immunity(self): ...
    def test_c5_declarative_verify_red_false_and_timeout_config(self): ...
    def test_c6_cli_init_doctor_and_self_contained_benchmarks(self): ...
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_generalization_eval.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented` y rutas personales pendientes en docs

- [ ] **Step 3: Implementar `skills/agy-dev-loop/scripts/cli.py` y actualizar `skills/agy-dev-loop/SKILL.md` y `README.md` eliminando toda ruta personal**

- [ ] **Step 4: Ejecutar la suite completa en `python3` (3.12) y `python` (3.15) para verificar estado `GREEN`**

Run: `python3 -m unittest discover -s tests -v && python -m unittest discover -s tests -v`
Expected: PASS (`OK` en todas las suites de `tests/` incluyendo `test_generalization_eval.py`)

- [ ] **Step 5: Commit de Task 4**

```bash
git add skills/agy-dev-loop/ README.md tests/test_generalization_eval.py docs/plans/2026-11-20-agy-dev-loop-generalization.md
git commit -m "feat(cli): add init/doctor CLI, portable docs, and C1-C6 generalization evaluation suite"
```
