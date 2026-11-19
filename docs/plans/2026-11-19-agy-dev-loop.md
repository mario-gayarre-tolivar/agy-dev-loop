# agy-dev-loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir una Skill global reutilizable en Google Antigravity (`agy`) que ejecute desarrollo de software guiado por planes en un bucle interno aislado (Orquestador Python + Ejecutor + Revisor con cortocircuito) y mejore sus reglas mediante un bucle externo gobernado por telemetría, mini-benchmarks offline y aprobación humana.

**Architecture:** El sistema separa la lógica determinista (`scripts/core/` en Python stdlib) de la orquestación de agentes en `run_workflow` (`scripts/inner_loop.py` y `scripts/outer_loop.py`). El bucle interno procesa una subtarea (`- [ ]`) a la vez sobre un `git worktree` dedicado, cortocircuitando al Revisor LLM hasta que `git_guard`, verificación de paso `RED`, tests, linter y build pasen en verde. El bucle externo actualiza `.agents/loop/lessons.md` con poda automática (máximo 12 reglas / 600 tokens) y, tras acumular 3 o más corridas, propone parches acotados a `## Reglas Aprendidas` en los prompts globales solo si superan los mini-benchmarks offline (`py_fixture` y `js_fixture`).

**Tech Stack:** Python 3.12+ / 3.15 stdlib (`unittest`, `subprocess`, `pathlib`, `json`, `hashlib`, `re`, `dataclasses`), Node.js v24.22 (`node --test`), Git 2.54 (`git worktree`, `git diff`), Google Antigravity (`run_workflow`, `SKILL.md`, `skills.json`).

