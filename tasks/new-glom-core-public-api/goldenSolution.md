# Golden solution -- new-glom-core-public-api

**Task:** Declare the public API of glom.core
**Source:** net-new
**Detector:** `missing_dunder_all`

## Why this is the correct fix

The reference solution adds a module-level `__all__` listing the 33 public classes and functions defined in `glom/core.py`.

The list was derived from the knowledge layer's symbol table: every symbol whose `kind` is class or function, whose `qualname` belongs to this module rather than an import, and whose name does not start with an underscore.

The verifier checks the properties the declaration must have, not the exact list ordering, so any correct superset-free declaration passes.

## Verified behaviours

The verifier grades 5 case(s):

  - `tests_task/test_glom_core_public_api.py::test_declares_an_export_list`
  - `tests_task/test_glom_core_public_api.py::test_export_list_is_a_list_of_strings`
  - `tests_task/test_glom_core_public_api.py::test_every_exported_name_resolves`
  - `tests_task/test_glom_core_public_api.py::test_no_private_or_duplicate_names`
  - `tests_task/test_glom_core_public_api.py::test_covers_the_modules_own_public_api`

## Diff

```diff
--- a/glom/core.py
+++ b/glom/core.py
@@ -30,6 +30,42 @@
 
 from boltons.typeutils import make_sentinel
 from face.helpers import get_wrap_width
+
+__all__ = [
+    "AUTO",
+    "Auto",
+    "BadSpec",
+    "Call",
+    "Coalesce",
+    "CoalesceError",
+    "FILL",
+    "Fill",
+    "GlomError",
+    "Glommer",
+    "Inspect",
+    "Invoke",
+    "Let",
+    "Path",
+    "PathAccessError",
+    "PathAssignError",
+    "Pipe",
+    "Ref",
+    "ScopeVars",
+    "Spec",
+    "TType",
+    "TargetRegistry",
+    "UnregisteredTarget",
+    "Val",
+    "Vars",
+    "arg_val",
+    "chain_child",
+    "format_invocation",
+    "format_oneline_trace",
+    "format_target_spec_trace",
+    "glom",
+    "register",
+    "register_op",
+]
 
 # from boltons.funcutils import format_invocation
```