**Spec:** [Instrucciones.md](file:///C:/Users/Perrotruco/Workspace/Instrucciones.md) (convergencia y refinamiento crítico de Fase 2 en esta sesión).

## Global Constraints

- **Cero dependencias externas:** Prohibido requerir paquetes de `pip` o `npm` fuera de la biblioteca estándar de Python (`>=3.12`) y el runner nativo `node --test` de Node 24.
- **Compatibilidad dual de Python en Windows:** Todo código Python DEBE ejecutarse sin errores tanto en `python3` (`Python 3.12.10`, usado por `run_workflow`) como en `python` (`Python 3.15.0`).
- **Importación explícita en `run_workflow`:** Como `run_workflow` no importa módulos locales automáticamente, `inner_loop.py` y `outer_loop.py` DEBEN insertar `Path(__file__).resolve().parent` en `sys.path` antes de importar `core`.
- **Aislamiento en Git obligatorio:** El bucle nunca opera sobre un árbol sucio ni sobre `main`/`master`. El Orquestador Python gestiona un `git worktree` dedicado en disco y pasa `workspace="inherit"` a `agent()`.
- **Manifiesto `.agents/loop.json` obligatorio:** Ningún bucle corre si el proyecto carece de `.agents/loop.json` o si su `test_cmd` falla en la validación inicial en seco (`dry-run`).
- **Cortocircuito del Revisor LLM:** Prohibido invocar al agente Revisor (`model="inherit"`) si fallan `git_guard`, la verificación del paso `RED`, `test_cmd`, `lint_cmd` o `build_cmd`.
- **Mutación acotada de prompts y lecciones:** El cuerpo base de `executor.md` y `reviewer.md` es inmutable para el Optimizador; solo puede mutar la sección `## Reglas Aprendidas` con tope estricto de `max_rules = 12` y `max_tokens_est = 600`. El mismo tope rige para `.agents/loop/lessons.md`.
- **Límites duros por defecto:** `max_iter = 5`, `circuit_breaker = 3` fallos consecutivos (o 2 huellas de error/diff idénticas para escalar de `"flash"` a `"pro"` o abortar).
- **Micro-TDD Estricto (`GEMINI.md`):** Cada tarea escribe primero el stub tipado con `raise NotImplementedError("Not implemented")` y su test en `tests/`, verifica el fallo (`RED`), implementa el código mínimo para pasar (`GREEN`), refactoriza y hace commit.

## Review Focus

1. **Debilitamiento o borrado de tests existentes:** Un Ejecutor que elimina archivos de test o reduce aserciones (`assert`) en rutas cubiertas por `protected_test_globs` debe ser bloqueado por `check_protected_tests_diff` antes de correr el Revisor.
2. **Test nuevo tautológico (`assert True`):** Un test nuevo que pasa incluso cuando se revierten temporalmente los cambios en archivos de producción debe fallar en `verify_new_tests_fail_without_prod_changes`.
3. **Bucle estancado con el mismo error:** Dos iteraciones consecutivas con el mismo hash de salida de error y diff (`compute_error_fingerprint`) en tier `"flash"` deben escalar de inmediato a `"pro"` (y abortar si ya estaba en `"pro"`).
4. **Crecimiento descontrolado de `lessons.md` o `## Reglas Aprendidas`:** Insertar una regla número 13 o superar los 600 tokens estimados debe podar o rechazar el exceso sin corromper la sección inmutable del archivo.
5. **Repositorio sucio o sin tests verificables:** Intentar iniciar una corrida en un repositorio con cambios sin commitear o con un `test_cmd` roto debe abortar en seco antes de crear worktrees o invocar agentes.

---

## File Structure

```text
C:\Users\Perrotruco\Workspace\agy-dev-loop\
├── docs\
│   └── plans\
│       └── 2026-11-19-agy-dev-loop.md
├── skills\
│   └── agy-dev-loop\
│       ├── SKILL.md
│       ├── prompts\
│       │   ├── executor.md
│       │   ├── reviewer.md
│       │   └── optimizer.md
│       └── scripts\
│           ├── core\
│           │   ├── __init__.py
│           │   ├── manifest.py        # Task 1: Esquema, autodetección y dry-run de .agents/loop.json
│           │   ├── plan_tasks.py      # Task 2: Parser y actualización de subtareas (- [ ] / - [x])
│           │   ├── git_guard.py       # Task 3: Worktree, diff guard de tests protegidos, verificación RED y rollback
│           │   ├── eval_runner.py     # Task 4: Gate determinista, huella de error y escalado de tier / circuit-breaker
│           │   ├── memory.py          # Task 5: Telemetría JSON por corrida y podador de .agents/loop/lessons.md
│           │   └── optimizer_guard.py # Task 6: Parcheo acotado de ## Reglas Aprendidas y runner de mini-benchmarks
│           ├── inner_loop.py          # Task 7: Script de run_workflow para ejecutar un plan subtarea por subtarea
│           └── outer_loop.py          # Task 7: Script de run_workflow para retrospectiva y propuesta de mejora global
├── benchmarks\
│   ├── py_fixture\                    # Task 6: Mini-proyecto Python con bug/tarea determinista y unittest
│   └── js_fixture\                    # Task 6: Mini-proyecto Node 24 con bug/tarea determinista y node --test
└── tests\
    ├── __init__.py
    ├── test_manifest.py
    ├── test_plan_tasks.py
    ├── test_git_guard.py
    ├── test_eval_runner.py
    ├── test_memory.py
    ├── test_optimizer_guard.py
    └── test_workflow_contracts.py
```

---

## Contratos de Interfaz y Esquemas JSON de Agentes

### Esquemas de salida (`finish` en `run_workflow`)

```python
EXECUTOR_SCHEMA = {
    "type": "object",
    "required": ["subtask_index", "files_changed", "test_files_touched", "summary"],
    "properties": {
        "subtask_index": {"type": "integer"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "test_files_touched": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

REVIEWER_SCHEMA = {
    "type": "object",
    "required": ["approved", "blocking_issues", "lesson_candidate"],
    "properties": {
        "approved": {"type": "boolean"},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
        "lesson_candidate": {
            "type": ["object", "null"],
            "required": ["symptom", "rule"],
            "properties": {
                "symptom": {"type": "string"},
                "rule": {"type": "string"},
            },
        },
    },
}

OPTIMIZER_SCHEMA = {
    "type": "object",
    "required": ["target_prompt", "candidate_rules", "rationale"],
    "properties": {
        "target_prompt": {"type": "string", "enum": ["executor.md", "reviewer.md"]},
        "candidate_rules": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["rule_id", "text"],
                "properties": {
                    "rule_id": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
        "rationale": {"type": "string"},
    },
}
```

---

## Matriz Secuencial de Micro-TDD

| Orden | Tarea | Módulo Principal | Suite de Test (`python3 -m unittest`) | Estado |
| --- | --- | --- | --- | --- |
| 1 | Manifiesto `.agents/loop.json` y modo `init` con `dry-run` | `skills/agy-dev-loop/scripts/core/manifest.py` | `tests/test_manifest.py` | `[x]` |
| 2 | Parser de planes y control por subtarea (`- [ ]` a `- [x]`) | `skills/agy-dev-loop/scripts/core/plan_tasks.py` | `tests/test_plan_tasks.py` | `[x]` |
| 3 | Aislamiento `git worktree`, diff guard anti-trampa y verificador `RED` | `skills/agy-dev-loop/scripts/core/git_guard.py` | `tests/test_git_guard.py` | `[ ]` |
| 4 | Gate determinista, huella de error y escalado / circuit-breaker | `skills/agy-dev-loop/scripts/core/eval_runner.py` | `tests/test_eval_runner.py` | `[ ]` |
| 5 | Telemetría de corridas y podador de `.agents/loop/lessons.md` | `skills/agy-dev-loop/scripts/core/memory.py` | `tests/test_memory.py` | `[ ]` |
| 6 | Mutador acotado de `## Reglas Aprendidas` y mini-benchmarks offline | `skills/agy-dev-loop/scripts/core/optimizer_guard.py` | `tests/test_optimizer_guard.py` | `[ ]` |
| 7 | Scripts `inner_loop.py`, `outer_loop.py`, prompts y `SKILL.md` | `skills/agy-dev-loop/scripts/*.py` | `tests/test_workflow_contracts.py` | `[ ]` |

---

### Task 1: Manifiesto `.agents/loop.json` y Modo `init` con `dry-run`

**Files:**
- Create: `skills/agy-dev-loop/scripts/core/__init__.py`
- Create: `skills/agy-dev-loop/scripts/core/manifest.py`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `pathlib.Path`, `subprocess.run`, `json`
- Produces:
  - `@dataclass(frozen=True) class LoopManifest` con campos: `test_cmd: str`, `lint_cmd: str | None = None`, `build_cmd: str | None = None`, `protected_test_globs: tuple[str, ...] = ("tests/**", "test/**", "**/*.test.*", "**/*.spec.*")`, `max_iter: int = 5`, `circuit_breaker: int = 3`
  - `load_manifest(repo_root: Path) -> LoopManifest` (lanza `ValueError` si falta `.agents/loop.json`, si `test_cmd` está vacío o si `max_iter < 1` / `circuit_breaker < 1`)
  - `detect_stack_manifest(repo_root: Path) -> LoopManifest` (detecta Python `unittest`/`manage.py` o Node `package.json`/`node --test`; lanza `ValueError` si no hay comando de test verificable)
  - `init_manifest(repo_root: Path, run_dry_check: bool = True) -> Path` (escribe `.agents/loop.json` y si `run_dry_check=True` ejecuta `test_cmd`, lanzando `RuntimeError` si devuelve código distinto de 0)

- [x] **Step 1: Inicializar repositorio Git en `C:\Users\Perrotruco\Workspace\agy-dev-loop` y escribir stubs con `NotImplementedError` + tests en `tests/test_manifest.py`**

```python
class TestManifest(unittest.TestCase):
    def test_load_manifest_rejects_missing_or_empty_test_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                load_manifest(root)

    def test_detect_and_init_manifest_verifies_dry_run_success_and_rejects_failing_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            manifest_path = init_manifest(root, run_dry_check=True)
            self.assertTrue(manifest_path.exists())
            loaded = load_manifest(root)
            self.assertIn("unittest", loaded.test_cmd)
```

- [x] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_manifest.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented`

- [x] **Step 3: Implementar `LoopManifest`, `load_manifest`, `detect_stack_manifest` e `init_manifest` en `skills/agy-dev-loop/scripts/core/manifest.py`**

Validar tipos, serializar `.agents/loop.json` con indentación de 2 espacios y ejecutar `subprocess.run(manifest.test_cmd, cwd=repo_root, shell=True, capture_output=True, text=True, timeout=60)` cuando `run_dry_check=True`.

- [x] **Step 4: Ejecutar test runner para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_manifest.py -v`
Expected: PASS (`OK`)

- [x] **Step 5: Commit de Task 1**

```bash
git add skills/agy-dev-loop/scripts/core/__init__.py skills/agy-dev-loop/scripts/core/manifest.py tests/test_manifest.py
git commit -m "feat(core): add project loop manifest loader and dry-run initializer"
```

---

### Task 2: Parser de Planes y Control por Subtarea (`- [ ]` a `- [x]`)

**Files:**
- Create: `skills/agy-dev-loop/scripts/core/plan_tasks.py`
- Test: `tests/test_plan_tasks.py`

**Interfaces:**
- Consumes: `pathlib.Path`, `re`
- Produces:
  - `@dataclass(frozen=True) class PlanSubtask` con campos: `index: int`, `title: str`, `body: str`, `completed: bool`, `line_number: int`
  - `parse_plan_subtasks(plan_path: Path) -> list[PlanSubtask]`
  - `next_pending_subtask(plan_path: Path) -> PlanSubtask | None`
  - `mark_subtask_completed(plan_path: Path, subtask_index: int) -> None`

- [x] **Step 1: Escribir stubs con `NotImplementedError` en `plan_tasks.py` y tests en `tests/test_plan_tasks.py`**

```python
class TestPlanTasks(unittest.TestCase):
    def test_parse_and_advance_single_subtask_at_a_time(self):
        sample = "# Plan\n- [ ] **Step 1: Create A**\nDetails A\n- [ ] **Step 2: Create B**\nDetails B\n"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.md"
            p.write_text(sample, encoding="utf-8")
            first = next_pending_subtask(p)
            self.assertIsNotNone(first)
            self.assertEqual(first.index, 0)
            self.assertIn("Step 1", first.title)
            mark_subtask_completed(p, 0)
            second = next_pending_subtask(p)
            self.assertEqual(second.index, 1)
```

- [x] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_plan_tasks.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented`

- [x] **Step 3: Implementar `parse_plan_subtasks`, `next_pending_subtask` y `mark_subtask_completed` en `skills/agy-dev-loop/scripts/core/plan_tasks.py`**

Capturar bloques que inicien con `^\s*-\s*\[([ xX])\]\s+(.+)$` y asociar las líneas siguientes hasta el próximo checkbox o encabezado `##`/`###` como `body`. Reemplazar únicamente la línea `line_number` de la subtarea `subtask_index` al marcar `[x]`.

- [x] **Step 4: Ejecutar test runner para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_plan_tasks.py -v`
Expected: PASS (`OK`)

- [x] **Step 5: Commit de Task 2**

```bash
git add skills/agy-dev-loop/scripts/core/plan_tasks.py tests/test_plan_tasks.py
git commit -m "feat(core): add plan subtask parser and single-step checkbox updater"
```

---

### Task 3: Aislamiento `git worktree`, Diff Guard Anti-Trampa y Verificador `RED`

**Files:**
- Create: `skills/agy-dev-loop/scripts/core/git_guard.py`
- Test: `tests/test_git_guard.py`

**Interfaces:**
- Consumes: `pathlib.Path`, `subprocess`, `fnmatch`
- Produces:
  - `ensure_clean_repo(repo_root: Path) -> None` (lanza `RuntimeError` si `git status --porcelain` no está vacío)
  - `create_isolated_worktree(repo_root: Path, run_id: str) -> Path` (crea rama `agy-loop/<run_id>` y worktree fuera del árbol principal o en `.agents/worktrees/<run_id>`)
  - `check_protected_tests_diff(worktree_path: Path, base_ref: str, protected_globs: tuple[str, ...]) -> tuple[bool, str]` (rechaza archivos de test borrados o reducción neta de líneas `assert` en tests existentes respecto a `base_ref`)
  - `verify_new_tests_fail_without_prod_changes(worktree_path: Path, base_ref: str, protected_globs: tuple[str, ...], test_cmd: str) -> tuple[bool, str]` (si hay archivos o bloques de test nuevos, restaura temporalmente solo los archivos de producción a `base_ref`, corre `test_cmd` esperando código != 0 para demostrar `RED`, y restaura de inmediato los archivos de producción)
  - `checkpoint_commit(worktree_path: Path, message: str) -> str`
  - `rollback_to_ref(worktree_path: Path, ref: str) -> None`

- [ ] **Step 1: Escribir stubs con `NotImplementedError` en `git_guard.py` y tests sobre un repo Git temporal en `tests/test_git_guard.py`**

```python
class TestGitGuard(unittest.TestCase):
    def test_rejects_deleted_or_weakened_existing_tests(self):
        # Crea repo git temporal con tests/test_calc.py que tiene 2 asserts;
        # modifica el archivo dejando 1 assert y verifica que check_protected_tests_diff devuelve (False, reason)
        ...

    def test_verify_new_tests_fail_without_prod_changes_rejects_tautological_test(self):
        # Añade un test nuevo con assert True y un cambio en app.py;
        # verifica que verify_new_tests_fail_without_prod_changes devuelve (False, reason)
        ...
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_git_guard.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented`

- [ ] **Step 3: Implementar las funciones de `git_guard.py` con bloque `try / finally` en `verify_new_tests_fail_without_prod_changes`**

Garantizar que en `verify_new_tests_fail_without_prod_changes` los archivos de producción modificados se respalden en memoria antes de hacer `git checkout <base_ref> -- <prod_files>` y se restauren siempre en el bloque `finally`.

- [ ] **Step 4: Ejecutar test runner para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_git_guard.py -v`
Expected: PASS (`OK`)

- [ ] **Step 5: Commit de Task 3**

```bash
git add skills/agy-dev-loop/scripts/core/git_guard.py tests/test_git_guard.py
git commit -m "feat(core): add git worktree isolation, test diff guard, and RED verifier"
```

---

### Task 4: Gate Determinista, Huella de Error y Escalado / Circuit-Breaker

**Files:**
- Create: `skills/agy-dev-loop/scripts/core/eval_runner.py`
- Test: `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `LoopManifest` (Task 1), `check_protected_tests_diff`, `verify_new_tests_fail_without_prod_changes` (Task 3)
- Produces:
  - `@dataclass(frozen=True) class GateResult` con campos: `passed: bool`, `failed_stage: str | None`, `output: str`, `error_fingerprint: str`, `should_run_reviewer: bool`
  - `compute_error_fingerprint(output: str, diff_text: str) -> str` (normaliza timestamps/rutas efímeras y devuelve SHA-256 corto de 16 caracteres)
  - `run_deterministic_gate(worktree_path: Path, manifest: LoopManifest, base_ref: str, verify_red: bool = True) -> GateResult`
  - `decide_next_action(current_tier: str, consecutive_failures: int, previous_fingerprints: list[str], new_fingerprint: str, circuit_breaker: int = 3) -> str` (devuelve `"continue_flash"`, `"escalate_pro"` o `"abort_circuit_breaker"`)

- [ ] **Step 1: Escribir stubs con `NotImplementedError` en `eval_runner.py` y tests en `tests/test_eval_runner.py`**

```python
class TestEvalRunner(unittest.TestCase):
    def test_short_circuits_reviewer_when_deterministic_stage_fails(self):
        # Si test_cmd falla, passed=False y should_run_reviewer=False
        ...

    def test_escalates_immediately_on_repeated_error_fingerprint(self):
        action = decide_next_action(
            current_tier="flash",
            consecutive_failures=2,
            previous_fingerprints=["abc12345"],
            new_fingerprint="abc12345",
            circuit_breaker=3,
        )
        self.assertEqual(action, "escalate_pro")
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_eval_runner.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented`

- [ ] **Step 3: Implementar `compute_error_fingerprint`, `run_deterministic_gate` y `decide_next_action` en `skills/agy-dev-loop/scripts/core/eval_runner.py`**

Ejecutar en orden: `check_protected_tests_diff`, `verify_new_tests_fail_without_prod_changes` (si `verify_red=True`), `manifest.test_cmd`, `manifest.lint_cmd` y `manifest.build_cmd`. Cortocircuitar al primer fallo con `should_run_reviewer=False`. Solo si todos pasan, devolver `passed=True, should_run_reviewer=True`.

- [ ] **Step 4: Ejecutar test runner para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_eval_runner.py -v`
Expected: PASS (`OK`)

- [ ] **Step 5: Commit de Task 4**

```bash
git add skills/agy-dev-loop/scripts/core/eval_runner.py tests/test_eval_runner.py
git commit -m "feat(core): add deterministic gate with reviewer short-circuit and fingerprint escalation"
```

---

### Task 5: Telemetría de Corridas y Podador de `.agents/loop/lessons.md`

**Files:**
- Create: `skills/agy-dev-loop/scripts/core/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `pathlib.Path`, `json`
- Produces:
  - `@dataclass(frozen=True) class Lesson` con campos: `lesson_id: str`, `symptom: str`, `rule: str`, `hits: int`, `last_run: str`
  - `@dataclass(frozen=True) class RunMetrics` con campos: `run_id: str`, `repo_name: str`, `subtasks_total: int`, `subtasks_passed: int`, `iterations: int`, `rollbacks: int`, `reviewer_rejections: int`, `failure_fingerprints: tuple[str, ...]`, `status: str`
  - `estimate_tokens(text: str) -> int` (`max(1, len(text) // 4)`)
  - `load_lessons(lessons_path: Path) -> list[Lesson]`
  - `upsert_and_prune_lessons(lessons_path: Path, new_lessons: list[Lesson], max_lessons: int = 12, max_tokens_est: int = 600) -> list[Lesson]`
  - `record_run_metrics(metrics_dir: Path, metrics: RunMetrics) -> Path`
  - `should_trigger_global_optimization(metrics_dir: Path, min_runs: int = 3) -> bool`

- [ ] **Step 1: Escribir stubs con `NotImplementedError` en `memory.py` y tests en `tests/test_memory.py`**

```python
class TestMemory(unittest.TestCase):
    def test_prunes_lessons_above_max_count_and_token_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "lessons.md"
            items = [
                Lesson(f"L{i}", f"symptom {i}", f"actionable rule {i}", hits=(i % 3) + 1, last_run=f"2026-11-{10+i:02d}")
                for i in range(15)
            ]
            kept = upsert_and_prune_lessons(p, items, max_lessons=12, max_tokens_est=600)
            self.assertLessEqual(len(kept), 12)
            self.assertLessEqual(estimate_tokens(p.read_text(encoding="utf-8")), 600)
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_memory.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented`

- [ ] **Step 3: Implementar `memory.py` con deduplicación por `symptom`/`rule` y ordenación por `(hits, last_run)` descendente antes de podar**

- [ ] **Step 4: Ejecutar test runner para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_memory.py -v`
Expected: PASS (`OK`)

- [ ] **Step 5: Commit de Task 5**

```bash
git add skills/agy-dev-loop/scripts/core/memory.py tests/test_memory.py
git commit -m "feat(core): add run telemetry recorder and bounded lessons pruner"
```

---

### Task 6: Mutador Acotado de `## Reglas Aprendidas` y Mini-Benchmarks Offline

**Files:**
- Create: `skills/agy-dev-loop/scripts/core/optimizer_guard.py`
- Create: `benchmarks/py_fixture/calc.py`
- Create: `benchmarks/py_fixture/tests/test_calc.py`
- Create: `benchmarks/js_fixture/math.mjs`
- Create: `benchmarks/js_fixture/math.test.mjs`
- Test: `tests/test_optimizer_guard.py`

**Interfaces:**
- Consumes: `estimate_tokens` (Task 5), `subprocess`
- Produces:
  - `@dataclass(frozen=True) class PromptRule` con campos: `rule_id: str`, `text: str`
  - `LEARNED_RULES_START = "<!-- LEARNED_RULES_START -->"` y `LEARNED_RULES_END = "<!-- LEARNED_RULES_END -->"`
  - `apply_bounded_rule_patch(prompt_path: Path, candidate_rules: list[PromptRule], max_rules: int = 12, max_tokens_est: int = 600) -> str` (verifica que el contenido fuera de los marcadores `LEARNED_RULES_START/END` permanece bit a bit idéntico y lanza `ValueError` si `candidate_rules` excede `max_rules` o `max_tokens_est`)
  - `run_offline_benchmarks(benchmarks_root: Path) -> tuple[bool, dict[str, bool]]` (ejecuta `python3 -m unittest` en `benchmarks/py_fixture` y `node --test` en `benchmarks/js_fixture`)
  - `stage_and_validate_global_patch(prompt_path: Path, candidate_rules: list[PromptRule], benchmarks_root: Path) -> tuple[bool, str]` (valida el parche y revierte `prompt_path` en `finally` si falla cualquier benchmark)

- [ ] **Step 1: Crear fixtures `benchmarks/py_fixture` y `benchmarks/js_fixture`, escribir stubs en `optimizer_guard.py` y tests en `tests/test_optimizer_guard.py`**

```python
class TestOptimizerGuard(unittest.TestCase):
    def test_preserves_frozen_prompt_core_and_enforces_rule_budget(self):
        template = "# Core Prompt\nDo not change.\n## Reglas Aprendidas\n<!-- LEARNED_RULES_START -->\n<!-- LEARNED_RULES_END -->\n"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "executor.md"
            p.write_text(template, encoding="utf-8")
            rules = [PromptRule(f"R{i}", f"Rule {i}") for i in range(13)]
            with self.assertRaises(ValueError):
                apply_bounded_rule_patch(p, rules, max_rules=12, max_tokens_est=600)

    def test_offline_benchmarks_pass_with_stdlib_runners(self):
        ok, results = run_offline_benchmarks(Path(__file__).resolve().parent.parent / "benchmarks")
        self.assertTrue(ok)
        self.assertEqual(results, {"py_fixture": True, "js_fixture": True})
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_optimizer_guard.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented`

- [ ] **Step 3: Implementar `optimizer_guard.py` y los dos mini-benchmarks offline (`py_fixture` y `js_fixture`)**

- [ ] **Step 4: Ejecutar test runner para verificar estado `GREEN`**

Run: `python3 -m unittest tests/test_optimizer_guard.py -v`
Expected: PASS (`OK`)

- [ ] **Step 5: Commit de Task 6**

```bash
git add skills/agy-dev-loop/scripts/core/optimizer_guard.py benchmarks/ tests/test_optimizer_guard.py
git commit -m "feat(core): add bounded prompt rule mutator and offline Python/Node benchmark harness"
```

---

### Task 7: Scripts `inner_loop.py`, `outer_loop.py`, Prompts Base y `SKILL.md`

**Files:**
- Create: `skills/agy-dev-loop/prompts/executor.md`
- Create: `skills/agy-dev-loop/prompts/reviewer.md`
- Create: `skills/agy-dev-loop/prompts/optimizer.md`
- Create: `skills/agy-dev-loop/scripts/inner_loop.py`
- Create: `skills/agy-dev-loop/scripts/outer_loop.py`
- Create: `skills/agy-dev-loop/SKILL.md`
- Test: `tests/test_workflow_contracts.py`

**Interfaces:**
- Consumes: Módulos `manifest`, `plan_tasks`, `git_guard`, `eval_runner`, `memory` y `optimizer_guard` de `skills/agy-dev-loop/scripts/core/`
- Produces:
  - `EXECUTOR_SCHEMA`, `REVIEWER_SCHEMA`, `OPTIMIZER_SCHEMA` y funciones puras `build_executor_prompt(...)`, `build_reviewer_prompt(...)`, `build_optimizer_prompt(...)`
  - Punto de entrada de `inner_loop.py` y `outer_loop.py` compatible con `run_workflow` (detectando si `agent` y `phase` están inyectados por el runtime de `run_workflow` para permitir importación y test unitario limpio fuera de `run_workflow`)

- [ ] **Step 1: Escribir stubs en `inner_loop.py` y `outer_loop.py`, crear plantillas en `prompts/` con marcadores `<!-- LEARNED_RULES_START -->` / `<!-- LEARNED_RULES_END -->` y escribir tests de contrato en `tests/test_workflow_contracts.py`**

```python
class TestWorkflowContracts(unittest.TestCase):
    def test_prompts_contain_immutable_core_and_learned_rules_markers(self):
        prompts_dir = Path(__file__).resolve().parent.parent / "skills" / "agy-dev-loop" / "prompts"
        for name in ("executor.md", "reviewer.md"):
            content = (prompts_dir / name).read_text(encoding="utf-8")
            self.assertIn("<!-- LEARNED_RULES_START -->", content)
            self.assertIn("<!-- LEARNED_RULES_END -->", content)

    def test_reviewer_prompt_excludes_executor_conversation_history(self):
        prompt = build_reviewer_prompt(
            subtask_title="Step 1",
            subtask_body="Add add(a, b)",
            git_diff="+def add(a, b): return a + b",
            gate_output="Ran 1 test: OK",
        )
        self.assertIn("+def add(a, b)", prompt)
        self.assertNotIn("executor_history", prompt)
```

- [ ] **Step 2: Ejecutar test runner para verificar estado `RED`**

Run: `python3 -m unittest tests/test_workflow_contracts.py -v`
Expected: FAIL / ERROR con `NotImplementedError: Not implemented`

- [ ] **Step 3: Implementar `inner_loop.py`, `outer_loop.py`, `prompts/*.md` y `skills/agy-dev-loop/SKILL.md`**

Asegurar que `inner_loop.py` inserta `Path(__file__).resolve().parent` en `sys.path`, valida `.agents/loop.json`, crea el `git worktree`, procesa cada subtarea pendiente con cortocircuito del Revisor LLM, aplica `rollback_to_ref` ante fallo y registra métricas + `lessons.md` al terminar.

- [ ] **Step 4: Ejecutar la suite completa en `python3` (3.12) y `python` (3.15) para verificar estado `GREEN`**

Run: `python3 -m unittest discover -s tests -v && python -m unittest discover -s tests -v`
Expected: PASS (`OK` en todas las suites de `tests/` en ambos intérpretes)

- [ ] **Step 5: Commit de Task 7**

```bash
git add skills/agy-dev-loop/ tests/test_workflow_contracts.py docs/plans/2026-11-19-agy-dev-loop.md
git commit -m "feat(skill): add agy-dev-loop workflow scripts, prompt templates, and SKILL.md"
```
